"""G11/G20 后续接线的回归：巡检不得续跑子会话 + 挂起现场有公开读口。

修前会怎样（第一条）：子 Agent 现在有自己的 `agent_runs` 行。进程在子 Agent 跑到一半
崩掉时，reconciler 6 分钟后会把 `sub:<父>:<run>` 当成普通会话接管并**续跑** ——
此时父 run 早已收尾，这个"幽灵运行"会带着写权限继续动工作区，且没有父预算与父上下文。
修前会怎样（第二条）：`suspension.py` 只能读 `AgentRuntime._awaiting_input` 私有属性。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ENGINE / rel).read_text(encoding="utf-8")


# ── 1. 巡检排除子会话 ─────────────────────────────────────────────────────
def test_sweep_query_excludes_subagent_sessions() -> None:
    src = _src("app/agent/resume.py")
    assert "session_id NOT LIKE 'sub:%'" in src, (
        "巡检必须排除 sub:<父>:<run>，否则崩溃窗口里会出现幽灵运行（带写权限续跑一个已收尾的子任务）"
    )


def test_sweep_predicate_sits_in_the_same_where_clause() -> None:
    """排除条件必须落在扫描语句自己的 WHERE 里，而不是注释里"说说而已"。"""
    src = _src("app/agent/resume.py")
    start = src.index("FROM agent_runs")
    window = src[start : start + 900]
    assert "status IN ('running', 'checkpointed', 'resuming')" in window
    assert "session_id NOT LIKE 'sub:%'" in window


# ── 2. 挂起现场只读访问口 ─────────────────────────────────────────────────
def test_runtime_exposes_awaiting_input_info() -> None:
    from app.agent.runtime import AgentRuntime

    assert hasattr(AgentRuntime, "awaiting_input_info"), "跨模块读私有属性不可接受"
    sig = inspect.signature(AgentRuntime.awaiting_input_info)
    assert list(sig.parameters) == ["self"], "只读访问口不该参数化"


def test_awaiting_input_info_returns_none_when_idle_and_copy_when_suspended() -> None:
    from app.agent.runtime import AgentRuntime

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._awaiting_input = None  # type: ignore[attr-defined]
    assert runtime.awaiting_input_info() is None

    state = {"tool_call_id": "c1", "reason": "need approval"}
    runtime._awaiting_input = state  # type: ignore[attr-defined]
    snapshot = runtime.awaiting_input_info()
    assert snapshot == state
    assert snapshot is not state, "必须返回拷贝：调用方改它不能改到运行时状态"


def test_suspension_uses_the_public_accessor() -> None:
    src = _src("app/subagent/suspension.py")
    assert "awaiting_input_info()" in src, "挂起协调应走公开读口"
    # 允许在 fallback 里读私有属性，但不能是唯一路径
    assert src.count("_awaiting_input") <= 2


# ── 3. 配置项存在 ─────────────────────────────────────────────────────────
def test_suspend_wait_setting_exists() -> None:
    src = _src("app/config.py")
    assert "subagent_suspend_wait_seconds" in src

    from app.config import Settings

    field = Settings.model_fields["subagent_suspend_wait_seconds"]
    assert field.default == 900
