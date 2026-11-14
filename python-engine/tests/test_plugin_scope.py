"""插件（MCP server）在对话内的作用域。

## 修的是什么

用户在对话里挑插件（`workbench_context.plugin_names`），按设计只应影响本次对话。
但真实情况是**这件事完全没生效**，有两处断点：

1. `runtime` 从未把 `plugin_names` 写进工具上下文。于是 `use_capability` 代理层
   无从得知用户挑了什么 —— 它列出的、能调用的，永远是"该用户连上的全部 MCP"。
2. 唯一在筛的地方 `_restrict_tools_to_plugins` 筛的是**可见工具清单**，而 MCP
   工具是隐藏来源（`HIDDEN_SOURCES`），压根不在那份清单里。它筛了个空。

两处叠加的结果：插件系统看着有开关、有选择器、有 UI，实际调用权限与用户的选择
毫无关系。这里钉住的是"选择真的落地"。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent.runtime import AgentTask, AgentRuntime
from app.gateway.provider import ChatResponse
from app.tools import use_capability as uc
from app.tools.context import get_all, get_plugin_names, restore_context, set_tool_context
from app.tools.registry import SOURCE_MCP, registry


@pytest.fixture(autouse=True)
def _isolate_registry_and_context() -> Any:
    """清掉本用例注册的 MCP 工具与残留上下文，避免污染全局。"""
    before = set(registry.list_names())
    saved = get_all()

    def _drop_mcp() -> None:
        for name in list(registry.list_names()):
            if name in before:
                continue
            tool = registry.get(name)
            if tool is not None and tool.source == SOURCE_MCP:
                registry.unregister(name)

    yield

    _drop_mcp()
    restore_context(saved)


def _register_mcp(name: str) -> None:
    async def handler(**kwargs: object) -> dict[str, object]:
        return {"ok": True, "echo": kwargs}

    registry.register(
        name=name,
        description=f"tool {name}",
        parameters={"type": "object"},
        handler=handler,
        source=SOURCE_MCP,
    )


# ── 上下文读取 ──────────────────────────────────────────────────────────


def test_plugin_names_default_to_empty_means_unscoped():
    """没挑插件 = 不限制（不是"一个都不许"）。"""
    assert get_plugin_names() == []


def test_plugin_names_are_read_from_context():
    set_tool_context(plugin_names=["fs", "  git  ", ""])

    assert get_plugin_names() == ["fs", "git"], "空串要丢掉，空白要裁掉"


def test_plugin_names_tolerate_a_wrong_shape():
    """形状不对时当作"没挑"，而不是把结构体当名字遍历。"""
    set_tool_context(plugin_names="fs")

    assert get_plugin_names() == []


# ── 代理层：列出与调用都受选择约束 ────────────────────────────────────


async def test_listing_hides_plugins_outside_the_selection():
    _register_mcp("fs_read")
    _register_mcp("git_log")
    set_tool_context(plugin_names=["fs"])

    out = await uc.use_capability()

    names = [c["name"] for c in out["capabilities"]]
    assert "fs_read" in names
    assert "git_log" not in names, "没被选中的插件不该出现在清单里（模型会照着调）"
    assert out["scoped_to_plugins"] == ["fs"]


async def test_listing_shows_everything_when_nothing_is_selected():
    _register_mcp("fs_read")
    _register_mcp("git_log")

    out = await uc.use_capability()

    names = [c["name"] for c in out["capabilities"]]
    assert {"fs_read", "git_log"} <= set(names)
    assert "scoped_to_plugins" not in out


async def test_calling_outside_the_selection_is_refused():
    """明确拒绝，而不是静默执行 —— 这属于越出本次对话的授权范围。"""
    _register_mcp("git_log")
    set_tool_context(plugin_names=["fs"])

    out = await uc.use_capability("git_log", {"n": 3})

    assert "error" in out
    assert "not enabled for this conversation" in out["error"]
    assert out["scoped_to_plugins"] == ["fs"]
    assert "echo" not in out, "拒绝的调用不能真的执行"


async def test_calling_inside_the_selection_works():
    _register_mcp("fs_read")
    set_tool_context(plugin_names=["fs"])

    out = await uc.use_capability("fs_read", {"path": "a"})

    assert out.get("ok") is True


async def test_scope_does_not_leak_into_builtin_tools():
    """内置工具本就在清单里，不该因为"没选任何插件"就被代理拦下。"""
    set_tool_context(plugin_names=["fs"])

    out = await uc.use_capability("read_file", {"path": "a"})

    assert "built-in" in out["error"], "内置工具走的是另一条拒绝理由"


# ── 接线：runtime 必须把选择交给工具上下文 ────────────────────────────


def _task(**overrides: Any) -> AgentTask:
    payload: dict[str, Any] = dict(
        id="t1",
        tenant_id=1,
        user_id="u1",
        session_id="s1",
        content="hi",
        system_prompt="SYS",
        max_turns=1,
    )
    payload.update(overrides)
    return AgentTask(**payload)


async def _context_during_run(task: AgentTask) -> dict[str, Any]:
    """在模型调用发生的那一刻快照工具上下文（那时 `set_tool_context` 已生效）。"""
    seen: dict[str, Any] = {}
    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        seen.update(get_all())
        yield ChatResponse(content="ok", finish_reason="stop")

    gateway.chat_stream = fake_stream

    runtime = AgentRuntime(gateway=gateway)
    _ = [event async for event in runtime.run(task)]
    return seen


@pytest.mark.asyncio
async def test_runtime_publishes_the_plugin_selection_to_tools():
    """回归：此前 `plugin_names` 从未进入工具上下文，代理层永远读不到用户的选择。"""
    seen = await _context_during_run(
        _task(workbench_context={"plugin_names": ["fs", "git"]})
    )

    assert seen.get("plugin_names") == ["fs", "git"]


@pytest.mark.asyncio
async def test_runtime_publishes_empty_when_nothing_selected():
    seen = await _context_during_run(_task(workbench_context={}))

    assert seen.get("plugin_names") == []
