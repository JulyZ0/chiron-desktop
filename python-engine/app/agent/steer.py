"""运行中指导（steer）—— 在轮次边界把用户的追加指令注入正在跑的会话。

与 `interrupt.py` 同一个通道形状（Redis 信号 + 进程内快路径 + 归属校验在调用端），
区别只在**读到的是文本而非停止信号**：run 继续，只是多了一条用户约束。

只在轮次边界注入：工具执行到一半插消息会让 `tool_calls` 与 tool 结果对不上
（与 interrupt 只在轮次边界检查是同一个理由）。跨副本靠 Redis list，本副本走本地队列。

## 两条寻址方式（G19）

* **按会话**（既有）：用户在界面上给正在跑的**顶层**回合补一句话，`/v1/agent/steer`
  带 `session_id`。
* **按子 run**（G19 新增）：父 Agent / 界面给**运行中的子 Agent** 追加指令。子 Agent 跑在
  合成会话 `sub:<父>:<run>` 里、不在 `run_registry`（那里是顶层回合的归属），所以按会话
  寻址时准入直接回 ``no active agent for this session``。这里让 runner 用
  :func:`register_child_run` 把 ``run_id → (子会话, 父会话)`` 登记进来，调用方用
  :func:`steer` 按 run 寻址，写入的仍是**子会话的既有 steer 队列** —— runtime 每轮开头
  `drain_steer(task.session_id)` 的消费路径一个字都不用改。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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

# ── G19：按子 run 寻址 ─────────────────────────────────────────────────────

#: 单个子 run **待注入**的指令条数上限。算的是 pending（子 Agent 取走即还额度），
#: 不是历史累计 —— 否则一个长跑的子 Agent 会被永久锁死。
#:
#: 溢出时**明确报错**（:data:`CODE_QUEUE_FULL`）：静默丢弃会让父 Agent 以为话带到了，
#: 而子 Agent 永远等不到那条约束。
MAX_CHILD_STEER_PER_RUN = 8

CODE_OK = "ok"
CODE_RUN_NOT_FOUND = "run_not_found"
CODE_RUN_FINISHED = "run_finished"
CODE_INVALID_MESSAGE = "invalid_message"
CODE_QUEUE_FULL = "queue_full"
CODE_DELIVERY_FAILED = "delivery_failed"

#: ``run_id → (子会话 id, 父会话 id)``。由 runner 在开跑时登记、收尾时注销。
#: 与 `subagent_registry`（进程内任务句柄）刻意分开：那张表要在跨副本取消时广播，
#: 这里只回答"这条追加指令该投到哪个队列"，进程内足够。
_RUN_TARGETS: dict[str, tuple[str, str]] = {}
#: ``子会话 id → run_id``：drain 时据此把"取走"折算回 per-run 额度。
_RUN_BY_SESSION: dict[str, str] = {}
#: 已经收尾的 run：再 steer 必须回 ``run_finished``，绝不静默成功。
#:
#: 用**有界 FIFO**（插入序 dict）而不是无界 set：每个子 run 收尾都会留一个 id，
#: 长驻引擎上这就是一条只增不减的泄漏。"已结束"只需要回答**最近**的 run（界面看到
#: 的卡片、刚收尾的委派）；更早的 id 早已不会被 steer，丢掉不影响语义。
_FINISHED: dict[str, None] = {}
_FINISHED_MAX = 4096
#: ``run_id → 待注入条数``。
_PENDING: dict[str, int] = {}


@dataclass(frozen=True)
class SteerResult:
    """一次按 run 寻址的投递结果。

    失败**必须带错误码**：调用方（HTTP 层 / 另一个 Agent）要据此区分"该重试"与"不该"。
    只给一句自然语言，重试逻辑就只能靠猜。
    """

    ok: bool
    code: str = CODE_OK
    reason: str = ""
    child_session: str = ""


def _mark_finished(run_id: str) -> None:
    """把 run 记进"已结束"（有界 FIFO）。重复记同一 id 会刷新它的位置。"""
    _FINISHED.pop(run_id, None)
    _FINISHED[run_id] = None
    while len(_FINISHED) > _FINISHED_MAX:
        _FINISHED.pop(next(iter(_FINISHED)))


def register_child_run(run_id: str, *, child_session_id: str, parent_agent_id: str) -> None:
    """登记一条运行中的子 run（runner 开跑时调用）。幂等：重复登记覆盖同一映射。"""
    if not run_id or not child_session_id:
        return
    _RUN_TARGETS[run_id] = (child_session_id, parent_agent_id or "")
    _RUN_BY_SESSION[child_session_id] = run_id
    _FINISHED.pop(run_id, None)
    _PENDING[run_id] = 0


def forget_child_run(run_id: str) -> None:
    """注销一条子 run（runner 收尾时调用），并清掉它残留的待注入条目。

    清队列是**安全属性**而不是清理癖好：子 Agent 收尾后残留的条目，会被下一次同会话
    的 run 取走（跨 run 误伤）—— 那句约束是给上一件事的，注入到下一件事里就是错误指令。
    幂等：重复调用不会重置任何正在跑的东西。
    """
    target = _RUN_TARGETS.pop(run_id, None)
    _PENDING.pop(run_id, None)
    if target is not None:
        child_session, _parent = target
        if _RUN_BY_SESSION.get(child_session) == run_id:
            _RUN_BY_SESSION.pop(child_session, None)
        # 翻转代数：与 clear 同一条路径，避免"clear 之后 Redis 才恢复"时旧副本复活。
        clear_steer_local(child_session)
    _mark_finished(run_id)


def child_session_of(run_id: str) -> str:
    """该 run 的合成子会话 id；未登记（或已注销）返回空串。"""
    target = _RUN_TARGETS.get(run_id)
    return target[0] if target is not None else ""


def parent_of(run_id: str) -> str:
    """派发该 run 的父会话 id；未登记返回空串。"""
    target = _RUN_TARGETS.get(run_id)
    return target[1] if target is not None else ""


def pending_count(run_id: str) -> int:
    """该 run 还有几条指令没被子 Agent 取走（有界队列的当前占用）。"""
    return _PENDING.get(run_id, 0)


async def steer(
    run_id: str,
    text: str,
    *,
    sender: str = "user",
    redis: Any = None,
) -> SteerResult:
    """父 Agent → 运行中的子 Agent：按 **run** 追加一条指令。

    只入队、**不打断**：它不 cancel run，也不把文本插到正在执行的工具调用中间
    （那会让 `tool_calls` 与 tool 结果错位）。子 Agent 在**下一轮**开头按既有
    `drain_steer` 取走。

    消息带发送者（``来自父 Agent <id> 的追加指令：``），来源标成 ``parent:<id>``：
    历史里必须分得清"这句是人说的、watchdog 发的，还是父 Agent 说的"。

    目标未知 / 已结束 / 队列满都是**明确错误**，绝不静默成功 —— 这条通道的全部价值
    就是让父 Agent 相信"话带到了"。
    """
    target = _RUN_TARGETS.get(run_id)
    if target is None:
        if run_id in _FINISHED:
            return SteerResult(
                ok=False, code=CODE_RUN_FINISHED, reason=f"child run {run_id!r} already finished"
            )
        return SteerResult(
            ok=False, code=CODE_RUN_NOT_FOUND, reason=f"no child run {run_id!r} is registered"
        )
    child_session, _parent = target

    reason = _validate(text)
    if reason:
        return SteerResult(ok=False, code=CODE_INVALID_MESSAGE, reason=reason, child_session=child_session)

    pending = _PENDING.get(run_id, 0)
    if pending >= MAX_CHILD_STEER_PER_RUN:
        return SteerResult(
            ok=False,
            code=CODE_QUEUE_FULL,
            reason=(
                f"queue full ({MAX_CHILD_STEER_PER_RUN} pending for run {run_id!r}); "
                "wait until the child drains them"
            ),
            child_session=child_session,
        )
    # 先占额度再 await：并发 steer 的检查-占用之间不能有挂起点，否则上限会被击穿。
    _PENDING[run_id] = pending + 1

    message = f"来自父 Agent {sender} 的追加指令：\n{text}"
    client = await _resolve_redis(redis)
    ok, delivery_reason = await _enqueue(
        child_session, message, source=f"parent:{sender}", client=client
    )
    if not ok:
        _PENDING[run_id] = pending  # 投递没成功 ⇒ 额度还回去
        return SteerResult(
            ok=False,
            code=CODE_DELIVERY_FAILED,
            reason=delivery_reason,
            child_session=child_session,
        )
    return SteerResult(ok=True, code=CODE_OK, reason=delivery_reason, child_session=child_session)


def _release_pending(session_id: str, drained: list[tuple[str, str]]) -> None:
    """drain 出来的条数里有多少条是"父 Agent 的追加指令"，就从 per-run 额度里扣掉。

    只扣 ``parent:`` 来源：同一个会话队列里可能有 watchdog / 用户的条目（那些不受
    per-run 上限约束），拿总条数去扣会让额度提前归还。
    """
    run_id = _RUN_BY_SESSION.get(session_id)
    if not run_id:
        return
    count = sum(1 for _text, source in drained if source.startswith("parent:"))
    if count:
        _PENDING[run_id] = max(0, _PENDING.get(run_id, 0) - count)

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

    # 限流按会话计数：跨副本也要一致，所以优先用 Redis 的计数窗口。
    #
    # 子 run 的 steer（:func:`steer`）**不走这一段**：它的上限是 per-run 的
    # :data:`MAX_CHILD_STEER_PER_RUN`，而不是"每会话每分钟 X 条"——父 Agent 在一轮里
    # 连续补几条约束是正常用法，按用户输入的限流口径会把它误伤成 429。
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

    return await _enqueue(session_id, text, source=source, client=client)


async def _enqueue(session_id: str, text: str, *, source: str, client: Any) -> tuple[bool, str]:
    """把一条指导写进本地队列 + Redis（不做限流；两个入口共用这一份投递逻辑）。"""
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
        drained = [(text, source) for _, _, text, source in local]
        # G19：子 Agent 取走一轮 ⇒ 把 per-run 待注入额度还回去。
        _release_pending(session_id, drained)
        return drained

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
        _release_pending(session_id, out)
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
