"""G1：每条流必须以**恰好一个**终态事件结束（PRD 9.2 / G1）。

修前会怎样：输入护栏、`UserPromptSubmit` hook、流中输出护栏三处拦截分支只发
`guardrail_blocked` 就 `return` —— 没有任何 `done/error/cancelled`。消费方（SDK、
ACP 适配层、评测机）只能靠"连接关闭"判断结束，而这与"断了"无法区分：一次被策略
拦下的请求会被记成"成功但空产出"，或者干脆挂住等终态。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.agent.runtime import AgentRuntime

ENGINE = Path(__file__).resolve().parents[1]
RUNTIME_SRC = (ENGINE / "app/agent/runtime.py").read_text(encoding="utf-8")
MAIN_SRC = (ENGINE / "app/main.py").read_text(encoding="utf-8")


def test_blocked_terminal_is_an_error_not_a_done() -> None:
    """拦截 = 本轮**没有产出**：报成 done 会让成功率/首 token 统计把它算成成功。"""
    runtime = AgentRuntime.__new__(AgentRuntime)  # 不触发 __init__ 的依赖
    event = AgentRuntime._blocked_terminal(runtime, "guardrail_blocked", "tr-1")
    assert event.type == "error"
    assert event.error == "guardrail_blocked"
    assert event.trace_id == "tr-1"


def test_every_guardrail_branch_emits_a_terminal() -> None:
    """三处 `guardrail_blocked` 后面必须出现终态（emit 或 `_blocked_terminal`）。"""
    lines = RUNTIME_SRC.splitlines()
    terminals = ('type="done"', 'type="error"', 'type="cancelled"', "_blocked_terminal(")

    checked = 0
    for index, line in enumerate(lines):
        if 'type="guardrail_blocked"' not in line:
            continue
        window = "\n".join(lines[index : index + 12])
        # 流中输出护栏那一处是 `break` 继续回合，允许交给循环收尾；
        # 其余两处必须当场补终态。
        if "break" in window and "_blocked_terminal" not in window:
            continue
        assert any(marker in window for marker in terminals), (
            f"runtime.py:{index + 1} 的 guardrail_blocked 分支没有终态事件"
        )
        checked += 1
    assert checked >= 2, f"只检查到 {checked} 处拦截分支，模式可能变了"


def test_sse_outlet_synthesizes_missing_terminal() -> None:
    """SSE 出口必须记录终态并把缺失的补上（未来新增早退路径也不会漏）。"""
    assert "terminal_seen = False" in MAIN_SRC
    assert 'in ("done", "error", "cancelled")' in MAIN_SRC
    assert "terminal_missing" in MAIN_SRC, "缺失终态时必须可辨认（不是静默补 done）"


def test_run_resets_terminal_state_per_turn() -> None:
    """`_blocked_terminal` 不依赖实例残留状态（同一 runtime 跑多轮不会串）。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    first = AgentRuntime._blocked_terminal(runtime, "guardrail_blocked", "a")
    second = AgentRuntime._blocked_terminal(runtime, "guardrail_blocked", "b")
    assert first.type == second.type == "error"
    assert first is not second


def test_helper_is_documented_with_reason() -> None:
    """为什么用 error 而不是 done —— 理由必须留在代码里，否则后人会"顺手改回去"。"""
    source = inspect.getsource(AgentRuntime._blocked_terminal)
    assert "没有产出" in source or "no output" in source.lower()
    assert re.search(r"error", source)
