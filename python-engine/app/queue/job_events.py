"""作业生命周期事件（G13）—— 让队列任务的起停只有**一处**来源。

## 修前会怎样

队列任务的状态只在三处各存一份，靠约定对齐：

* Redis：stream 的 PEL + idempotency（``status``/``attempt``/``lease_until``）；
* 会话（PG）：``messages`` 里没有任何"某个后台作业开始/结束了"的条目；
* 前端（SSE）：只能按各自的"进度约定"再拼一份视图。

于是同一个任务有三套互相独立的进度，谁也不能回答"这个作业现在到底在哪一步"；
而作业失败进 DLQ 时，会话里连一条记录都没有 —— 用户只看到"没有结果"。

## 修后

worker 在关键节点发一条结构化事件（与 ``subagent.*`` 旁路事件同构，
经 Redis → Go relay → 会话 SSE）：

=========================  =================================================
``job.started``            一次尝试开始执行（status=running）
``job.settled``            一次尝试结束，status ∈ completed/failed/deferred/discarded
=========================  =================================================

字段（线上契约，Go 侧 relay 与前端都按它读）::

    {"type", "job_id", "task_type", "status", "session_id", "error", "ts", "tenant_id"}

**``job.settled`` 的语义是"这一次尝试结束了"，不等于"任务终态"**：被延迟重投的任务
会以 ``status=deferred`` 收掉本次尝试的卡片，重投后再发一条 ``job.started``。
把它们混成一件事，前端就只能二选一地撒谎（要么永远"运行中"，要么假装失败）。

## 旁路（性能边界）

与 ``app/agent/event_sink.py`` 同一原则：**投递失败绝不影响任务**。
:func:`emit_job_event` 自己吞掉异常并计数；调用点（worker）再兜一层 —— 两层都在，
是因为"投递出故障把任务搞失败"是最贵的一种耦合。

## 为什么发 pub/sub 而不是 Stream

实时通路与子 Agent 事件完全同构（Go 侧订阅后转投 SSE hub）。写 Stream 只在有人
**消费**它做回放时才有意义；当前没有这样的读者，写进去只会变成一份没人读的腐烂数据。
需要回放时再加一条流，届时由消费方决定保留窗口。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# ── 事件名（线上契约，勿随意改名）──
EV_JOB_STARTED = "job.started"
EV_JOB_SETTLED = "job.settled"

# ── status 取值（与前端徽标一致）──
#: 一次尝试正在执行。
ST_RUNNING = "running"
#: 执行成功且完成转移被接受。
ST_COMPLETED = "completed"
#: 执行抛错（无论随后是重投还是进 DLQ）。
ST_FAILED = "failed"
#: "现在不行，等一会儿再来"（会话锁/上游不可达）：不是失败，只是本次尝试收口。
ST_DEFERRED = "deferred"
#: 没跑就被丢弃（deadline 已过 / 重领次数耗尽）。
ST_DISCARDED = "discarded"

#: 通道名 —— **跨语言契约**：Go 侧 ``internal/api/job_events_relay_g13.go`` 订阅同一个
#: 字面量。改动它等于让实时推送静默失效（事件还在发，只是没人收），
#: 因此 ``tests/test_queue_job_events_g13.py`` 对两侧都做了断言。
JOB_EVENTS_CHANNEL_NAME = "job:events"

#: ``error`` 字段的截断长度。错误正文会一路送到浏览器；"够定性"即可，
#: 全文留在 DLQ 与日志里。
MAX_ERROR_CHARS = 500


def job_events_channel() -> str:
    """带上 ``REDIS_KEY_PREFIX`` 的实际通道名。"""
    return rkey(JOB_EVENTS_CHANNEL_NAME)


def _redact_export(text: str) -> str:
    """出口脱敏（H62）：事件会流向浏览器与导出，密钥不该跟着走。

    脱敏失败**不拦事件** —— 把安全问题换成可用性问题更糟（与 ``event_sink`` 同款取向）。
    """
    if not text:
        return text
    try:
        from app.subagent.redact import redact_text

        return redact_text(text)[0]
    except Exception:  # noqa: BLE001
        return text


def resolve_job_identity(payload_raw: str, task_id: str) -> tuple[str, str]:
    """从任务载荷取 ``(job_id, session_id)``。

    载荷解析失败时**仍然**返回 ``task_id``：事件恰恰在最需要被看见的时候（载荷坏了、
    任务即将失败）不能因为解析不出来而消失。``session_id`` 取不到就是空字符串，
    由 relay 按"无处可投"自行丢弃 —— 这个判断留给知道路由规则的那一层。
    """
    try:
        payload = json.loads(payload_raw or "{}")
    except (TypeError, ValueError):
        return task_id, ""
    if not isinstance(payload, dict):
        return task_id, ""
    job_id = str(payload.get("job_id") or task_id or "")
    session_id = str(payload.get("session_id") or "")
    return job_id, session_id


def job_event_payload(
    *,
    event: str,
    job_id: str,
    task_type: str,
    status: str,
    session_id: str = "",
    tenant_id: int = 0,
    error: str = "",
    ts: float | None = None,
) -> dict[str, Any]:
    """拼一条作业事件。

    字段**总是全部出现**（空值给空串而不是省略键）：前端按"有没有这个字段"分支判断
    很容易把"没发生"与"这一版引擎还没上线"混起来；固定形状让两侧契约可以逐字段对。
    """
    return {
        "type": event,
        "job_id": job_id,
        "task_type": task_type,
        "status": status,
        "session_id": session_id,
        "error": _redact_export(error)[:MAX_ERROR_CHARS] if error else "",
        "ts": time.time() if ts is None else float(ts),
        "tenant_id": int(tenant_id or 0),
    }


def _count_failed(event: str) -> None:
    """投递失败计数。指标模块不可用时静默跳过 —— 观测不该是单点。"""
    try:
        from app.observability.metrics import QUEUE_JOB_EVENT_FAILED

        QUEUE_JOB_EVENT_FAILED.labels(event=event).inc()
    except Exception:  # noqa: BLE001
        return


async def emit_job_event(
    redis: Any,
    *,
    event: str,
    job_id: str,
    task_type: str,
    status: str,
    session_id: str = "",
    tenant_id: int = 0,
    error: str = "",
    ts: float | None = None,
) -> dict[str, Any] | None:
    """发一条作业事件；**绝不抛出**。

    Returns:
        成功时返回已投递的 payload（测试与日志都靠它断言"网关会收到什么"）；
        投递失败时返回 ``None``（已经记了告警与指标）。
    """
    payload = job_event_payload(
        event=event,
        job_id=job_id,
        task_type=task_type,
        status=status,
        session_id=session_id,
        tenant_id=tenant_id,
        error=error,
        ts=ts,
    )
    try:
        await redis.publish(
            job_events_channel(), json.dumps(payload, ensure_ascii=False)
        )
    except Exception as exc:  # noqa: BLE001 - 旁路：投递失败只记日志
        _count_failed(event)
        logger.warning(
            "job event dropped (type=%s job=%s task=%s): %s",
            event,
            job_id,
            task_type,
            str(exc)[:160],
        )
        return None
    return payload
