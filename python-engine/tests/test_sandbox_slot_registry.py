"""R7：注册表只在沙箱工具外层占一个租户槽，嵌套调用不重复占。"""
from __future__ import annotations

import pytest

import importlib

registry_mod = importlib.import_module("app.tools.registry")
from app.tools.registry import ToolRegistry
from app.tools.sandbox_slot import SandboxLease, sandbox_slot_held


def _registry(handler) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        name="shell_exec",
        description="fake sandbox tool",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    reg.register(
        name="read_file",
        description="not a sandbox tool",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    return reg


@pytest.mark.asyncio
async def test_registry_denies_when_slot_full(monkeypatch) -> None:
    called = False

    async def fake_acquire():
        return None

    async def handler(**_: object) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(registry_mod, "acquire_sandbox_slot", fake_acquire)
    out = await _registry(handler).execute("shell_exec", {})
    assert out["code"] == "sandbox_concurrency_limited"
    assert called is False, "被限流时不能真的执行"

    # 非沙箱工具完全不碰槽位。
    out = await _registry(handler).execute("read_file", {})
    assert "code" not in out and called is True


@pytest.mark.asyncio
async def test_registry_holds_slot_across_nested_calls(monkeypatch) -> None:
    released = 0

    class _Lease(SandboxLease):
        async def release(self) -> None:  # type: ignore[override]
            nonlocal released
            released += 1

    async def fake_acquire():
        return _Lease(key="sem:sandbox:1")

    async def handler(**_: object) -> dict:
        return {"nested": sandbox_slot_held()}

    monkeypatch.setattr(registry_mod, "acquire_sandbox_slot", fake_acquire)
    out = await _registry(handler).execute("shell_exec", {})
    assert out == {"nested": True}
    assert released == 1
