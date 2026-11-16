"""执行规范（先规划 / 该委派就委派 / 清单不空不收工）的回归测试。

这些断言钉住的是**行为约束**，不是文案措辞：措辞会改，但下面几条一旦回退，
第 1 项那个故障就会原样复现（模型把清单当可选风格、从不派子 agent）。
"""

from __future__ import annotations

import pytest

from app.agent.planning_guidance import render_planning_guidance
from app.agent.runtime import AgentTask, _apply_system_prefix


def test_both_capabilities_renders_all_three_rules():
    out = render_planning_guidance(has_todo=True, has_subagent=True)

    assert "Planning and delegation" in out
    assert "todo_write" in out, "必须点名清单工具，否则模型不知道用哪个"
    assert "subagent" in out, "必须点名委派工具"
    # 收工条件：清单不空就不算完
    assert "not finished while the list still has" in out


def test_minimal_mode_without_either_capability_is_byte_identical():
    """极简模式两样都没有 ⇒ 空串。调用点据此保持 prompt 逐字不变。"""
    assert render_planning_guidance(has_todo=False, has_subagent=False) == ""


def test_only_todo_mode_does_not_mention_subagent():
    """只有清单、没有委派能力时，绝不能教模型调不存在的工具。"""
    out = render_planning_guidance(has_todo=True, has_subagent=False)

    assert "todo_write" in out
    assert "subagent" not in out


def test_only_subagent_mode_omits_the_finish_rule():
    """没有清单工具时，"清单不空就不算完"是模型做不到的要求 —— 会换来一轮辩解。"""
    out = render_planning_guidance(has_todo=False, has_subagent=True)

    assert "subagent" in out
    assert "todo_write" not in out
    assert "not finished while the list still has" not in out


def test_concise_never_licenses_skipping_required_steps():
    """`main.py` 的默认 persona 写着 "Reply briefly in Chinese" ——
    必须显式声明它只约束措辞，否则模型会把"简短"读成"少做事"。"""
    out = render_planning_guidance(has_todo=True, has_subagent=True)

    assert "Reply briefly" in out, "要抄真实措辞，否则模型对不上号"
    assert "constrain \nwording only" in out or "wording only" in out
    assert "never mean skipping a step" in out


def test_delegation_rule_names_the_cost_and_the_exclusions():
    """只说"可以委派"不够：模型会为单次工具调用也派 agent，白烧几倍 token。"""
    out = render_planning_guidance(has_todo=True, has_subagent=True)

    assert "Do **not** delegate" in out
    assert "single tool call" in out


@pytest.mark.parametrize(
    ("has_todo", "has_subagent"),
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_rendering_is_pure(has_todo: bool, has_subagent: bool):
    """同一输入两次渲染必须逐字相同 —— 否则前缀缓存每轮都会失效。"""
    first = render_planning_guidance(has_todo=has_todo, has_subagent=has_subagent)
    second = render_planning_guidance(has_todo=has_todo, has_subagent=has_subagent)

    assert first == second


# ── 接线：规范必须真的进 system 段 ────────────────────────────────────────
#
# 上面测的是"渲染对不对"。但第 1 项真正坏过的地方是**没接线** ——
# `PromptEngine.assemble` 写着「工具 / 技能 / RAG / 规划」，却从未被生产路径调用。
# 所以这里必须断言它落到了 `messages` 里，而不是只存在于某个模块的返回值中。

SYSTEM = "You are Chiron. Reply briefly in Chinese."


def _task() -> AgentTask:
    return AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="把这个仓库里的旧 API 全部迁移到新接口",
        system_prompt=SYSTEM,
        max_turns=3,
    )


def _prefix(*, planning_suffix: str = "") -> list[dict[str, object]]:
    return _apply_system_prefix(
        [{"role": "user", "content": "go"}], _task(), planning_suffix=planning_suffix
    )


def test_guidance_lands_in_the_system_message():
    messages = _prefix(planning_suffix=render_planning_guidance(has_todo=True, has_subagent=True))

    assert messages[0]["role"] == "system"
    assert "todo_write" in str(messages[0]["content"])


def test_guidance_sits_at_the_end_of_the_stable_prefix():
    """必须在 persona **之后**：否则 "Reply briefly" 之后又跟一段规范，
    模型容易只记住最近那句 "briefly"，把规范读成可选项。"""
    guidance = render_planning_guidance(has_todo=True, has_subagent=True)
    content = str(_prefix(planning_suffix=guidance)[0]["content"])

    assert content.startswith(SYSTEM)
    assert content.endswith(guidance)
    assert content == f"{SYSTEM}\n\n{guidance}"


def test_guidance_does_not_break_the_cache_breakpoint():
    """规范写在稳定前缀里，断点仍标在同一条消息上 —— 否则每轮整段失效。"""
    messages = _prefix(planning_suffix=render_planning_guidance(has_todo=True, has_subagent=True))

    assert messages[0]["cache_breakpoint"] is True


def test_empty_guidance_keeps_the_prompt_byte_identical():
    """极简模式（没有 todo_write / subagent）⇒ system 段逐字不变。"""
    without = _prefix()
    with_empty = _prefix(planning_suffix=render_planning_guidance(has_todo=False, has_subagent=False))

    assert without == with_empty
    assert without[0]["content"] == SYSTEM
