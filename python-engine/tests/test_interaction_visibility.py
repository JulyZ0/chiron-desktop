"""用户报的两个"交互能力等于不存在"的回归。

两处缺陷的共同形态都是「注册了 ≠ 可见/可达」，症状却是"界面从来不弹任何东西"：

1. **`ask` 模式下从不弹工具确认**。`ToolGuard` 正确判出了 `confirm`，但紧接着的
   LLM 安全评审（guardian）在 `write` 级上把它降级成 `allow` —— 而 guardian 一次
   都没被跳过，`ask` 于是被悄悄降级成 `auto`。用户选了"询问"，界面却一次都不问。

2. **模型从不主动向用户提问**。`ask_user` 早已注册，却不在任何模式的工具白名单里
   （`normal` 20 个 / `code` 14 个 / `minimal` 3 个，全都没有它），`_get_core_tools`
   把它过滤掉了 —— 模型看不见这个工具，自然永远不会调用。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.tools  # noqa: F401 — 触发内置工具注册（含 ask_user）
from app.agent.guards import SESSION_MODE_ASK, SESSION_MODE_AUTO
from app.agent.runtime import AgentRuntime


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="sess-1",
        user_id="user-1",
        tenant_id=0,
        content="帮我改一下这个文件",
        llm_config={"tools_mode": SESSION_MODE_ASK},
    )


def _write_call() -> dict[str, object]:
    """一个必然被判 `confirm` 的调用：`write_file` 属 write 级。"""
    return {
        "id": "tc-write-1",
        "name": "write_file",
        "arguments": '{"path": "notes.txt", "content": "hi"}',
    }


class TestAskUserIsAlwaysVisible:
    """`ask_user` 必须进每一个模式的工具面。"""

    @pytest.mark.parametrize("mode", ["normal", "code", "minimal"])
    def test_ask_user_visible_in_every_mode(self, mode: str):
        runtime = AgentRuntime(gateway=None)
        tools = runtime._get_core_tools(None) if mode == "normal" else None
        if tools is None:
            from app.agent.modes import get_mode_config

            tools = runtime._get_core_tools(get_mode_config(mode))

        assert tools, f"{mode} 模式下工具列表为空"
        names = {t["function"]["name"] for t in tools}
        # 没有它，模型就没有任何渠道向用户提问 ——"agent 从不发起选项提问"的根因。
        assert "ask_user" in names, (
            f"{mode} 模式的工具面里没有 ask_user：模型看不见这个能力，"
            "于是永远不会主动向用户提问"
        )


class TestAskModeSkipsGuardian:
    """`ask` 模式下 guardian 不得把 `confirm` 降级成 `allow`。"""

    def test_guardian_not_consulted_in_ask_mode(self, monkeypatch):
        runtime = AgentRuntime(gateway=None)
        runtime._current_mode = SESSION_MODE_ASK

        consulted: list[str] = []

        async def _fake_review(*_args, **_kwargs):
            consulted.append("called")
            # 若被调用，返回一个"允许放行"的评审结论 —— 正是它曾把 ask 吞掉
            from app.agent.guardian import GuardianAssessment

            return GuardianAssessment(
                outcome="allow", risk_level="low", rationale="looks fine", failed=False
            )

        async def _fake_begin(tool_call, task, level="", targs=None):
            from app.agent.runtime import AgentEvent

            return AgentEvent(
                type="approval",
                tool_call_id=str(tool_call.get("id")),
                tool_name=str(tool_call.get("name")),
                content="请确认",
            )

        monkeypatch.setattr(runtime, "_guardian_review", _fake_review)
        monkeypatch.setattr(runtime, "_begin_approval", _fake_begin)

        result, approval = asyncio.run(
            runtime._guarded_execute_tool(_write_call(), _task(), allow_approval=True)
        )

        assert consulted == [], (
            "ask 模式下仍然调用了 guardian：它会把 write 级的 confirm 降级为 allow，"
            "等于把用户选的「询问」悄悄变成「自动」"
        )
        assert approval is not None, "ask 模式下必须产出审批事件（前端据此弹确认卡片）"
        assert approval.type == "approval"
        assert result is None, "确认分支不执行工具，等用户裁决后由 _await_approval 补结果"

    def test_auto_mode_write_is_not_prompted(self, monkeypatch):
        """反向守卫：`auto` 模式下 write 级本来就不确认 —— 这次改动不能让它开始弹窗。

        （`auto` 只对 delete/external 要求确认，见 `tool_policy.requires_confirmation`。
        所以 write 级在 auto 下走的是 allow 路径，压根不进 guardian，这是既有语义。）
        """
        runtime = AgentRuntime(gateway=None)
        runtime._current_mode = SESSION_MODE_AUTO
        runtime._loop_guard = None

        consulted: list[str] = []

        async def _fake_review(*_args, **_kwargs):
            consulted.append("called")
            return None

        async def _fake_execute(*_args, **_kwargs):
            # 真实签名返回 `(result, ask_reason)` 二元组：ask_reason 为 hook 拦成"需确认"的原因
            return {"ok": True}, None

        monkeypatch.setattr(runtime, "_guardian_review", _fake_review)
        monkeypatch.setattr(runtime, "_execute_tool", _fake_execute, raising=False)

        result, approval = asyncio.run(
            runtime._guarded_execute_tool(_write_call(), _task(), allow_approval=True)
        )

        assert approval is None, "auto 模式下 write 级不应弹确认卡片（与改动前一致）"
        assert result == {"ok": True}, "auto 模式下 write 级应当直接执行"
