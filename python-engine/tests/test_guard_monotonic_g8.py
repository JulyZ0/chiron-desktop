"""G8：守卫只能减权 —— 类型/断言 + 执行侧语义的回归。

修前会怎样：`HookDecision.kind` 是自由字符串，写 `kind="allow"` 不会报错；而
`execute_tool` 只特判 `ask`，其余一律放行 —— 于是一条"允许"决定会**覆盖**前面的
拒绝，且没有任何日志。DSH 用返回类型堵死这条路，这里用"合法集合 + 构造断言 +
执行侧显式分派"达到同样的效果。
"""

from __future__ import annotations

import inspect

import pytest

from app.hooks import manager as hook_manager
from app.hooks.manager import (
    DECISION_ASK,
    DECISION_DENY,
    GUARD_DECISIONS,
    HookDecision,
)


# ── 1. 合法集合：只有 deny / ask ───────────────────────────────────────────
def test_guard_decisions_have_no_allow() -> None:
    assert GUARD_DECISIONS == frozenset({DECISION_DENY, DECISION_ASK})
    assert "allow" not in GUARD_DECISIONS


@pytest.mark.parametrize("kind", ["allow", "permit", "", "DENY", None])
def test_illegal_decision_kind_is_rejected_at_construction(kind: object) -> None:
    with pytest.raises(ValueError):
        HookDecision("reason", kind=kind)  # type: ignore[arg-type]


def test_deny_and_ask_still_work() -> None:
    denied = HookDecision("nope")
    asked = HookDecision("confirm?", DECISION_ASK)
    assert denied.kind == DECISION_DENY
    assert asked.kind == DECISION_ASK
    # 仍然是一个 str（历史调用方按字符串用）
    assert isinstance(denied, str) and str(denied) == "nope"


# ── 2. 执行侧：deny 永远生效，ask 被 approved 抑制但 deny 不会 ─────────────
def test_execute_tool_only_special_cases_ask() -> None:
    """执行侧的分派必须只认 `ask`；任何"允许"值都进不了这个分支。

    这是"守卫只能减权"的执行侧一半：如果将来有人给 HookDecision 加了 allow
    并让 execute_tool 认它，这条断言会先失败。
    """
    from app.agent import tool_execution

    src = inspect.getsource(tool_execution.execute_tool)
    assert "DECISION_ASK" in src
    assert "allow" not in src.lower().replace("allow_approval", ""), (
        "execute_tool 不该出现'allow'决定的分支：守卫只能收紧"
    )


def test_approved_does_not_bypass_deny_documented() -> None:
    """`approved=True` 只能抑制 `ask`，不能翻 `deny` —— 理由必须在代码里写清。"""
    from app.agent import tool_execution

    src = inspect.getsource(tool_execution.execute_tool)
    assert "deny" in src.lower()
    doc = inspect.getdoc(tool_execution.execute_tool) or ""
    assert "deny" in doc.lower() and "ask" in doc.lower()


# ── 3. 历史用法不受影响 ───────────────────────────────────────────────────
def test_existing_callers_construct_with_defaults() -> None:
    """`HookDecision(reason)`（不带 kind）默认就是 deny，老代码照旧。"""
    assert HookDecision("blocked").kind == DECISION_DENY
    # 模块级常量仍指向同样的字符串值
    assert hook_manager.DECISION_DENY == "deny"
