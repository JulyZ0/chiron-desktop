"""真取消：引擎侧的中断信号 + run 循环的响应 + 控制端点的校验。

这是 ACP `session/cancel` 的前置 —— "假取消"（客户端停止读 SSE）会让引擎继续烧 token、继续写
文件。本文件钉住三件事：

1. **信号语义**：本地快路径 + Redis 跨副本；**clear 之后绝不残留**（残留会误伤下一次会话）；
2. **run 的响应**：在**轮次边界**停下，发 `cancelled` **再补 `done`**（只发 cancelled 会让下游
   SSE 挂住），且**不再调用模型**；
3. **端点校验**：会话归属 + 调用者身份 + run token —— 与 approval / answer 同一套。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import redis.asyncio as aioredis

from app.agent import interrupt
from app.agent.interrupt import (
    clear_interrupt,
    is_interrupted,
    request_interrupt,
    request_interrupt_local,
)
from app.agent.runtime import AgentRuntime, AgentTask
from app.gateway.provider import ChatResponse


@pytest.fixture(autouse=True)
def _clean_local_flags():
    """每个用例前后都清干净本地标志（它是模块级状态，跨用例会串）。"""
    interrupt._LOCAL.clear()
    yield
    interrupt._LOCAL.clear()


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


class _FakeRedis:
    """最小 Redis 替身：只要 `setex` / `get` / `delete` 三个方法。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False
        self.deleted: list[str] = []

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def delete(self, key: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.deleted.append(key)
        self.store.pop(key, None)

    async def eval(self, script: str, _numkeys: int, *args: Any) -> int:
        import json

        if self.fail:
            raise RuntimeError("redis down")
        if "cur == '1'" in script or "cur ~= '1'" in script:
            signal_key = str(args[0])
            raw = self.store.get(signal_key)
            if "cur ~= '1'" in script:
                if raw is not None and raw != "1":
                    return 0
                self.store[signal_key] = str(args[1])
                return 1
            return int(raw == "1" and self.store.pop(signal_key, None) == "1")
        owner_key, signal_key = str(args[0]), str(args[1])
        token, generation = str(args[2]), int(args[3])
        owner = self.store.get(owner_key)
        owner_data = json.loads(owner) if owner else None
        if "redis.call('SET', KEYS[2]" in script:
            if not owner_data or (
                owner_data.get("run_token") != token
                or owner_data.get("generation") != generation
            ):
                return 0
            self.store[signal_key] = str(args[4])
            return 1
        if owner_data and (
            owner_data.get("run_token") != token
            or owner_data.get("generation") != generation
        ):
            return 0
        raw = self.store.get(signal_key)
        data = json.loads(raw) if raw else None
        if data and data.get("run_token") == token and data.get("generation") == generation:
            self.store.pop(signal_key, None)
            return 1
        return 0


# ── 信号语义 ────────────────────────────────────────────────────────────


async def test_local_flag_works_without_redis():
    """没有 Redis 也要能用：同副本的 run 靠本地标志就能停下。"""
    assert await is_interrupted("s1", redis=None) is False

    request_interrupt_local("s1")

    assert await is_interrupted("s1", redis=None) is True


async def test_redis_flag_is_read_across_replicas():
    """run 在别的副本时，只有 Redis 里的信号能被读到（本地标志是空的）。"""
    redis = _FakeRedis()

    assert await request_interrupt("s1", redis=redis) is True
    interrupt._LOCAL.clear()  # 模拟"这个副本上没有该 run 的本地标志"

    assert await is_interrupted("s1", redis=redis) is True


async def test_clear_removes_both_local_and_redis():
    """**不清会误伤下一次会话** —— 同一 session 的下一次 run 会一上来就被取消。"""
    redis = _FakeRedis()
    await request_interrupt("s1", redis=redis)

    await clear_interrupt("s1", redis=redis)

    assert await is_interrupted("s1", redis=redis) is False
    assert redis.store == {}


async def test_redis_failure_does_not_break_local_path():
    """Redis 挂了也要能取消同副本的 run，且读取时不抛错（当没被取消处理）。"""
    redis = _FakeRedis()
    redis.fail = True

    assert await request_interrupt("s1", redis=redis) is False  # 没置上 Redis
    assert await is_interrupted("s1", redis=redis) is True  # 本地仍生效
    await clear_interrupt("s1", redis=redis)  # 不该抛


async def test_stale_worker_cannot_request_or_clear_new_signal():
    from app import run_registry

    redis = _FakeRedis()
    owner_key = run_registry.run_key("s-fenced")
    signal_key = interrupt.interrupt_key("s-fenced")
    redis.store[owner_key] = '{"run_token":"rt-new","generation":2}'

    assert await request_interrupt(
        "s-fenced", redis=redis, run_token="rt-old", generation=1
    ) is False
    assert signal_key not in redis.store

    redis.store[signal_key] = '{"run_token":"rt-new","generation":2}'
    await clear_interrupt("s-fenced", redis=redis, run_token="rt-old", generation=1)
    assert signal_key in redis.store
    assert await interrupt.probe_interrupt(
        "s-fenced", redis=redis, run_token="rt-new", generation=2
    ) is True


async def test_current_worker_clears_only_its_signal():
    from app import run_registry

    redis = _FakeRedis()
    owner_key = run_registry.run_key("s-current")
    signal_key = interrupt.interrupt_key("s-current")
    redis.store[owner_key] = '{"run_token":"rt-current","generation":3}'

    assert await request_interrupt(
        "s-current", redis=redis, run_token="rt-current", generation=3
    ) is True
    await clear_interrupt(
        "s-current", redis=redis, run_token="rt-current", generation=3
    )
    assert signal_key not in redis.store


async def test_missing_signal_is_not_an_interrupt():
    redis = _FakeRedis()

    assert await interrupt.probe_interrupt("s-missing", redis=redis) is False
    assert await is_interrupted("s-missing", redis=redis) is False


async def test_invalid_explicit_fence_cannot_write_local_signal():
    from app import run_registry

    request_interrupt_local("s-invalid", run_token="rt-unbound", generation=0)
    assert interrupt._LOCAL == {}
    assert run_registry.resolve_run_fence("rt-unbound") is None


async def test_stale_request_rejection_does_not_remove_new_local_signal():
    from app import run_registry

    class _RacingRedis(_FakeRedis):
        async def eval(self, script: str, numkeys: int, *args: Any) -> int:
            result = await super().eval(script, numkeys, *args)
            interrupt._LOCAL["s-race"] = ("rt-new", 2, time.time())
            return result

    redis = _RacingRedis()
    redis.store[run_registry.run_key("s-race")] = json.dumps(
        {"run_token": "rt-new", "generation": 2}
    )

    assert await request_interrupt(
        "s-race", redis=redis, run_token="rt-old", generation=1
    ) is False
    assert interrupt._LOCAL["s-race"][:2] == ("rt-new", 2)
    assert interrupt.interrupt_key("s-race") not in redis.store


async def test_unfenced_clear_preserves_fenced_local_and_redis_signals():
    from app import run_registry

    redis = _FakeRedis()
    session_id = "s-new-fence"
    redis.store[run_registry.run_key(session_id)] = json.dumps(
        {"run_token": "rt-new", "generation": 2}
    )
    redis.store[interrupt.interrupt_key(session_id)] = json.dumps(
        {"run_token": "rt-new", "generation": 2}
    )
    interrupt._LOCAL[session_id] = ("rt-new", 2, time.time())

    await clear_interrupt(session_id, redis=redis)

    assert interrupt._LOCAL[session_id][:2] == ("rt-new", 2)
    assert interrupt.interrupt_key(session_id) in redis.store
    assert redis.deleted == []


async def test_invalid_explicit_clear_cannot_remove_legacy_signal():
    redis = _FakeRedis()
    redis.store[interrupt.interrupt_key("s-invalid-clear")] = "1"

    await clear_interrupt(
        "s-invalid-clear", redis=redis, run_token="rt-unbound", generation=0
    )

    assert redis.store[interrupt.interrupt_key("s-invalid-clear")] == "1"
    assert redis.deleted == []


@pytest.mark.live
async def test_real_redis_interrupt_fences_and_atomic_legacy_clear(real_redis):
    from app import run_registry

    session_id = f"h8-interrupt-{uuid.uuid4().hex}"
    owner_key = run_registry.run_key(session_id)
    signal_key = interrupt.interrupt_key(session_id)
    try:
        await real_redis.set(owner_key, json.dumps({"run_token": "rt-shared", "generation": 2}))
        assert await request_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=2
        ) is True
        assert await request_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=1
        ) is False
        await clear_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=1
        )
        await clear_interrupt(session_id, redis=real_redis)
        assert await interrupt.probe_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=2
        ) is True
        await clear_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=2
        )
        assert await interrupt.probe_interrupt(
            session_id, redis=real_redis, run_token="rt-shared", generation=2
        ) is False

        await real_redis.set(signal_key, "1")
        await clear_interrupt(session_id, redis=real_redis)
        assert await real_redis.get(signal_key) is None
    finally:
        await real_redis.delete(owner_key, signal_key)


async def test_empty_session_id_is_ignored():
    assert await request_interrupt("", redis=_FakeRedis()) is False
    assert await is_interrupted("", redis=_FakeRedis()) is False


# ── run 循环的响应 ──────────────────────────────────────────────────────


async def _run_with_interrupt(session_id: str = "s1") -> tuple[list[Any], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        calls.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id=session_id,
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=3,
    )
    request_interrupt_local(session_id)
    events = [event async for event in runtime.run(task)]
    return events, calls


async def test_run_stops_at_turn_boundary_and_emits_cancelled_then_done():
    events, calls = await _run_with_interrupt()

    types = [e.type for e in events]
    assert "cancelled" in types, "要明确告诉下游这是取消，不是失败"
    assert types[-1] == "done", "只发 cancelled 会让下游的 SSE 挂住"
    assert calls == [], "取消就该在轮次边界停住，不该再调模型"


async def test_interrupt_signal_is_cleared_after_the_run():
    """run 结束（含被取消）后信号必须清掉，否则下一次会话一上来就被取消。"""
    await _run_with_interrupt("s-clear")

    assert await is_interrupted("s-clear", redis=None) is False


async def test_run_without_interrupt_proceeds_normally():
    calls: list[dict[str, Any]] = []
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        calls.append(kwargs)
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s-normal",
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=1,
    )
    events = [event async for event in runtime.run(task)]

    assert calls, "没有取消信号时应当正常调用模型"
    assert "cancelled" not in [e.type for e in events]


# ── 控制端点的校验 ──────────────────────────────────────────────────────


async def _post_interrupt(body: dict[str, Any], *, caller: str = "") -> dict[str, Any]:
    import httpx
    from httpx import ASGITransport

    import app.main as main

    from app.config import settings

    headers = {"x-user-id": caller} if caller else {}
    headers["x-internal-token"] = settings.internal_token
    async with httpx.AsyncClient(
        transport=ASGITransport(app=main.create_app()), base_url="http://t", headers=headers
    ) as client:
        resp = await client.post("/v1/agent/interrupt", json=body)
        return resp.json()


async def test_endpoint_requires_session_id():
    out = await _post_interrupt({})

    assert out["ok"] is False and "session_id" in out["error"]


async def test_endpoint_rejects_non_owner(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s1": (MagicMock(), "owner-u", "tok")}, raising=False
    )

    out = await _post_interrupt({"session_id": "s1"}, caller="someone-else")

    assert out["ok"] is False and "owner" in out["error"]


async def test_endpoint_sets_signal_for_owner(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s2": (MagicMock(), "owner-u", "tok")}, raising=False
    )
    faked = _FakeRedis()
    monkeypatch.setattr(main, "_redis", faked, raising=False)

    out = await _post_interrupt({"session_id": "s2"}, caller="owner-u")

    assert out["ok"] is True
    assert await is_interrupted("s2", redis=faked) is True


async def test_endpoint_rejects_stale_run_token(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main, "_ACTIVE_RUNTIMES", {"s3": (MagicMock(), "owner-u", "tok-new")}, raising=False
    )

    out = await _post_interrupt(
        {"session_id": "s3", "run_token": "tok-old"}, caller="owner-u"
    )

    assert out["ok"] is False and "run token" in out["error"]


async def test_endpoint_falls_back_to_run_registry_when_run_is_elsewhere(monkeypatch):
    """本副本没有该 run（在别的副本）时也要能置信号 —— 这才是跨副本取消。"""
    import app.main as main
    from app import run_registry

    monkeypatch.setattr(main, "_ACTIVE_RUNTIMES", {}, raising=False)
    faked = _FakeRedis()
    monkeypatch.setattr(main, "_redis", faked, raising=False)

    async def fake_owner_of(_redis: Any, _session_id: str) -> dict[str, Any]:
        owner = {
            "instance_id": "other",
            "owner_uid": "owner-u",
            "run_token": "tok",
            "generation": 1,
        }
        faked.store[run_registry.run_key("s4")] = json.dumps(owner)
        return owner

    monkeypatch.setattr("app.run_registry.owner_of", fake_owner_of)

    out = await _post_interrupt({"session_id": "s4"}, caller="owner-u")

    assert out["ok"] is True
    assert await interrupt.probe_interrupt(
        "s4", redis=faked, run_token="tok", generation=1
    ) is True


async def test_endpoint_reports_no_active_run(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_ACTIVE_RUNTIMES", {}, raising=False)

    async def fake_owner_of(_redis: Any, _session_id: str) -> None:
        return None

    monkeypatch.setattr("app.run_registry.owner_of", fake_owner_of)

    out = await _post_interrupt({"session_id": "s5"}, caller="owner-u")

    assert out["ok"] is False and "no active agent" in out["error"]
