"""G16：任务级心跳 —— 让"卡住"与"在跑"可区分。

修前会怎样：worker 只在 claim 时写一次 ``lease_until``，之后靠续租；续租失败只记 debug
日志，任务**内部**的停顿没有任何痕迹。于是上层只有两种判断："PEL 里还有它"（在跑？）
与"PEL 里没了"（结束了？），第三种状态"还占着但不动了"看不见 —— 只能等腰斩式的
lease 超时（默认 10 分钟）被别的实例重领，而重领之后也没有证据说明上一次是卡住的。

这里钉住三件事：

1. 心跳随处理推进而更新（假时钟 + 注入的短间隔，不靠真的等 30 秒）；
2. 超过 N 倍间隔未更新 → 判定 stale，并记 ``queue_job_stale_total``；
3. 正常完成的任务**不留** stale 记录（留着它，这个信号会被历史噪声淹没）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.observability.metrics import QUEUE_JOB_STALE
from app.queue import heartbeat, idempotency
from app.queue.streams import ENGINE_QUEUE
from app.queue.worker import QueueWorker


class FakeRedis:
    """只实现心跳用到的命令（外加 G13 事件的 publish 空实现）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttl_ms: dict[str, int] = {}
        self.fail_hset = False

    async def publish(self, channel: str, payload: str) -> int:  # noqa: ARG002
        return 1

    async def hset(self, key: str, mapping: Any = None, **kwargs: Any) -> int:
        if self.fail_hset:
            raise ConnectionError("redis down")
        data = {k: str(v) for k, v in (mapping or kwargs).items()}
        self.hashes.setdefault(key, {}).update(data)
        return len(data)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def pexpire(self, key: str, ms: int) -> int:
        self.ttl_ms[key] = int(ms)
        return 1

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.hashes.pop(key, None) is not None else 0
            self.ttl_ms.pop(key, None)
        return removed

    async def scan_iter(self, match: str | None = None, count: int | None = None):  # noqa: ARG002
        prefix = (match or "").rstrip("*")
        for key in list(self.hashes):
            if key.startswith(prefix):
                yield key


class FakeClock:
    """可推进的墙钟（心跳按秒比较，不需要单调性，所以替的是 ``time.time``）。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _mock_fenced_transitions(monkeypatch):
    claim = idempotency.Attempt("engine", "tool_job:t1", "state", "token", 1)
    monkeypatch.setattr(
        idempotency,
        "claim",
        AsyncMock(return_value=idempotency.ClaimDecision(idempotency.ClaimStatus.ACQUIRED, claim)),
    )
    monkeypatch.setattr(idempotency, "complete", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "ack_completed", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "retry", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "defer", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "dead_letter", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "forward", AsyncMock(return_value=True))


def fields(
    task_type: str = "tool_job",
    task_id: str = "t1",
    payload: str = '{"job_id": "j1", "session_id": "s1"}',
) -> dict[bytes, bytes]:
    return {
        b"task_type": task_type.encode(),
        b"task_id": task_id.encode(),
        b"payload": payload.encode(),
        b"retry_count": b"0",
    }


def make_worker(redis: FakeRedis, *, interval: float = 0.01) -> QueueWorker:
    return QueueWorker(redis=redis, queue=ENGINE_QUEUE, job_heartbeat_interval=interval)


class TestHeartbeatWrite:
    async def test_heartbeat_advances_while_the_task_runs(self) -> None:
        """心跳是**周期**的：处理期间每隔一个间隔就更新一次，而不是只在进门时写一条。"""
        clock = FakeClock()
        redis = FakeRedis()
        hb = heartbeat.JobHeartbeat(redis, job_id="j1", interval=10.0, clock=clock)

        await hb.attach(task_type="tool_job")
        first = await heartbeat.read_heartbeat(redis, "j1")
        assert first is not None
        assert first.task_type == "tool_job"
        assert first.beats == 1

        clock.advance(10.0)
        await hb.beat()

        second = await heartbeat.read_heartbeat(redis, "j1")
        assert second is not None
        assert second.heartbeat_at > first.heartbeat_at
        assert second.beats == first.beats + 1
        assert second.is_stale(now=clock(), interval=10.0, multiplier=3.0) is False
        await hb.detach()

    async def test_background_loop_keeps_beating(self) -> None:
        """不靠手动 beat：attach 之后就有一个后台循环在持续刷新。"""
        redis = FakeRedis()
        hb = heartbeat.JobHeartbeat(redis, job_id="j2", interval=0.01, clock=time.time)
        await hb.attach(task_type="tool_job")
        try:
            deadline = time.monotonic() + 2.0
            info = await heartbeat.read_heartbeat(redis, "j2")
            while info is not None and info.beats < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                info = await heartbeat.read_heartbeat(redis, "j2")
            assert info is not None and info.beats >= 3, "后台心跳循环没有在推进"
        finally:
            await hb.detach()

    async def test_ttl_outlives_the_stale_threshold(self) -> None:
        """记录的 TTL 必须**明显大于**判定阈值。

        否则记录先过期，"卡住"就退化成"记录不存在" —— 与"正常完成"再也分不开。
        """
        redis = FakeRedis()
        hb = heartbeat.JobHeartbeat(redis, job_id="j1", interval=10.0, clock=FakeClock())
        await hb.attach(task_type="tool_job")
        try:
            threshold = heartbeat.stale_threshold(interval=10.0, multiplier=3.0)
            ttl_seconds = redis.ttl_ms[heartbeat.heartbeat_key("j1")] / 1000
            assert ttl_seconds > threshold
        finally:
            await hb.detach()

    async def test_write_failure_is_swallowed(self) -> None:
        """心跳是旁路：Redis 写不进去只记日志，绝不把任务带崩。"""
        redis = FakeRedis()
        redis.fail_hset = True
        hb = heartbeat.JobHeartbeat(redis, job_id="j1", interval=10.0, clock=FakeClock())
        await hb.attach(task_type="tool_job")  # 不抛
        await hb.beat()  # 不抛
        await hb.detach()
        assert await heartbeat.read_heartbeat(redis, "j1") is None


class TestStaleDetection:
    async def test_heartbeat_older_than_n_intervals_is_stale(self) -> None:
        clock = FakeClock()
        redis = FakeRedis()
        hb = heartbeat.JobHeartbeat(redis, job_id="j1", interval=10.0, clock=clock)
        await hb.attach(task_type="tool_job")
        try:
            clock.advance(29.0)
            info = await heartbeat.read_heartbeat(redis, "j1")
            assert info is not None
            assert info.is_stale(now=clock(), interval=10.0, multiplier=3.0) is False

            # 再挪 2 秒 → 越过 3×10s 阈值
            clock.advance(2.0)
            info = await heartbeat.read_heartbeat(redis, "j1")
            assert info is not None
            assert info.is_stale(now=clock(), interval=10.0, multiplier=3.0) is True
        finally:
            await hb.detach()

    async def test_scan_reports_only_the_stuck_job_and_counts_it(self) -> None:
        """只读查询：卡住的进列表、正常的不进；每条 stale 记一次指标。"""
        clock = FakeClock()
        redis = FakeRedis()
        fresh = heartbeat.JobHeartbeat(redis, job_id="fresh", interval=10.0, clock=clock)
        stuck = heartbeat.JobHeartbeat(redis, job_id="stuck", interval=10.0, clock=clock)
        await fresh.attach(task_type="tool_job")
        await stuck.attach(task_type="tool_job")
        try:
            # stuck 落后 60s（> 3×10s），fresh 仍在跳
            redis.hashes[heartbeat.heartbeat_key("stuck")]["heartbeat_at"] = (
                f"{clock() - 60.0:.3f}"
            )
            before = QUEUE_JOB_STALE.labels(task_type="tool_job")._value.get()

            stale = await heartbeat.scan_stale_heartbeats(
                redis, now=clock(), interval=10.0, multiplier=3.0
            )

            assert [info.job_id for info in stale] == ["stuck"]
            after = QUEUE_JOB_STALE.labels(task_type="tool_job")._value.get()
            assert after == before + 1
        finally:
            await fresh.detach()
            await stuck.detach()

    async def test_unknown_job_has_no_heartbeat(self) -> None:
        redis = FakeRedis()
        assert await heartbeat.read_heartbeat(redis, "nope") is None
        assert await heartbeat.read_heartbeat(redis, "") is None

    async def test_scan_is_empty_when_redis_cannot_be_scanned(self) -> None:
        """读不到 ≠ 卡住：扫描失败返回空列表，交给告警看"心跳整体消失"这一别的信号。"""

        class _Broken(FakeRedis):
            async def scan_iter(self, match: str | None = None, count: int | None = None):  # noqa: ARG002
                raise ConnectionError("redis down")
                yield  # pragma: no cover - 让函数保持异步生成器语义

        assert await heartbeat.scan_stale_heartbeats(_Broken(), interval=10.0) == []


class TestWorkerWiring:
    async def test_worker_keeps_the_heartbeat_alive_while_dispatching(self) -> None:
        """处理期间必须有心跳记录（否则上层看不出"在跑"）。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        seen: list[Any] = []

        async def dispatch(task_type: str, payload: dict[str, Any], tenant_id: int = 0) -> None:
            seen.append(await heartbeat.read_heartbeat(redis, "j1"))

        worker._dispatch = AsyncMock(side_effect=dispatch)

        await worker._process_message("1-0", fields())

        assert seen and seen[0] is not None, "处理期间没有心跳记录"
        assert seen[0].task_type == "tool_job"
        assert seen[0].beats >= 1

    async def test_finished_task_leaves_no_stale_record(self) -> None:
        """正常完成 → 记录被删掉，扫描结果为空。

        留着它，下一次扫描就会把它算成"卡住" —— 一个永远在响的告警等于没有告警。
        """
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message("1-0", fields())

        assert heartbeat.heartbeat_key("j1") not in redis.hashes
        assert await heartbeat.scan_stale_heartbeats(redis, interval=0.01) == []

    async def test_failed_task_also_clears_its_heartbeat(self) -> None:
        """失败同样收口：卡住信号只留给"真的还占着但不动了"的任务。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        await worker._process_message("1-0", fields())

        assert heartbeat.heartbeat_key("j1") not in redis.hashes
        assert await heartbeat.scan_stale_heartbeats(redis, interval=0.01) == []

    async def test_heartbeat_failure_does_not_break_the_task(self) -> None:
        """Redis 写不进去时任务照常完成（心跳是观测，不是流程）。"""
        redis = FakeRedis()
        redis.fail_hset = True
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message("1-0", fields())

        worker._dispatch.assert_awaited_once()
        idempotency.complete.assert_awaited_once()
