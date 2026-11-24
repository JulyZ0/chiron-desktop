"""H8: session run ownership is fenced by token and generation."""

from __future__ import annotations

import json
import os
import uuid

import pytest
import redis.asyncio as aioredis

from app import run_registry

_REDIS_URL = (
    os.getenv("CHIRON_TEST_REDIS_URL")
    or os.getenv("REDIS_URL")
    or "redis://127.0.0.1:6379/0"
)


@pytest.fixture
async def real_redis():
    client = aioredis.from_url(
        _REDIS_URL,
        decode_responses=True,
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
    try:
        yield client
    finally:
        await client.aclose()


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex

    async def eval(self, script: str, _numkeys: int, *args: object) -> int:
        key = str(args[0])
        raw = self.kv.get(key)
        data = json.loads(raw) if raw else None
        if "redis.call('DEL'" in script:
            expected_generation = int(args[2])
            return int(
                bool(
                    data
                    and data.get("run_token") == args[1]
                    and int(data.get("generation") or 0) == expected_generation
                )
            )
        if "redis.call('SET'" in script:
            expected_token = str(args[-2])
            expected_generation = int(args[-1])
            if data is not None and (
                data.get("run_token") != expected_token
                or int(data.get("generation") or 0) != expected_generation
            ):
                return 0
            self.kv[key] = str(args[1])
            self.ttl[key] = int(args[2])
            return 1
        raise AssertionError(f"unexpected Lua operation: {script}")


@pytest.fixture(autouse=True)
def clean_fences() -> None:
    run_registry._FENCES.clear()
    run_registry._CURRENT_FENCE.set(None)
    yield
    run_registry._FENCES.clear()
    run_registry._CURRENT_FENCE.set(None)


async def test_claim_is_atomic_and_records_generation() -> None:
    redis = FakeRedis()
    lease = run_registry.RunLease(redis, "s1", "eng-a", "rt-a")

    assert await lease.start() is True
    owner = await run_registry.owner_of(redis, "s1")
    assert owner is not None
    assert owner["run_token"] == "rt-a"
    assert owner["generation"] == 1
    await lease.stop()


async def test_new_worker_cannot_overwrite_live_owner() -> None:
    redis = FakeRedis()
    first = run_registry.RunLease(redis, "s1", "eng-a", "rt-a")
    second = run_registry.RunLease(redis, "s1", "eng-b", "rt-b")

    assert await first.start() is True
    assert await second.start() is False
    assert (await run_registry.owner_of(redis, "s1"))["run_token"] == "rt-a"
    await first.stop()


async def test_stale_heartbeat_and_release_cannot_mutate_new_owner() -> None:
    redis = FakeRedis()
    stale = run_registry.RunLease(redis, "s1", "eng-a", "rt-a", generation=1)
    assert await stale.start() is True
    redis.kv[run_registry.run_key("s1")] = json.dumps(
        {
            "instance_id": "eng-b",
            "run_token": "rt-b",
            "generation": 2,
            "last_seen": "new",
        }
    )

    assert await stale._publish() is False
    assert stale.owned is False
    await stale.stop()
    owner = await run_registry.owner_of(redis, "s1")
    assert owner is not None and owner["run_token"] == "rt-b"
    assert owner["generation"] == 2


async def test_same_token_with_new_generation_is_stale() -> None:
    redis = FakeRedis()
    stale = run_registry.RunLease(redis, "s1", "eng-a", "rt-shared", generation=1)
    assert await stale.start() is True
    redis.kv[run_registry.run_key("s1")] = json.dumps(
        {"instance_id": "eng-a", "run_token": "rt-shared", "generation": 2}
    )

    assert await stale._publish() is False
    await stale.stop()
    assert (await run_registry.owner_of(redis, "s1"))["generation"] == 2


def test_fence_context_resolves_token_and_generation() -> None:
    fence = run_registry.bind_run_fence("rt-context", 7)
    assert fence is not None
    token = run_registry.activate_run_fence(fence)

    assert run_registry.current_run_fence() == fence
    assert run_registry.resolve_run_fence("rt-context") == fence
    assert run_registry.resolve_run_fence("rt-context", 8) != fence
    run_registry.deactivate_run_fence(token)
    assert run_registry.resolve_run_fence("rt-context") == fence


async def test_unstarted_lease_cannot_release_an_existing_owner() -> None:
    redis = FakeRedis()
    key = run_registry.run_key("s-unstarted")
    redis.kv[key] = json.dumps({"run_token": "rt-owner", "generation": 4})
    lease = run_registry.RunLease(redis, "s-unstarted", "eng-a", "rt-owner", generation=4)

    await lease.stop()

    assert json.loads(redis.kv[key])["generation"] == 4


async def test_invalid_fence_cannot_claim() -> None:
    redis = FakeRedis()
    lease = run_registry.RunLease(redis, "s-invalid", "eng-a", "", generation=1)

    assert await lease.start() is False
    assert redis.kv == {}


@pytest.mark.live
async def test_real_redis_lua_fences_claim_heartbeat_and_release(real_redis) -> None:
    session_id = f"h8-registry-{uuid.uuid4().hex}"
    key = run_registry.run_key(session_id)
    lease = run_registry.RunLease(real_redis, session_id, "eng-a", "rt-shared", generation=1)
    try:
        assert await lease.start() is True
        assert await lease._publish() is True
        owner = await run_registry.owner_of(real_redis, session_id)
        assert owner is not None and owner["generation"] == 1

        await real_redis.set(key, json.dumps({"run_token": "rt-shared", "generation": 2}))
        assert await lease._publish() is False
        await lease.stop()
        owner = await run_registry.owner_of(real_redis, session_id)
        assert owner is not None and owner["generation"] == 2
    finally:
        await real_redis.delete(key)
