"""运行中指导（steer）—— 在轮次边界把用户的追加指令注入正在跑的会话。

与 `interrupt.py` 同一个通道形状（Redis 信号 + 进程内快路径 + 归属校验在调用端），
区别只在**读到的是文本而非停止信号**：run 继续，只是多了一条用户约束。

只在轮次边界注入：工具执行到一半插消息会让 `tool_calls` 与 tool 结果对不上
（与 interrupt 只在轮次边界检查是同一个理由）。跨副本靠 Redis list，本副本走本地队列。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 队列存活时间（秒）。够一轮跑完即可，过期自动消失以免误伤下一次会话。
STEER_TTL_SECONDS = 300
#: 限流：每个会话每分钟最多接受这么多条，超出**报错**而不是静默丢弃。
MAX_PER_MINUTE = 6
#: 单条上限（字符）。指令是短句，超长的应该走正常提交。
MAX_CHARS = 2000
#: 队列长度上限，防止一条失控的 run 把 Redis 撑爆。
MAX_QUEUE = 20

#: 进程内快路径：session_id → [(置入时刻, 代数, 文本, 来源)]，同副本时零网络往返。
#:
#: 为什么每条都带**置入时刻**与**代数**（PRD 9.7 / H37）：
#: 本地副本只是一份**没有落 Redis 的临时副本**。没有 TTL 时，一条 `push_steer` 之后
#: 若 Redis 一直不可用，这条文本会永远留在进程里，等下一次同 session 的 run 起来时被
#: 当成"新指令"注入（跨会话误伤）；没有代数时，run 收尾 `clear_steer_local` 之后仍在
#: 队列里的旧副本会被下一次 drain 取走（顺序错乱、来源标错）。
_LOCAL: dict[str, list[tuple[float, int, str, str]]] = {}

#: 每个 session 的代数：`clear_steer_local` 自增 ⇒ 旧代数的本地副本全部作废。
_GENERATION: dict[str, int] = {}


def _generation(session_id: str) -> int:
    return _GENERATION.get(session_id, 0)


def _prune_local(session_id: str, now: float | None = None) -> None:
    """丢弃过期（超过 :data:`STEER_TTL_SECONDS`）与旧代数的本地副本。"""
    entries = _LOCAL.get(session_id)
    if not entries:
        _LOCAL.pop(session_id, None)
        return
    current = _generation(session_id)
    now = _now() if now is None else now
    fresh = [
        entry for entry in entries
        if entry[1] == current and now - entry[0] <= STEER_TTL_SECONDS
    ]
    if fresh:
        _LOCAL[session_id] = fresh
    else:
        _LOCAL.pop(session_id, None)

#: 只投递到本副本时的原因前缀：调用方据此告诉用户"跨副本投递没有得到保证"。
LOCAL_ONLY_PREFIX = "local-only"


def _encode(text: str, source: str) -> str:
    """Redis 里一条指导的编码：JSON（带来源），不再是裸文本。

    裸文本会让来源在跨副本时丢掉 —— 本地队列能记住，Redis 那条记不住，
    于是"同副本看得出来、跨副本看不出来"，是最难查的那种不一致。
    """
    return json.dumps({"text": text, "source": source}, ensure_ascii=False)


def _decode(raw: str) -> tuple[str, str]:
    """解出一条 (文本, 来源)。

    裸文本按 `user` 处理而不是拒收：滚动升级期间旧副本还在写纯文本，
    读端不兼容就会把这些指令整条丢掉 —— 丢指令比标错来源严重得多。
    """
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return raw, "user"
    if isinstance(obj, dict) and isinstance(obj.get("text"), str):
        return obj["text"], str(obj.get("source") or "user")
    return raw, "user"


def steer_key(session_id: str) -> str:
    """Redis 键名（与 `interrupt_key` 同一命名约定）。"""
    from app.redis_keys import rkey

    return rkey("engine:steer:") + session_id


async def _resolve_redis(redis: Any = None) -> Any:
    if redis is not None:
        return redis
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001 — 无 Redis 时仍有本地队列
        logger.debug("steer: redis unavailable (%s), local-only", exc)
        return None


def _validate(text: str) -> str | None:
    """返回拒绝原因；空串表示通过。"""
    if not text.strip():
        return "text is required"
    if len(text) > MAX_CHARS:
        return f"text too long (>{MAX_CHARS} chars)"
    return None


async def push_steer(session_id: str, text: str, *, source: str = "user",
                     redis: Any = None) -> tuple[bool, str]:
    """入队一条指导；返回 (是否成功, 原因)。

    成功语义（PRD 9.7 / H37）：``ok=True`` 只承诺"已进入**本副本**的投递队列"。
    Redis 写入成功时原因为空（跨副本投递已落实）；Redis 不可用或写入失败时原因以
    :data:`LOCAL_ONLY_PREFIX` 开头 —— **不得**在 Redis 成功之前承诺跨副本投递，
    否则用户会以为话已经带到另一个副本上的 run 了。

    失败必须让调用方看见：静默丢弃会让用户以为话已经带到了正在跑的 Agent 那里。
    """
    if not session_id:
        return False, "session_id is required"
    reason = _validate(text)
    if reason:
        return False, reason

    client = await _resolve_redis(redis)

    # 限流按会话计数：跨副本也要一致，所以优先用 Redis 的计数窗口。
    if client is not None:
        try:
            from app.redis_keys import rkey

            rate_key = rkey("engine:steer:rate:") + session_id
            count = await client.incr(rate_key)
            if count == 1:
                await client.expire(rate_key, 60)
            if count > MAX_PER_MINUTE:
                return False, f"rate limited ({MAX_PER_MINUTE}/min)"
        except Exception as exc:  # noqa: BLE001 — 计数失败不阻断投递，本地队列仍可用
            logger.warning("steer: rate counter failed: %s", exc)

    # 本地快路径与 Redis 都要写：同副本读本地零往返，跨副本读 Redis。
    # 本地副本带**时刻 + 代数**：TTL 到期或代数翻转（run 收尾 clear）后不再被 drain 到。
    _prune_local(session_id)
    _LOCAL.setdefault(session_id, []).append((_now(), _generation(session_id), text, source))
    _LOCAL[session_id] = _LOCAL[session_id][-MAX_QUEUE:]

    if client is None:
        return True, f"{LOCAL_ONLY_PREFIX}: redis unavailable, queued on this replica only"
    try:
        key = steer_key(session_id)
        await client.lpush(key, _encode(text, source))
        await client.ltrim(key, 0, MAX_QUEUE - 1)
        await client.expire(key, STEER_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("steer: redis push failed (local copy kept): %s", exc)
        return True, f"{LOCAL_ONLY_PREFIX}: redis push failed ({exc}), queued on this replica only"
    return True, ""


async def drain_steer(session_id: str, *, redis: Any = None) -> list[tuple[str, str]]:
    """取走该会话待注入的指导，按投递顺序返回 `(文本, 来源)`。

    返回来源而不是裸文本：注入的那条消息要落 `messages.source`，否则历史里
    分不清"这句是人说的还是 watchdog 发的"—— 而两者的可信度不同，用户有权知道。

    先本地后 Redis：本地命中通常是同副本（用户在同一个连接上），往 Redis 再查一遍
    会把同一条文本取到两次 —— 所以**两者取并集去重**没有意义，改为"本地非空就只信本地"。

    本地只交出**未过期且属于当前代数**的副本（见 :data:`_LOCAL`）；过期/旧代数的条目
    直接丢弃，绝不注入到别的 run 里。
    """
    if not session_id:
        return []
    _prune_local(session_id)
    local = _LOCAL.pop(session_id, [])
    if local:
        return [(text, source) for _, _, text, source in local]

    client = await _resolve_redis(redis)
    if client is None:
        return []
    try:
        key = steer_key(session_id)
        # lrange + delete 不是原子的：先后两次读之间可能有新消息进来，被一并删掉。
        # 用 RENAME 把"取走"收敛成单次原子操作；目标键必须唯一，否则多副本同时
        # drain 会互相覆盖（后到的 rename 覆盖前者，前者的内容就丢了）。
        import uuid

        take_key = f"{key}:drain:{uuid.uuid4().hex[:12]}"
        try:
            await client.rename(key, take_key)
        except Exception:  # noqa: BLE001 — 键不存在 = 没有待注入内容
            return []
        raw = await client.lrange(take_key, 0, -1) or []
        await client.delete(take_key)
        out = []
        for item in raw:
            text = item.decode() if isinstance(item, bytes) else str(item)
            out.append(_decode(text))
        out.reverse()  # lpush 是头插，取出后反序才是投递顺序
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("steer: drain failed: %s", exc)
        return []


def clear_steer_local(session_id: str) -> None:
    """清理本地队列并**翻转代数**（run 收尾时调用，避免残留误伤下一次会话）。

    只 `pop` 是不够的：如果还有一条 push 与 clear 并发（或 clear 之后 Redis 才恢复），
    旧副本会重新出现的 drain 结果里。自增代数让"clear 之前的本地条目"永久作废。
    """
    if not session_id:
        return
    _LOCAL.pop(session_id, None)
    _GENERATION[session_id] = _generation(session_id) + 1


def _now() -> float:
    import time

    return time.time()
