"""H9 回归：取消必须传播到**当前调用**（模型请求 / 工具调用 / 子进程 / 子 Agent）。

本文件钉住四件事，逐条对 PRD 9.2 的 H9 验收：

1. **模型请求可取消**：长流在取消后**真的断开**，而不是把这一段听完；
2. **工具调用可取消**：工具在收敛期内真的停下（观察到 `CancelledError`），并且
   取消后这一轮按"已取消"收尾（`cancelled` + `done`），不会把半截调用当成功；
3. **子进程可取消**：父调用被取消时子进程被确实杀掉（`returncode` 落地），不留孤儿；
4. **结果语义**：已经可能产生外部动作的调用返回 `outcome=unknown / result_unknown=True`
   —— **不伪装成功**；只读调用才允许说"取消了、没副作用"；
5. **最长收敛时间**：连"拒绝停下"的工具也不会把调用方无限挂住。

为了让"真的停"可被观测，探针一律记录**真实**的发生时刻/状态，不看返回值自述。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agent import interrupt
from app.agent.cancellation import (
    cancelled_result,
    convergence_seconds,
    poll_interval_seconds,
    terminate_grace_seconds,
)
from app.agent.interrupt import request_interrupt_local
from app.agent.runtime import AgentRuntime, AgentTask
from app.gateway.provider import ChatResponse

#: 断言用的额外宽限：收敛时间是**上界**，调度抖动不该让用例变脆。
_SLACK_SECONDS = 1.5


@pytest.fixture(autouse=True)
def _clean_local_flags():
    """`interrupt._LOCAL` 是模块级状态，跨用例会串（与 test_agent_interrupt 同一口径）。"""
    interrupt._LOCAL.clear()
    yield
    interrupt._LOCAL.clear()


def _task(session_id: str, *, max_turns: int = 1) -> AgentTask:
    return AgentTask(
        id=f"run-{session_id}",
        tenant_id="t",
        user_id="u",
        session_id=session_id,
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=max_turns,
    )


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {
        "id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


# ── 收敛口径本身：配置项必须自洽（轮询 + 宽限 ≤ 收敛上界）─────────────────


def test_convergence_budget_is_self_consistent():
    assert poll_interval_seconds() <= convergence_seconds()
    assert poll_interval_seconds() + terminate_grace_seconds() <= convergence_seconds() + 1e-9


# ── 1. 模型请求：长流在取消后真的断开 ────────────────────────────────────


async def test_model_request_is_cancelled_mid_stream():
    session_id = "s-cancel-stream"
    chunks = {"n": 0}
    gateway = MagicMock()

    async def fake_stream(**_kwargs: Any):
        while True:
            chunks["n"] += 1
            yield ChatResponse(content="x", finish_reason=None)
            await asyncio.sleep(0.02)

    gateway.chat_stream = fake_stream
    runtime = AgentRuntime(gateway=gateway)
    task = _task(session_id)

    first_text = asyncio.Event()
    events: list[Any] = []

    async def consume() -> None:
        async for event in runtime.run(task):
            events.append(event)
            if event.type == "text" and not first_text.is_set():
                first_text.set()

    runner = asyncio.create_task(consume())
    await asyncio.wait_for(first_text.wait(), timeout=10)

    started = time.monotonic()
    request_interrupt_local(session_id)
    await asyncio.wait_for(runner, timeout=convergence_seconds() + 10)
    elapsed = time.monotonic() - started

    types = [event.type for event in events]
    assert "cancelled" in types, "取消要明确告诉下游，不是静默断流"
    assert types[-1] == "done", "只发 cancelled 会让下游 SSE 挂住"
    assert "tool_call" not in types, "取消后不得继续下发/执行半截工具调用"
    assert elapsed <= convergence_seconds() + _SLACK_SECONDS, (
        f"收敛太慢：{elapsed:.2f}s > {convergence_seconds():.2f}s + slack"
    )

    # 探针：流**真的**停了（不是"我们不再读它，而它还在产 chunk"）。
    produced = chunks["n"]
    await asyncio.sleep(0.3)
    assert chunks["n"] == produced, "取消后模型流仍在产出 chunk —— 流没有断开"


# ── 2/4. 工具调用：收敛期内停下 + 有副作用可能时结果是 unknown ─────────────


async def test_tool_call_stops_within_convergence_and_reports_unknown():
    session_id = "s-cancel-tool"
    runtime = AgentRuntime(gateway=MagicMock())
    task = _task(session_id)

    started = asyncio.Event()
    stopped_at: list[float] = []
    side_effect_applied: list[str] = []

    async def slow_dispatch(tool_call: dict[Any, Any], _task: Any) -> dict[Any, Any]:
        side_effect_applied.append(str(tool_call.get("name")))  # 已经动手了
        started.set()
        try:
            while True:
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            stopped_at.append(time.monotonic())
            raise

    runtime._dispatch_tool = slow_dispatch  # type: ignore[method-assign]
    call = _call("write_file", {"path": "notes.txt", "content": "hello"})

    runner = asyncio.create_task(runtime._execute_tool(call, task))
    await asyncio.wait_for(started.wait(), timeout=10)

    cancelled_at = time.monotonic()
    request_interrupt_local(session_id)
    result, ask_reason = await asyncio.wait_for(
        runner, timeout=convergence_seconds() + 10
    )

    assert ask_reason is None
    assert isinstance(result, dict)
    assert side_effect_applied == ["write_file"], "探针前置：工具确实已经跑起来了"
    assert stopped_at, "工具必须真的收到取消（观察到 CancelledError）"
    assert stopped_at[0] - cancelled_at <= convergence_seconds() + _SLACK_SECONDS

    # 结果语义：**不是成功**，是"未知"
    assert result.get("cancelled") is True
    assert result.get("outcome") == "unknown"
    assert result.get("result_unknown") is True
    assert "unknown" in str(result.get("error", "")).lower()
    assert "exit_code" not in result and "stdout" not in result
    assert result.get("ok") is None and result.get("success") is None


async def test_read_only_tool_cancel_does_not_claim_unknown_result():
    session_id = "s-cancel-read"
    runtime = AgentRuntime(gateway=MagicMock())
    task = _task(session_id)

    started = asyncio.Event()
    stopped_at: list[float] = []

    async def slow_read(tool_call: dict[Any, Any], _task: Any) -> dict[Any, Any]:
        started.set()
        try:
            while True:
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            stopped_at.append(time.monotonic())
            raise

    runtime._dispatch_tool = slow_read  # type: ignore[method-assign]
    call = _call("read_file", {"path": "notes.txt"})

    runner = asyncio.create_task(runtime._execute_tool(call, task))
    await asyncio.wait_for(started.wait(), timeout=10)
    request_interrupt_local(session_id)
    result, _ = await asyncio.wait_for(runner, timeout=convergence_seconds() + 10)

    assert stopped_at, "只读调用同样必须在收敛期内停下"
    assert result.get("cancelled") is True
    assert result.get("outcome") == "cancelled"
    assert result.get("result_unknown") is False


def test_cancelled_result_for_side_effecting_tool_is_unknown():
    """纯语义探针：可能产生外部动作的级别一律 unknown（含未声明工具 → 保守归 write）。"""
    unknown = cancelled_result("write_file", level="write", stopped=True, elapsed=0.1)
    assert unknown["result_unknown"] is True and unknown["outcome"] == "unknown"

    still_running = cancelled_result("read_file", level="read", stopped=False, elapsed=9.0)
    assert still_running["result_unknown"] is True, "没停下来的调用不能说'没副作用'"


# ── 5. 收敛上界：连"拒绝停下"的工具都不会挂住调用方 ─────────────────────


async def test_stubborn_tool_cannot_hang_the_caller_beyond_convergence():
    session_id = "s-cancel-stubborn"
    runtime = AgentRuntime(gateway=MagicMock())
    task = _task(session_id)

    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn(tool_call: dict[Any, Any], _task: Any) -> dict[Any, Any]:
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                # 模拟"卡在不可取消的 C 调用里"：吞掉取消继续跑
                continue
        return {}

    runtime._dispatch_tool = stubborn  # type: ignore[method-assign]
    call = _call("write_file", {"path": "notes.txt", "content": "hello"})

    runner = asyncio.create_task(runtime._execute_tool(call, task))
    await asyncio.wait_for(started.wait(), timeout=10)
    cancelled_at = time.monotonic()
    request_interrupt_local(session_id)
    try:
        result, _ = await asyncio.wait_for(runner, timeout=convergence_seconds() + 10)
    finally:
        release.set()

    elapsed = time.monotonic() - cancelled_at
    assert elapsed <= convergence_seconds() + _SLACK_SECONDS
    assert result.get("outcome") == "unknown"
    assert result.get("result_unknown") is True


# ── 3. 子进程：父调用取消时子进程真的被杀掉 ─────────────────────────────


# 用真实子进程做探针：不连 Redis/PG、不碰网络，本机确定性可复现（与
# `test_run_code.py` / `test_git_tools_security.py` 同一条口径，因此**不打**
# `integration` —— 那个标记在本仓库意味着"需要真 PostgreSQL"，会让探针被跳过。）
async def test_subprocess_is_killed_when_parent_call_is_cancelled():
    from app.agent.cancellation import communicate_or_cancel

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def await_output() -> None:
        await communicate_or_cancel(proc, timeout=60)

    runner = asyncio.create_task(await_output())
    await asyncio.sleep(0.3)  # 让子进程真的起来
    assert proc.returncode is None

    started = time.monotonic()
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    elapsed = time.monotonic() - started

    assert proc.returncode is not None, "父调用已取消，子进程还活着 —— 孤儿进程"
    assert elapsed <= convergence_seconds() + _SLACK_SECONDS
