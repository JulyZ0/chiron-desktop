"""R7：租户级沙箱并发槽（获取/释放/嵌套标记/fail-open）。"""
from __future__ import annotations

import pytest

from app.tools import sandbox_slot
from app.tools.context import set_tool_context


class _FakeRedis:
    def __init__(self) -> None:
        self.count = 0
        self.ttl: int | None = None

    async def eval(self, script: str, numkeys: int, key: str, *args):  # noqa: ARG002
        if "INCR" in script:
            limit = int(args[0])
            if self.count + 1 > limit:
                return 0
            self.count += 1
            self.ttl = int(args[1])
            return 1
        if self.count > 0:
            self.count -= 1
            return 1
        return 0


@pytest.mark.asyncio
async def test_acquire_release_and_limit(monkeypatch) -> None:
    redis = _FakeRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", fake_get_redis)
    monkeypatch.setattr(
        "app.config.settings.sandbox_max_concurrency_per_tenant", 1
    )
    set_tool_context(tenant_id=7)
    try:
        first = await sandbox_slot.acquire_sandbox_slot()
        assert first is not None and first.key.endswith(":7")
        assert redis.count == 1 and redis.ttl == 300

        assert await sandbox_slot.acquire_sandbox_slot() is None, "超过上限应拒绝"

        await first.release()
        assert redis.count == 0

        second = await sandbox_slot.acquire_sandbox_slot()
        assert second is not None
        await second.release()
    finally:
        set_tool_context(tenant_id=0)


@pytest.mark.asyncio
async def test_disabled_and_fail_open(monkeypatch) -> None:
    set_tool_context(tenant_id=7)
    try:
        monkeypatch.setattr(
            "app.config.settings.sandbox_max_concurrency_per_tenant", 0
        )
        lease = await sandbox_slot.acquire_sandbox_slot()
        assert lease is not None and lease.key == "", "关闭时返回恒放行租约"

        monkeypatch.setattr(
            "app.config.settings.sandbox_max_concurrency_per_tenant", 2
        )

        async def broken_redis():
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.redis_client.get_redis", broken_redis)
        lease = await sandbox_slot.acquire_sandbox_slot()
        assert lease is not None and lease.key == "", "Redis 故障必须放行"
        await lease.release()
    finally:
        set_tool_context(tenant_id=0)


def test_hold_sets_nested_marker() -> None:
    lease = sandbox_slot.SandboxLease()
    assert sandbox_slot.sandbox_slot_held() is False
    with lease.hold():
        assert sandbox_slot.sandbox_slot_held() is True
    assert sandbox_slot.sandbox_slot_held() is False
