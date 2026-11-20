"""跨副本审批/回答的等待：hybrid 唤醒 + 兜底轮询（从 `runtime.AgentRuntime` 抽出）。

## 这层在解决什么

审批与提问的等待方可能**不是**收到 HTTP 决策的那个副本：网关多副本部署、会话无亲和
路由、副本扩缩容，都会让"用户点了批准"落在 A 副本，而正在 `await` 的 run 在 B 副本。
仲裁者必须是一个**跨进程**的共享点，于是用 Redis 键承载决策（GET+DEL 单次消费 =
仲裁），再用一条 per-tc 唤醒频道把它变成"能立刻认领"（否则就是忙轮询）。

## 为什么唤醒不是正确性依赖

唤醒频道只是**加速器**。写入方 SET 键后 PUBLISH 一条 per-tc 唤醒，等待方收到即回到
顶部 GET+DEL；但"订阅建立前的写入"、"订阅端断连丢消息"两种情况都会丢唤醒，所以每轮
都先 GET（覆盖前者）、并以 `interval` 为兜底超时（覆盖后者）。**最坏退化成纯轮询，
绝不双消费、绝不永久卡住** —— deadline 到即返回 None。

搬到这里只是位置的改变：函数体与 `runtime` 内的原实现逐字一致（`runtime` 保留同名
方法做转发），语义零变化。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 跨副本审批/回答的兜底轮询间隔（秒）。唤醒频道只是**加速器**，不是仲裁者：
#: 写入方 SET 决策/答案键后 PUBLISH 一条 per-tc 唤醒，等待副本收到即提前 GET+DEL；
#: 但订阅建立前的写入、或订阅端断连丢消息，都靠这条兜底轮询收口 —— 所以最坏退化=纯轮询，
#: 绝不双消费、绝不永久卡住（deadline 到即返回 None）。0.15s 忙轮询放宽到此值把 QPS 降 ~10x，
#: 即时性反而更好（正常路径靠 wake 秒级认领，不再等下一轮 0.15s）。
REMOTE_WAIT_POLL_INTERVAL_SECONDS = 1.5


def approval_scope(user_id: str) -> str:
    """审批键的用户作用域段（JWT/网关注入）。非空时把审批隔离到该用户。"""
    return (user_id or "").strip()


def approval_key(tc_id: str, user_id: str = "") -> str:
    """跨副本审批决策键。带用户作用域，避免他人写入同键干扰或越权。"""
    from app.redis_keys import rkey

    scope = approval_scope(user_id)
    if scope:
        return rkey(f"approval:{scope}:{tc_id}")
    return rkey(f"approval:{tc_id}")


def approval_wake_channel(tc_id: str, user_id: str = "") -> str:
    """跨副本审批的 per-tc 唤醒频道（只叫醒等待该 tc_id 的副本，避免惊群）。"""
    from app.redis_keys import rkey

    scope = approval_scope(user_id)
    if scope:
        return rkey(f"approval:wake:{scope}:{tc_id}")
    return rkey(f"approval:wake:{tc_id}")


async def take_key_with_wake(
    key: str, channel: str, timeout: float, interval: float
) -> Any | None:
    """等待某 Redis 键出现并**原子取走**（GET+DEL 单次消费是仲裁，不可动摇）。

    hybrid：唤醒频道只是加速器。写入方 SET 后 PUBLISH `channel`（per-tc），这里先订阅
    再进循环 —— 每轮先 GET（覆盖"订阅建立前键已写入"的竞态），未命中就等唤醒或兜底
    `interval` 超时，二者先到即回到顶部再 GET。唤醒丢失（订阅端断连）时兜底轮询仍收口。
    因此最坏退化=纯轮询（间隔 `interval`），绝不双消费、deadline 到即返回 None 不卡死。
    """
    try:
        from app.redis_client import aclose_pubsub, get_redis

        redis = await get_redis()
    except Exception as e:  # noqa: BLE001
        logger.warning("remote wait: redis unavailable (%s)", e)
        return None
    if redis is None:
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    pubsub = None
    try:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
    except Exception as e:  # noqa: BLE001 - 订阅失败降级为纯轮询（仍正确，只是慢）
        logger.debug("remote wait: subscribe failed, poll-only fallback: %s", e)
        if pubsub is not None:
            try:
                await aclose_pubsub(pubsub)
            except Exception:  # noqa: BLE001
                logger.debug("remote wait: failed-subscribe pubsub close", exc_info=True)
        pubsub = None

    try:
        while True:
            try:
                raw = await redis.get(key)
                if raw is not None:
                    await redis.delete(key)  # 单次消费：只有一个副本能取走
                    return raw
            except Exception as e:  # noqa: BLE001 - 取键失败不致命，继续等待
                logger.debug("remote wait poll error: %s", e)
            remain = deadline - loop.time()
            if remain <= 0:
                return None
            wait = min(interval, remain)
            if pubsub is None:
                await asyncio.sleep(wait)
                continue
            try:
                # 收到本 tc 的唤醒 → 立即回到顶部 GET；无唤醒则最多等 wait 后兜底 GET。
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait)
            except Exception as e:  # noqa: BLE001 - 唤醒通道异常退回 sleep，不丢正确性
                logger.debug("remote wait wake error: %s", e)
                await asyncio.sleep(wait)
    finally:
        if pubsub is not None:
            try:
                await aclose_pubsub(pubsub)
            except Exception:  # noqa: BLE001 - 关连接失败不该盖住上面的返回
                logger.debug("remote wait pubsub close failed", exc_info=True)


async def poll_remote_decision(
    tc_id: str,
    timeout: float,
    user_id: str = "",
    interval: float = REMOTE_WAIT_POLL_INTERVAL_SECONDS,
):
    """跨副本审批：等决策键出现并取走（hybrid 唤醒 + 兜底轮询，见 `take_key_with_wake`）。

    `user_id` 非空时校验决策载荷的归属：不匹配的载荷（他人写入同键或串键）一律丢弃
    并继续等待 —— 键带用户作用域只是第一层，载荷校验才是"不能替他人批准"的判据。
    """
    from app.agent.runtime_types import ApprovalDecision

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            return None
        try:
            raw = await take_key_with_wake(
                approval_key(tc_id, user_id),
                approval_wake_channel(tc_id, user_id),
                remain,
                interval,
            )
        except Exception as e:  # noqa: BLE001 - 轮询异常不该把审批直接判失败
            logger.debug("remote approval poll failed: %s", e)
            return None
        if raw is None:
            return None
        # `from_raw` 同时接受新的 JSON 形态与旧的 "1"/"0"（见其 docstring）
        decision = ApprovalDecision.from_raw(raw)
        if decision is None:
            return None
        if user_id and decision.user_id != user_id:
            logger.warning(
                "remote approval decision ownership mismatch (tc=%s expected=%s got=%s) — dropped",
                tc_id,
                user_id,
                decision.user_id,
            )
            continue
        return decision
