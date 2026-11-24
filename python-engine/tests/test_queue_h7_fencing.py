"""H7 queue ownership tests against a real Redis server."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

from app.queue import idempotency
from app.queue.streams import QueueSpec, atomic_promote_delayed, atomic_xadd_ack
from app.queue.worker import QueueWorker, RetryLaterError
from app.redis_keys import rkey

pytestmark = pytest.mark.live

_REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL")
    or os.getenv("REDIS_URL")
    or "redis://127.0.0.1:6379/0"
)


@dataclass
class Harness:
    redis: aioredis.Redis
    queue: QueueSpec
    namespace: str
    keys: list[str]

    async def deliver(
        self, fields: dict[str, str], consumer: str = "consumer-a"
    ) -> tuple[str, dict[Any, Any]]:
        message_id = await self.redis.xadd(self.queue.stream, fields)
        result = await self.redis.xreadgroup(
            self.queue.group, consumer, {self.queue.stream: ">"}, count=1
        )
        assert result
        return message_id, result[0][1][0][1]


@pytest.fixture
async def h7(monkeypatch):
    client = aioredis.from_url(
        _REDIS_URL,
        decode_responses=False,
        socket_connect_timeout=1,
        socket_timeout=2,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        if (os.getenv("CHIRON_TEST_REDIS_URL") or os.getenv("REDIS_URL") or "").strip():
            pytest.fail(f"Redis was declared but is unavailable at {_REDIS_URL}: {exc}")
        pytest.skip(f"Redis is unavailable at {_REDIS_URL}")

    uid = uuid.uuid4().hex[:12]
    prefix = rkey(f"test:h7:{uid}")
    queue = QueueSpec(
        key=uid,
        stream=f"{prefix}:tasks",
        group=f"workers-{uid}",
        dlq=f"{prefix}:dlq",
        delayed=f"{prefix}:delayed",
    )
    keys = [queue.stream, queue.dlq, queue.delayed, f"{prefix}:bad-target"]
    from app.queue import streams

    monkeypatch.setitem(streams.TASK_QUEUE, "tool_job", queue)
    await client.xgroup_create(queue.stream, queue.group, id="0", mkstream=True)
    try:
        yield Harness(client, queue, uid, keys)
    finally:
        state_keys = [
            key async for key in client.scan_iter(match=rkey(f"queue:idempotency:{uid}:*"))
        ]
        await client.delete(*keys, *state_keys)
        await client.aclose()


def _fields(**overrides: str) -> dict[str, str]:
    fields = {
        "task_id": "task-1",
        "task_type": "tool_job",
        "tenant_id": "7",
        "payload": '{"job_id":"job-1"}',
        "retry_count": "0",
        "idempotency_key": "business-1",
        "deadline": "2030-01-01T00:00:00Z",
        "trace_id": "trace-1",
        "priority": "9",
        "created_at": "2026-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return fields


async def _claim(h7: Harness, consumer: str = "consumer-a", lease_seconds: int = 30):
    return await idempotency.claim(
        h7.redis,
        namespace=h7.namespace,
        key="business-1",
        task_id="task-1",
        task_type="tool_job",
        tenant_id=7,
        lease_seconds=lease_seconds,
    )


async def test_concurrent_claim_has_exactly_one_owner(h7: Harness):
    decisions = await asyncio.gather(
        *(_claim(h7, consumer=f"consumer-{index}") for index in range(24))
    )

    assert sum(item.status is idempotency.ClaimStatus.ACQUIRED for item in decisions) == 1
    assert all(
        item.status is idempotency.ClaimStatus.BUSY
        for item in decisions
        if item.status is not idempotency.ClaimStatus.ACQUIRED
    )
    winner = next(item.claim for item in decisions if item.claim is not None)
    assert winner is not None
    assert winner.attempt == 1
    assert await idempotency.fail(h7.redis, winner) is True

    retries = await asyncio.gather(
        *(_claim(h7, consumer=f"retry-{index}") for index in range(24))
    )
    retry_winner = next(item.claim for item in retries if item.claim is not None)
    assert retry_winner is not None
    assert retry_winner.attempt == 2
    assert sum(item.claim is not None for item in retries) == 1


async def test_stale_attempt_cannot_complete_fail_or_ack(h7: Harness):
    message_id, _ = await h7.deliver(_fields(), consumer="old-consumer")
    old = (await _claim(h7, consumer="old-consumer", lease_seconds=1)).claim
    assert old is not None

    await h7.redis.xclaim(
        h7.queue.stream, h7.queue.group, "new-consumer", 0, [message_id]
    )
    await asyncio.sleep(1.05)
    new = (await _claim(h7, consumer="new-consumer")).claim
    assert new is not None
    assert new.attempt == 2

    assert await idempotency.complete(
        h7.redis, old, h7.queue.stream, h7.queue.group, "old-consumer", message_id
    ) is False
    assert await idempotency.fail(h7.redis, old) is False
    assert await idempotency.ack(
        h7.redis, old, h7.queue.stream, h7.queue.group, "old-consumer", message_id
    ) is False
    assert await idempotency.retry(
        h7.redis,
        old,
        source=h7.queue.stream,
        target=h7.queue.stream,
        group=h7.queue.group,
        consumer="old-consumer",
        message_id=message_id,
        fields=_fields(),
        maxlen=100,
    ) is False
    assert await idempotency.defer(
        h7.redis,
        old,
        source=h7.queue.stream,
        target=h7.queue.delayed,
        group=h7.queue.group,
        consumer="old-consumer",
        message_id=message_id,
        member="{}",
        score=1,
    ) is False
    assert await idempotency.dead_letter(
        h7.redis,
        old,
        source=h7.queue.stream,
        target=h7.queue.dlq,
        group=h7.queue.group,
        consumer="old-consumer",
        message_id=message_id,
        fields=_fields(),
        maxlen=100,
    ) is False
    assert await h7.redis.xlen(h7.queue.stream) == 1
    assert await h7.redis.zcard(h7.queue.delayed) == 0
    assert await h7.redis.xlen(h7.queue.dlq) == 0
    pending = await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, message_id, message_id, count=1
    )
    assert pending[0]["consumer"] == b"new-consumer"

    assert await idempotency.complete(
        h7.redis, new, h7.queue.stream, h7.queue.group, "new-consumer", message_id
    ) is True
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []
    duplicate = await _claim(h7)
    assert duplicate.status is idempotency.ClaimStatus.COMPLETED


async def test_duplicate_delivery_runs_side_effect_once(h7: Harness):
    fields = _fields()
    first_id, first_fields = await h7.deliver(fields, consumer="consumer-a")
    second_id, second_fields = await h7.deliver(fields, consumer="consumer-b")
    worker_a = QueueWorker(h7.redis, queue=h7.queue)
    worker_b = QueueWorker(h7.redis, queue=h7.queue)
    worker_a._consumer_name = "consumer-a"
    worker_b._consumer_name = "consumer-b"
    effects: list[str] = []

    async def dispatch(_task_type: str, _payload: dict[str, Any], _tenant_id: int = 0):
        await asyncio.sleep(0.05)
        effects.append("executed")

    worker_a._dispatch = AsyncMock(side_effect=dispatch)
    worker_b._dispatch = AsyncMock(side_effect=dispatch)
    await asyncio.gather(
        worker_a._process_message(first_id, first_fields),
        worker_b._process_message(second_id, second_fields),
    )
    assert effects == ["executed"]

    await worker_a._process_message(first_id, first_fields)
    await worker_b._process_message(second_id, second_fields)
    assert effects == ["executed"]
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []


async def test_stale_worker_failure_cannot_requeue_or_ack(h7: Harness, monkeypatch):
    monkeypatch.setattr("app.queue.worker.CLAIM_MIN_IDLE_SECS", 1)
    message_id, fields = await h7.deliver(_fields(), consumer="old-consumer")
    old_worker = QueueWorker(h7.redis, queue=h7.queue)
    new_worker = QueueWorker(h7.redis, queue=h7.queue)
    old_worker._consumer_name = "old-consumer"
    new_worker._consumer_name = "new-consumer"
    old_entered = asyncio.Event()
    new_entered = asyncio.Event()
    release_old = asyncio.Event()
    release_new = asyncio.Event()

    async def old_dispatch(*_args: Any):
        old_entered.set()
        await release_old.wait()
        raise RuntimeError("stale failure")

    async def new_dispatch(*_args: Any):
        new_entered.set()
        await release_new.wait()

    old_worker._dispatch = AsyncMock(side_effect=old_dispatch)
    new_worker._dispatch = AsyncMock(side_effect=new_dispatch)
    old_task = asyncio.create_task(old_worker._process_message(message_id, fields))
    await old_entered.wait()
    await h7.redis.xclaim(
        h7.queue.stream, h7.queue.group, "new-consumer", 0, [message_id]
    )
    await asyncio.sleep(1.05)
    new_task = asyncio.create_task(new_worker._process_message(message_id, fields))
    await new_entered.wait()

    release_old.set()
    await old_task
    assert await h7.redis.xlen(h7.queue.stream) == 1
    pending = await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, message_id, message_id, count=1
    )
    assert pending[0]["consumer"] == b"new-consumer"
    state_key = idempotency.state_key(h7.namespace, "business-1")
    assert await h7.redis.hget(state_key, "status") == b"running"

    release_new.set()
    await new_task
    assert await h7.redis.hget(state_key, "status") == b"completed"
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []


async def test_retry_is_atomic_and_preserves_metadata(h7: Harness):
    message_id, fields = await h7.deliver(_fields())
    worker = QueueWorker(h7.redis, queue=h7.queue)
    worker._consumer_name = "consumer-a"
    worker._dispatch = AsyncMock(side_effect=[RuntimeError("retry me"), None])

    await worker._process_message(message_id, fields)
    assert await h7.redis.xlen(h7.queue.stream) == 2
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []

    result = await h7.redis.xreadgroup(
        h7.queue.group, "consumer-a", {h7.queue.stream: ">"}, count=1
    )
    retry_id, retry_fields = result[0][1][0]
    expected = {key.encode(): value.encode() for key, value in _fields().items()}
    expected[b"retry_count"] = b"1"
    assert retry_fields == expected

    await worker._process_message(retry_id, retry_fields)
    assert worker._dispatch.await_count == 2
    state_key = idempotency.state_key(h7.namespace, "business-1")
    assert await h7.redis.hget(state_key, "status") == b"completed"
    assert await h7.redis.hget(state_key, "attempt") == b"2"


async def test_deferred_retry_preserves_metadata(h7: Harness):
    message_id, fields = await h7.deliver(_fields())
    worker = QueueWorker(h7.redis, queue=h7.queue)
    worker._consumer_name = "consumer-a"
    worker._dispatch = AsyncMock(side_effect=RetryLaterError("busy"))

    await worker._process_message(message_id, fields)
    members = await h7.redis.zrange(h7.queue.delayed, 0, -1)
    assert len(members) == 1
    deferred = json.loads(members[0])
    expected = dict(_fields(), delay_count="1")
    assert deferred == expected
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []

    score = await h7.redis.zscore(h7.queue.delayed, members[0])
    assert await worker._promote_delayed(now=float(score) + 1) == 1
    result = await h7.redis.xreadgroup(
        h7.queue.group, "consumer-a", {h7.queue.stream: ">"}, count=1
    )
    promoted_fields = result[0][1][0][1]
    assert promoted_fields == {
        key.encode(): value.encode() for key, value in expected.items()
    }


async def test_failed_atomic_dlq_write_leaves_source_pending(h7: Harness):
    message_id, _ = await h7.deliver(_fields())
    claim = (await _claim(h7)).claim
    assert claim is not None
    bad_target = h7.keys[-1]
    await h7.redis.set(bad_target, "not-a-stream")

    with pytest.raises(aioredis.ResponseError):
        await idempotency.dead_letter(
            h7.redis,
            claim,
            source=h7.queue.stream,
            target=bad_target,
            group=h7.queue.group,
            consumer="consumer-a",
            message_id=message_id,
            fields=_fields(error="boom"),
            maxlen=100,
        )

    pending = await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, message_id, message_id, count=1
    )
    assert len(pending) == 1
    state_key = idempotency.state_key(h7.namespace, "business-1")
    assert await h7.redis.hget(state_key, "status") == b"running"
    assert await h7.redis.get(bad_target) == b"not-a-stream"


async def test_plain_xadd_ack_is_atomic_on_target_failure(h7: Harness):
    message_id, _ = await h7.deliver(_fields())
    bad_target = h7.keys[-1]
    await h7.redis.set(bad_target, "not-a-stream")

    with pytest.raises(aioredis.ResponseError):
        await atomic_xadd_ack(
            h7.redis,
            source=h7.queue.stream,
            target=bad_target,
            group=h7.queue.group,
            consumer="consumer-a",
            message_id=message_id,
            fields=_fields(),
            maxlen=100,
        )
    assert len(
        await h7.redis.xpending_range(
            h7.queue.stream, h7.queue.group, message_id, message_id, count=1
        )
    ) == 1

    await h7.redis.delete(bad_target)
    new_id = await atomic_xadd_ack(
        h7.redis,
        source=h7.queue.stream,
        target=bad_target,
        group=h7.queue.group,
        consumer="consumer-a",
        message_id=message_id,
        fields=_fields(),
        maxlen=100,
    )
    assert new_id is not None
    assert await h7.redis.xlen(bad_target) == 1
    assert await h7.redis.xpending_range(
        h7.queue.stream, h7.queue.group, "-", "+", count=10
    ) == []


async def test_delayed_promotion_is_single_owner(h7: Harness):
    member = '{"task_id":"delayed-1"}'
    await h7.redis.zadd(h7.queue.delayed, {member: 1.0})
    results = await asyncio.gather(
        atomic_promote_delayed(
            h7.redis,
            delayed=h7.queue.delayed,
            target=h7.queue.stream,
            member=member,
            maxlen=100,
        ),
        atomic_promote_delayed(
            h7.redis,
            delayed=h7.queue.delayed,
            target=h7.queue.stream,
            member=member,
            maxlen=100,
        ),
    )
    assert sum(result is not None for result in results) == 1
    assert await h7.redis.xlen(h7.queue.stream) == 1
    assert await h7.redis.zcard(h7.queue.delayed) == 0
