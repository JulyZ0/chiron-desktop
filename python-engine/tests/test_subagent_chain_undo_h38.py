"""H38：子 Agent 走完整执行链 + undo 快照安全（PRD 9.2）。

前端缺陷两条，都在"看起来能用"的表象下：

1. **子 Agent 绕过执行链**：`multi_agent.py::_execute_tool` 直接 `registry.execute`，
   于是 ToolGuard / 服务端授权 / 副作用账本对它不生效 —— 主 Agent 里"删文件要先确认"，
   子 Agent 里直接删。
2. **undo 会把文件弄坏**：快照以文本存（读取时 `errors="replace"`），二进制文件在
   快照那一刻就被替换成 U+FFFD；`pop()` 之后恢复失败还会把快照丢掉（不可重试）；
   校验边界与实际写入之间的窗口可被 symlink 替换（TOCTOU）。
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from app.agent import undo_stack
from app.agent.undo_stack import Snapshot


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ── 1. 子 Agent 执行器 ─────────────────────────────────────────────────────
def test_guarded_executor_denies_when_approval_needed() -> None:
    """后台子 Agent 无法交互：需要确认 ⇒ 显式拒绝（不是静默执行）。"""
    from app.agent.subagent_execution import make_guarded_subagent_executor

    class _Runtime:
        pass

    async def scenario() -> dict:
        runtime = _Runtime()

        async def fake_guarded(runtime_arg, tool_call, task, *, allow_approval=True):  # noqa: ANN001
            assert allow_approval is False, "子 Agent 必须显式声明不可交互"
            return None, {"type": "approval_request", "tool": tool_call["name"]}, 

        import app.agent.tool_execution as te

        original = te.guarded_execute_tool
        te.guarded_execute_tool = fake_guarded  # type: ignore[assignment]
        try:
            executor = make_guarded_subagent_executor(runtime)
            return await executor({"name": "shell_exec", "arguments": {"cmd": "rm -rf x"}})
        finally:
            te.guarded_execute_tool = original  # type: ignore[assignment]

    result = _run(scenario())
    assert result.get("denied") is True
    assert "requires user confirmation" in (result.get("error") or "")


def test_guarded_executor_passes_through_allowed_tool() -> None:
    from app.agent.subagent_execution import make_guarded_subagent_executor

    async def fake_guarded(runtime_arg, tool_call, task, *, allow_approval=True):  # noqa: ANN001
        return {"output": "done"}, None

    import app.agent.tool_execution as te

    async def scenario() -> dict:
        original = te.guarded_execute_tool
        te.guarded_execute_tool = fake_guarded  # type: ignore[assignment]
        try:
            executor = make_guarded_subagent_executor(object(), tenant_id=3, user_id="u1")
            return await executor({"name": "read_file", "arguments": {"path": "a"}})
        finally:
            te.guarded_execute_tool = original  # type: ignore[assignment]

    result = _run(scenario())
    assert result["tool"] == "read_file"
    assert result["output"] == "done"
    assert "denied" not in result


def test_subagent_uses_injected_executor_instead_of_registry() -> None:
    """注入了执行器就**不得**再直连注册表。"""
    from app.agent.multi_agent import SubAgent

    calls: list[str] = []

    async def executor(tool_call):  # noqa: ANN001
        calls.append(tool_call["name"])
        return {"tool": tool_call["name"], "output": "via-executor"}

    agent = SubAgent(name="x", description="d", system_prompt="p", tool_executor=executor)
    out = _run(agent._execute_tool({"name": "read_file", "arguments": {"path": "a"}}))
    assert out["output"] == "via-executor"
    assert calls == ["read_file"]


def test_dispatcher_injects_guarded_executor_when_runtime_present() -> None:
    from app.agent.multi_agent import AgentDispatcher

    dispatcher = AgentDispatcher(runtime=object())
    agent = dispatcher.register_agent(name="a", description="d", system_prompt="p")
    assert agent._tool_executor is not None, "有 runtime 时必须注入 guard 链执行器"

    plain = AgentDispatcher().register_agent(name="b", description="d", system_prompt="p")
    assert plain._tool_executor is None, "没有 runtime 时保持原语义（直连注册表）"


# ── 2. undo：二进制安全 ───────────────────────────────────────────────────
def test_restore_keeps_binary_bytes(tmp_path: Path) -> None:
    """二进制文件撤销后必须**逐字节相同**（旧实现会写成替换字符）。"""
    target = tmp_path / "image.bin"
    original = bytes(range(256))
    snapshot = Snapshot(
        path=str(target),
        existed=True,
        content=original.decode("utf-8", errors="replace"),  # 老字段（已损坏）
        content_b64=base64.b64encode(original).decode("ascii"),
        tool="write_file",
        at=0.0,
    )

    # 让 restore 认为目标在工作区内
    import app.tools.sandbox as sandbox

    original_ws = sandbox.workspace_dir
    sandbox.workspace_dir = lambda: tmp_path  # type: ignore[assignment]
    try:
        message = undo_stack.restore(snapshot)
    finally:
        sandbox.workspace_dir = original_ws  # type: ignore[assignment]

    assert message.startswith("restored"), message
    assert target.read_bytes() == original, "二进制内容必须逐字节还原"


def test_restore_falls_back_to_text_for_legacy_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    snapshot = Snapshot(
        path=str(target), existed=True, content="hello", tool="write_file", at=0.0
    )
    import app.tools.sandbox as sandbox

    original_ws = sandbox.workspace_dir
    sandbox.workspace_dir = lambda: tmp_path  # type: ignore[assignment]
    try:
        undo_stack.restore(snapshot)
    finally:
        sandbox.workspace_dir = original_ws  # type: ignore[assignment]
    assert target.read_text(encoding="utf-8") == "hello"


def test_restore_refuses_to_write_through_symlink(tmp_path: Path) -> None:
    real = tmp_path / "outside.txt"
    real.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    snapshot = Snapshot(
        path=str(link), existed=True, content="hijacked", tool="write_file", at=0.0
    )
    import app.tools.sandbox as sandbox

    original_ws = sandbox.workspace_dir
    sandbox.workspace_dir = lambda: tmp_path  # type: ignore[assignment]
    try:
        message = undo_stack.restore(snapshot)
    finally:
        sandbox.workspace_dir = original_ws  # type: ignore[assignment]

    assert message.startswith(("failed", "refused")), message
    assert real.read_text(encoding="utf-8") == "outside", "不得透过 symlink 写到别处"


# ── 3. undo：失败可重试（快照不被吞掉）────────────────────────────────────
def test_pop_and_restore_requeues_snapshot_on_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pushed: list[Snapshot] = []
    snapshot = Snapshot(path="/ws/a.txt", existed=True, content="old", tool="write_file", at=0.0)

    async def fake_pop(session_id):  # noqa: ANN001
        return snapshot

    async def fake_push(session_id, snap):  # noqa: ANN001
        pushed.append(snap)
        return True

    monkeypatch.setattr(undo_stack, "pop", fake_pop)
    monkeypatch.setattr(undo_stack, "push", fake_push)
    monkeypatch.setattr(undo_stack, "restore", lambda s: f"failed to restore {s.path}: disk full")

    message, returned = _run(undo_stack.pop_and_restore("s1"))
    assert returned is snapshot
    assert "failed" in message
    assert "可重试" in message
    assert pushed and pushed[0] is snapshot, "失败必须把快照放回栈"


def test_pop_and_restore_does_not_requeue_on_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pushed: list[Snapshot] = []
    snapshot = Snapshot(path="/ws/a.txt", existed=True, content="old", tool="write_file", at=0.0)

    async def fake_pop(session_id):  # noqa: ANN001
        return snapshot

    async def fake_push(session_id, snap):  # noqa: ANN001
        pushed.append(snap)
        return True

    monkeypatch.setattr(undo_stack, "pop", fake_pop)
    monkeypatch.setattr(undo_stack, "push", fake_push)
    monkeypatch.setattr(undo_stack, "restore", lambda s: "restored ok")

    message, _ = _run(undo_stack.pop_and_restore("s1"))
    assert message == "restored ok"
    assert pushed == [], "成功时不该再压回栈"


def test_undo_command_reports_retryable_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`/undo` 必须走 pop_and_restore（否则失败即丢快照）。"""
    import inspect

    from app.commands import builtins as commands

    src = inspect.getsource(commands)
    assert "pop_and_restore" in src
    assert "await undo_stack.pop(session_id)" not in src
