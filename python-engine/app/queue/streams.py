"""队列规格 —— stream / 消费组 / DLQ / 延迟重试键。

**为什么需要这一层。** ``kb_index`` 的*执行者*与其它任务不同：它需要 PageIndex SDK，
而引擎镜像不装它（S4：索引由独立的 pageindex-worker 消费，见 docker-compose 的
``pageindex-worker`` 服务）。而 Redis Stream 是**单消费组**语义 —— 同一组内任意
consumer 都能领到任意消息，所以"让引擎 worker 别碰 kb_index"**不能靠过滤**（它已经
把消息从队列里拿走了），只能换一条 stream。

DLQ 与延迟重试键也**按队列分开**：否则 kb 任务的重试会落回引擎 worker 手里，
变成"怎么投都修不好"的死信。

滚动升级兼容：旧版 producer 可能仍把 ``kb_index`` 投到主 stream（它还不知道新队列）。
引擎 worker 遇到这种"不属于自己"的任务时**转发到目标队列并 ACK**（见 worker 的
``_dispatch``），而不是失败进 DLQ —— 否则一次滚动升级就会把期间的索引任务全打进死信。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from app.observability.metrics import QUEUE_RETENTION_DEGRADED
from app.redis_keys import rkey

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueSpec:
    """一条队列的完整键集合。``key`` 是队列身份，用于日志、指标和幂等命名空间。"""

    key: str
    stream: str
    group: str
    dlq: str
    delayed: str
    maxlen: int = 100000
    dlq_maxlen: int = 10000


#: 引擎任务（对话/记忆/工作流/工具作业…）—— 由 python-engine 内的 worker 消费。
ENGINE_QUEUE = QueueSpec(
    key="engine",
    stream=rkey("engine:tasks"),
    group="engine-workers",
    dlq=rkey("engine:tasks:dlq"),
    delayed=rkey("engine:tasks:delayed"),
)

#: 知识库索引 —— 由 pageindex-worker 消费（它装着 PageIndex SDK；引擎镜像不装）。
KB_QUEUE = QueueSpec(
    key="kb",
    stream=rkey("engine:tasks:kb"),
    group="kb-workers",
    dlq=rkey("engine:tasks:kb:dlq"),
    delayed=rkey("engine:tasks:kb:delayed"),
)

#: task_type → 队列。**新增"需要独立依赖/独立执行者"的任务类型时，在这里登记。**
#: 未登记的类型一律走引擎队列（保持既有行为）。
TASK_QUEUE: dict[str, QueueSpec] = {
    "kb_index": KB_QUEUE,
}

ALL_QUEUES: tuple[QueueSpec, ...] = (ENGINE_QUEUE, KB_QUEUE)

#: 兼容旧引用（这两个名字历史上指"主队列"）。新代码请直接用 ``QueueSpec`` ——
#: "任务该投哪条队列"只应该有一个答案（``queue_for_task``）。
TASK_STREAM = ENGINE_QUEUE.stream
DLQ_STREAM = ENGINE_QUEUE.dlq
GROUP_NAME = ENGINE_QUEUE.group


def queue_for_task(task_type: str) -> QueueSpec:
    """该任务类型该投到哪条队列。"""
    return TASK_QUEUE.get(task_type, ENGINE_QUEUE)


def is_foreign(task_type: str, current: QueueSpec) -> bool:
    """任务是否属于**别的**队列（滚动升级期间出现在本队列里 = 需要转发）。"""
    return queue_for_task(task_type).key != current.key


_XADD_ACK_LUA = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[3], ARGV[3], 1)
if #pending == 0 or pending[1][2] ~= ARGV[2] then
    return false
end
-- H34：源流里还有别人的未 ACK 条目就不裁剪（那可能是最旧的条目）。
local summary = redis.call('XPENDING', KEYS[1], ARGV[1])
local others = tonumber(summary[1] or 0) or 0
local added
if others > 1 then
    added = redis.call('XADD', KEYS[2], '*', unpack(ARGV, 5))
else
    added = redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[4], '*', unpack(ARGV, 5))
end
redis.call('XACK', KEYS[1], ARGV[1], ARGV[3])
return added
"""

_PROMOTE_LUA = """
local message = cjson.decode(ARGV[2])
local fields = {'MAXLEN', '~', ARGV[1], '*'}
for key, value in pairs(message) do
    fields[#fields + 1] = key
    fields[#fields + 1] = tostring(value)
end
local kind = redis.call('TYPE', KEYS[2])['ok']
if kind ~= 'none' and kind ~= 'stream' then
    return redis.error_reply('WRONGTYPE delayed promotion target is not a stream')
end
if redis.call('ZREM', KEYS[1], ARGV[2]) == 0 then
    return false
end
return redis.call('XADD', KEYS[2], unpack(fields))
"""


def _field_args(fields: dict[Any, Any]) -> list[str]:
    return [
        str(part)
        for key, value in fields.items()
        for part in (
            key.decode() if isinstance(key, bytes) else key,
            value.decode() if isinstance(value, bytes) else value,
        )
    ]


@dataclass(frozen=True)
class AppendSpan:
    """一次入队的结果：id + 是否发生了裁剪 / 是否因保护 PEL 而降级。"""

    message_id: str
    trimmed: bool
    degraded: bool


async def _pending_count(redis: aioredis.Redis, stream: str, group: str) -> int | None:
    """消费组里未 ACK 的条数；读不到（NOGROUP/Redis 故障）返回 None。

    ``None`` 在调用方那里按"可能有未 ACK 项"处理 —— 宁可少裁剪，也不赌一把删掉任务。
    """
    try:
        summary = await redis.xpending(stream, group)
    except Exception as exc:  # noqa: BLE001 - 读不到就不裁剪
        logger.debug("queue retention: XPENDING failed for %s/%s: %s", stream, group, exc)
        return None
    if isinstance(summary, bool):  # bool 是 int 的子类，先排除
        return None
    if isinstance(summary, (int, str)):
        try:
            return int(summary)
        except (TypeError, ValueError):
            return None
    if isinstance(summary, dict):
        try:
            return int(summary.get("pending", 0) or 0)
        except (TypeError, ValueError):
            return None
    # 某些客户端/版本返回 `[count, min, max, consumers]`
    if isinstance(summary, (list, tuple)) and summary:
        try:
            return int(summary[0] or 0)
        except (TypeError, ValueError):
            return None
    return 0


async def append_task(
    redis: aioredis.Redis,
    *,
    stream: str,
    group: str,
    fields: dict[Any, Any],
    maxlen: int,
) -> AppendSpan:
    """入队，且**绝不为了裁剪而删掉未 ACK 的任务**（PRD 9.7 H34）。

    Redis 的 ``XADD MAXLEN`` 是流级修剪：删的是最旧的条目，它并不知道那条任务还在
    某个消费组的 PEL 里等着重试。一旦积压顶到上限，一次普通入队就能把"正在重试的
    任务"从流里抹掉 —— 且不进 DLQ、不留日志。

    策略（保守优先）：

    1. 组里**还有未 ACK 的条目** → 完全不裁剪（``XADD`` 不带 MAXLEN）。队列会短暂
       超出上限，但既没有任务被删，也能通过 :data:`QUEUE_RETENTION_DEGRADED` 指标
       与告警看到"该扩容/该查积压了"。
    2. ``XPENDING`` 读不到（NOGROUP / Redis 抖动）→ 同样不裁剪（同上，宁可超限）。
    3. 没有未 ACK 条目 → 用**近似裁剪** ``MAXLEN ~ n``：允许长度在 n 附近浮动，
       换取 O(1) 的追加成本，并且不会为了"精确 n"去逐条评估删除。
    """
    if maxlen is None or maxlen <= 0:
        message_id = await redis.xadd(stream, fields)
        return AppendSpan(str(message_id), trimmed=False, degraded=False)

    pending = await _pending_count(redis, stream, group)
    if pending is None or pending > 0:
        QUEUE_RETENTION_DEGRADED.labels(stream=stream).inc()
        logger.warning(
            "queue retention degraded: stream=%s group=%s pending=%s maxlen=%d "
            "-> append without MAXLEN to protect un-ACKed tasks",
            stream,
            group,
            "unknown" if pending is None else pending,
            maxlen,
        )
        message_id = await redis.xadd(stream, fields)
        return AppendSpan(str(message_id), trimmed=False, degraded=True)

    # 近似裁剪：`~` 让 Redis 在"块边界"上停止删除，不会为了精确长度做额外工作；
    # 具体长度由 Redis 决定，但成本是 O(1) 摊还。
    message_id = await redis.xadd(stream, fields, maxlen=maxlen, approximate=True)
    return AppendSpan(str(message_id), trimmed=True, degraded=False)


async def atomic_xadd_ack(
    redis: aioredis.Redis,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    fields: dict[Any, Any],
    maxlen: int,
) -> str | None:
    """Append a copy and ACK its owned PEL entry in one Redis script."""
    result = await redis.eval(
        _XADD_ACK_LUA,
        2,
        source,
        target,
        group,
        consumer,
        message_id,
        maxlen,
        *_field_args(fields),
    )
    if not result:
        return None
    return result.decode() if isinstance(result, bytes) else str(result)


async def atomic_promote_delayed(
    redis: aioredis.Redis,
    *,
    delayed: str,
    target: str,
    member: str,
    maxlen: int,
) -> str | None:
    """Remove a delayed member and append it without a crash-loss window."""
    result = await redis.eval(_PROMOTE_LUA, 2, delayed, target, maxlen, member)
    if not result:
        return None
    return result.decode() if isinstance(result, bytes) else str(result)
