"""EngineRegistry 心跳载荷：池观测字段必须随心跳上报（R6）。"""

from __future__ import annotations

import json

from app.engine_registry import EngineRegistry


class _FakeRedis:
    def __init__(self) -> None:
        self.value: dict = {}
        self.key = ""
        self.ttl: int | None = None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.key = key
        self.value = json.loads(value)
        self.ttl = ex


async def test_publish_includes_runtime_stats(monkeypatch) -> None:
    redis = _FakeRedis()
    reg = EngineRegistry(redis, "engine-0", "http://engine-0:8000/")
    monkeypatch.setattr(
        reg,
        "_runtime_stats",
        lambda: {"active_tasks": 3, "cpu_percent": 12.5, "memory_mb": 262.0},
    )

    await reg._publish("engine:instance:engine-0")

    assert redis.key == "engine:instance:engine-0"
    assert redis.ttl == 60
    assert redis.value["url"] == "http://engine-0:8000"
    assert redis.value["instance_id"] == "engine-0"
    assert redis.value["active_tasks"] == 3
    assert redis.value["cpu_percent"] == 12.5
    assert redis.value["memory_mb"] == 262.0
    assert redis.value["last_seen"]


async def test_runtime_stats_failure_keeps_heartbeat(monkeypatch) -> None:
    redis = _FakeRedis()
    reg = EngineRegistry(redis, "engine-1", "http://engine-1:8000")

    def _boom() -> dict:
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr(reg, "_runtime_stats", _boom)

    await reg._publish("k")

    assert redis.value["url"] == "http://engine-1:8000"
    assert "active_tasks" not in redis.value
