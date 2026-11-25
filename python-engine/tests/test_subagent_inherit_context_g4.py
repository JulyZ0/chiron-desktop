"""G4：`inherit_context` 真的生效（父历史真的有界传下去）。"""

from __future__ import annotations

import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ENGINE / rel).read_text(encoding="utf-8")


# ── 快照助手本身 ───────────────────────────────────────────────────────────
def test_snapshot_keeps_roles_and_drops_unknown() -> None:
    from app.agent.runtime import _parent_message_snapshot

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "not a chat role"},
        {"role": "assistant", "content": "yo"},
        "not-a-dict",
    ]
    snap = _parent_message_snapshot(msgs)
    assert [m["role"] for m in snap] == ["user", "assistant"]


def test_snapshot_is_bounded_in_count_and_length() -> None:
    from app.agent.runtime import _PARENT_SNAPSHOT_MAX_MESSAGES, _parent_message_snapshot

    many = [{"role": "user", "content": "x"} for _ in range(200)]
    assert len(_parent_message_snapshot(many)) == _PARENT_SNAPSHOT_MAX_MESSAGES

    long_msg = [{"role": "assistant", "content": "y" * 50_000}]
    assert len(_parent_message_snapshot(long_msg)[0]["content"]) == 8000


def test_snapshot_tolerates_missing_input() -> None:
    from app.agent.runtime import _parent_message_snapshot

    assert _parent_message_snapshot(None) == []
    assert _parent_message_snapshot([]) == []


# ── 打通：runtime 写、工具读 ───────────────────────────────────────────────
def test_runtime_publishes_parent_messages_each_turn() -> None:
    src = _src("app/agent/runtime.py")
    assert "parent_messages=_parent_message_snapshot(messages)" in src, (
        "每轮必须把父历史快照放进 tool context，否则工具层拿不到"
    )


def test_subagent_tool_passes_parent_messages_to_runner() -> None:
    """修前：`SubAgentRunner(...)` 从不传 `parent_messages` ⇒ 开关空转。"""
    src = _src("app/tools/subagent.py")
    call_sites = [
        m.start() for m in re.finditer(r"inherit_context=inherit_context,", src)
    ]
    assert call_sites, "找不到 runner 构造点"
    for pos in call_sites:
        window = src[pos : pos + 600]
        assert "parent_messages=" in window, (
            "每个 SubAgentRunner 构造点都必须在传 inherit_context 的同时传 parent_messages"
        )
        assert 'get_tool_context("parent_messages"' in window


def test_inherit_module_still_owns_redaction() -> None:
    """工具层只做有界快照；脱敏与字符预算仍在 inherit.py（两处都能省，只有一处能保证安全）。"""
    inherit_src = _src("app/subagent/inherit.py")
    assert "build_inherited_context" in inherit_src
    assert "redact" in inherit_src.lower()
