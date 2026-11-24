# ruff: noqa: N803 - 假 Redis 客户端的形参必须与 redis-py 的关键字参数同名
"""H34：队列保留策略不得删除未完成任务（假客户端，不连真 Redis）。

PRD 9.7 H34 验收：

- 使用近似裁剪且保留未处理项，或改为独立保留/归档策略；
- 满载时拒绝或降级而非静默丢任务；
- DLQ 与指标可核对。

这里断言的是**入队时传给 Redis 的参数**：组里还有未 ACK 条目时，``XADD`` 不允许
带 ``MAXLEN``（否则 Redis 会按流长删掉最旧条目，而那些条目可能仍在 PEL 里）；没有
未 ACK 条目时才使用 ``MAXLEN ~ n`` 近似裁剪。Lua 脚本里的同一策略用源码断言守护。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.observability.metrics import QUEUE_DLQ_TOTAL, QUEUE_RETENTION_DEGRADED
from app.queue import idempotency, streams
from app.queue.producer import QueueProducer
from app.queue.streams import append_task

_STREAM = "chiron:engine:tasks"
_GROUP = "engine-workers"


class FakeRedis:
    """只实现入队路径用到的命令，记录每次调用的参数。"""

    def __init__(
        self,
        *,
        pending: int | list[Any] = 0,
        pending_error: BaseException | None = None,
    ) -> None:
        self.pending = pending
        self.pending_error = pending_error
        self.xadd_calls: list[tuple[str, dict[Any, Any], dict[str, Any]]] = []

    async def xpending(self, stream: str, group: str, *args: Any) -> Any:
        if self.pending_error is not None:
            raise self.pending_error
        if args:  # 明细查询（range）不在本路径里使用
            raise AssertionError("unexpected XPENDING range query")
        return self.pending

    async def xadd(self, stream: str, fields: dict[Any, Any], **kwargs: Any) -> str:
        self.xadd_calls.append((stream, fields, kwargs))
        return f"{len(self.xadd_calls)}-0"


def _degraded() -> float:
    return QUEUE_RETENTION_DEGRADED.labels(stream=_STREAM)._value.get()


async def test_pending_entries_block_trimming():
    redis = FakeRedis(pending=3)
    before = _degraded()

    span = await append_task(
        redis,  # type: ignore[arg-type]
        stream=_STREAM,
        group=_GROUP,
        fields={"a": "1"},
        maxlen=100000,
    )

    assert span.degraded is True
    assert span.trimmed is False
    _stream, _fields, kwargs = redis.xadd_calls[0]
    assert "maxlen" not in kwargs, "组内有未 ACK 条目时不得带 MAXLEN（会删掉 PEL 里的任务）"
    assert _degraded() == before + 1


async def test_no_pending_uses_approximate_trimming():
    redis = FakeRedis(pending=0)
    before = _degraded()

    span = await append_task(
        redis,  # type: ignore[arg-type]
        stream=_STREAM,
        group=_GROUP,
        fields={"a": "1"},
        maxlen=100000,
    )

    assert span.trimmed is True
    assert span.degraded is False
    _stream, _fields, kwargs = redis.xadd_calls[0]
    assert kwargs["maxlen"] == 100000
    assert kwargs["approximate"] is True, "必须近似裁剪（MAXLEN ~），不能逐条精确删除"
    assert _degraded() == before


async def test_xpending_failure_degrades_instead_of_guessing():
    redis = FakeRedis(pending_error=RuntimeError("NOGROUP no such group"))
    before = _degraded()

    span = await append_task(
        redis,  # type: ignore[arg-type]
        stream=_STREAM,
        group=_GROUP,
        fields={"a": "1"},
        maxlen=100,
    )

    assert span.degraded is True
    assert "maxlen" not in redis.xadd_calls[0][2]
    assert _degraded() == before + 1


async def test_pending_summary_as_list_is_understood():
    redis = FakeRedis(pending=[2, "1-0", "1-0", [["c1", 2]]])
    span = await append_task(
        redis,  # type: ignore[arg-type]
        stream=_STREAM,
        group=_GROUP,
        fields={"a": "1"},
        maxlen=100,
    )
    assert span.degraded is True
    assert "maxlen" not in redis.xadd_calls[0][2]


async def test_zero_maxlen_never_trims():
    redis = FakeRedis(pending=0)
    span = await append_task(
        redis,  # type: ignore[arg-type]
        stream=_STREAM,
        group=_GROUP,
        fields={"a": "1"},
        maxlen=0,
    )
    assert span.trimmed is False
    assert span.degraded is False
    assert "maxlen" not in redis.xadd_calls[0][2]


async def test_producer_enqueue_goes_through_retention_guard(monkeypatch):
    redis = FakeRedis(pending=1)
    producer = QueueProducer(redis)  # type: ignore[arg-type]

    task_id = await producer.enqueue(
        "tool_job",
        tenant_id=7,
        payload={"job_id": "j-1"},
        priority=5,
        deadline_seconds=120,
        idempotency_key="job-1",
    )

    assert task_id
    assert len(redis.xadd_calls) == 1
    stream, fields, kwargs = redis.xadd_calls[0]
    assert stream.endswith("engine:tasks")
    assert "maxlen" not in kwargs
    # 元数据齐备（H7：retry 保留 idempotency key/deadline/trace/priority）
    assert fields["idempotency_key"] == "job-1"
    assert fields["priority"] == "5"
    assert fields["deadline"]
    assert fields["trace_id"] is not None


def test_lua_scripts_never_use_exact_maxlen():
    for path, name in (
        (Path(streams.__file__), "streams.py"),
        (Path(idempotency.__file__), "idempotency.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "'MAXLEN', ARGV" not in source, f"{name} 仍在用精确 MAXLEN"
        assert "'MAXLEN', '~'" in source, f"{name} 缺少近似裁剪"


def test_lua_requeue_skips_trimming_while_other_entries_are_pending():
    """重投/转发脚本里：还有别人的未 ACK 条目时不带 MAXLEN。"""
    for path in (Path(streams.__file__), Path(idempotency.__file__)):
        source = path.read_text(encoding="utf-8")
        assert "XPENDING" in source
        assert "others > 1" in source, f"{path.name} 未按 PEL 余量决定是否裁剪"


def test_dlq_metric_is_checkable():
    """DLQ 计数可按 task_type 核对（H34 的"指标可核对"）。"""
    before = QUEUE_DLQ_TOTAL.labels(task_type="tool_job")._value.get()
    QUEUE_DLQ_TOTAL.labels(task_type="tool_job").inc()
    assert QUEUE_DLQ_TOTAL.labels(task_type="tool_job")._value.get() == before + 1
