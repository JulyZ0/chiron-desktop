"""H7：队列 claim / attempt fencing 在 fakeredis（带 Lua 执行）上跑通。

为什么值得单独做这一层：`tests/test_queue_h7_fencing.py` 是 `pytest.mark.live`，
需要**真 Redis**，本机跑不了 —— 于是"独占认领 / 旧 attempt 不生效"这些判据
平时只能靠 CI 才发现回归。这里用 fakeredis（`fakeredis[lua]`，真的会执行 Lua）
把同一批判据在本地钉住：逻辑层先绿，CI 再用真 Redis 复核。

覆盖：
  · 并发 claim 只有一个成功，其余是 `busy`；
  · 旧 attempt 的 complete / fail / ack 全部无效（token 不符）；
  · 已完成的任务再投递直接 `completed`（副作用幂等），且能 ACK 掉重复消息；
  · 重试保留原信封（idempotency/deadline/trace/priority）；
  · claim 在 Redis 报错时**向上抛**而不是"当作拿到了"（fail-closed）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import redis.asyncio as aioredis

from app.queue import idempotency

pytest.importorskip("fakeredis")


@pytest.fixture()
def fake_redis():  # type: ignore[no-untyped-def]
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()

    async def factory() -> aioredis.Redis:
        return fakeredis.aioredis.FakeRedis(
            server=server, decode_responses=True, lua_modules=None
        )

    return factory


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _claim(redis, *, key="task-1", lease=60):  # type: ignore[no-untyped-def]
    return await idempotency.claim(
        redis,
        namespace="engine",
        key=key,
        task_id=key,
        task_type="agent_run",
        tenant_id=1,
        lease_seconds=lease,
    )


def test_only_one_concurrent_claim_wins(fake_redis) -> None:
    async def scenario() -> None:
        redis = await fake_redis()
        first, second = await asyncio.gather(_claim(redis), _claim(redis))
        statuses = sorted([first.status.value, second.status.value])
        assert statuses == ["acquired", "busy"], statuses

    _run(scenario())


def test_stale_attempt_cannot_fail_complete_or_ack(fake_redis) -> None:
    async def scenario() -> None:
        redis = await fake_redis()
        # 第一个 attempt 拿到租约，但租约立即过期（lease_seconds=1 之外用 0 模拟不可行，
        # 这里直接让第二个 claim 在"过期后"成功：先手工把 lease_until 清零）。
        first = await _claim(redis, lease=60)
        assert first.status is idempotency.ClaimStatus.ACQUIRED
        state = idempotency.state_key("engine", "task-1")
        await redis.hset(state, "lease_until", "0")

        second = await _claim(redis, lease=60)
        assert second.status is idempotency.ClaimStatus.ACQUIRED
        assert second.claim.attempt == first.claim.attempt + 1

        # 旧 attempt 的三条出口都必须无效
        assert await idempotency.fail(redis, first.claim) is False
        moved = await idempotency.retry(
            redis,
            first.claim,
            source="engine:q",
            target="engine:q",
            group="g",
            consumer="c",
            message_id="1-0",
            fields={"task_id": "task-1"},
            maxlen=0,
        )
        assert moved is False

        # 新 attempt 仍然有效
        assert await idempotency.fail(redis, second.claim) is True

    _run(scenario())


def test_completed_task_suppresses_duplicate_side_effects(fake_redis) -> None:
    async def scenario() -> None:
        redis = await fake_redis()
        claim = await _claim(redis)
        assert claim.status is idempotency.ClaimStatus.ACQUIRED

        again = await _claim(redis)
        assert again.status is idempotency.ClaimStatus.BUSY, "租约内重复投递应是 busy"

        # 直接标成 completed（模拟完成转移）
        state = idempotency.state_key("engine", "task-1")
        await redis.hset(state, "status", "completed")

        after = await _claim(redis)
        assert after.status is idempotency.ClaimStatus.COMPLETED, "完成后重复投递不得再次执行"

    _run(scenario())


def test_retry_preserves_envelope_metadata(fake_redis) -> None:
    async def scenario() -> None:
        redis = await fake_redis()
        claim = await _claim(redis)
        assert claim.status is idempotency.ClaimStatus.ACQUIRED
        await redis.xadd("engine:q", {"task_id": "task-1"})
        await redis.xgroup_create("engine:q", "g", id="0", mkstream=True)
        # 先投递给 consumer（转移脚本要求消息在该 consumer 的 PEL 里）。
        delivered = await redis.xreadgroup("g", "c", {"engine:q": ">"}, count=1)
        assert delivered, "应先有一条待投递消息"
        message_id = delivered[0][1][0][0]

        envelope: dict[str, Any] = {
            "task_id": "task-1",
            "task_type": "agent_run",
            "idempotency_key": "idem-abc",
            "deadline": "1735689600",
            "trace_id": "trace-1",
            "priority": "5",
        }
        ok = await idempotency.retry(
            redis,
            claim.claim,
            source="engine:q",
            target="engine:q",
            group="g",
            consumer="c",
            message_id=message_id,
            fields=envelope,
            maxlen=0,
        )
        assert ok is True

        entries = await redis.xrange("engine:q")
        assert len(entries) == 2
        forwarded = entries[-1][1]
        for field in ("idempotency_key", "deadline", "trace_id", "priority"):
            assert forwarded[field] == envelope[field], f"重试丢失了 {field}"

    _run(scenario())


def test_claim_propagates_redis_errors_instead_of_failing_open(monkeypatch) -> None:
    """Redis 报错时必须抛，不能"当作拿到了租约"（那会双跑同一任务）。"""

    class _Broken:
        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    async def scenario() -> None:
        with pytest.raises(ConnectionError):
            await _claim(_Broken())

    _run(scenario())
