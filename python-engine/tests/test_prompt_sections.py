"""G-P1：system 段的**命名分段**与**按名 diff**（回归测试）。

**这里钉住什么**

`_apply_system_prefix` 是 system 段的唯一定形点。此前每段内容只是拼进字符串的一段
文本：改一句话只能整段比字符串，失败信息指向整个 system 段；也无从回答"是哪一段变了"。

分段之后有两条硬约束，一旦回退就原样复现上面那两个问题：

1. **逐字节等价** —— 默认配置下渲染出的 system 段必须与改造前**完全相同**。
   这不是风格问题：稳定前缀是各家 provider 前缀缓存的命中依据，多一个分隔符、
   少一个空行，整段缓存就失效。这条比"缓存命中率"更容易测，也更该守。
2. **最小 patch** —— 只改一段 ⇒ diff 只含该段。这条让诊断能直接说出名字，
   而不是"system 变了"。

对位 pi 的 `buildSystemPromptSections` / `diffSystemPromptSections`
（`packages/coding-agent/src/core/system-prompt.ts:127`、`:212`）。
"""

from __future__ import annotations

import pytest

from app.agent.harness_profile import (
    HarnessProfile,
    register_harness_profile,
    reset_harness_profiles,
)
from app.agent.prompt_sections import (
    CONTROLS_SUFFIX,
    GOAL_CONTEXT,
    HARNESS_SUFFIX,
    MEMORY,
    PLANNING_SUFFIX,
    REPORT,
    STABLE_PREFIX,
    TENANT_SUFFIX,
    TOOL_CHANGE_NOTICE,
    PromptSections,
    diff_sections,
    render_inline,
    standalone_texts,
)
from app.agent.runtime import AgentTask, _apply_system_prefix

SYSTEM = "You are Chiron. Reply briefly in Chinese."


def _task(**kwargs) -> AgentTask:
    base = {
        "id": "t",
        "tenant_id": 1,
        "user_id": "u",
        "session_id": "s",
        "content": "hi",
        "system_prompt": SYSTEM,
    }
    base.update(kwargs)
    return AgentTask(**base)


@pytest.fixture(autouse=True)
def _clean_profiles():
    reset_harness_profiles()
    yield
    reset_harness_profiles()


# ── 逐字节等价（约束 1）──────────────────────────────────


def test_inline_matches_pre_refactor_concatenation():
    """inline 段的拼法必须与改造前逐字相同：`"\\n\\n".join` 且跳过空段。

    改造前的实现是逐段 `f"{base}\\n\\n{suffix}"`（只在非空时追加）。两者对空串的处理
    必须一致 —— 否则多出一个尾随分隔符，前缀缓存整段失效。
    """
    sections = PromptSections(
        inline={
            STABLE_PREFIX: "P",
            HARNESS_SUFFIX: "H",
            TENANT_SUFFIX: "T",
            CONTROLS_SUFFIX: "C",
            PLANNING_SUFFIX: "G",
        }
    )

    assert render_inline(sections) == "P\n\nH\n\nT\n\nC\n\nG"


def test_empty_inline_sections_are_skipped_not_rendered_as_blanks():
    """空段必须完全跳过 —— 而不是留下 `\\n\\n\\n\\n` 这种"看起来一样其实变了"的正文。"""
    sections = PromptSections(inline={STABLE_PREFIX: "P", TENANT_SUFFIX: "", CONTROLS_SUFFIX: ""})

    assert render_inline(sections) == "P"


def test_default_task_system_message_is_byte_identical():
    """默认态（无记忆 / 无 hook / 无工具变更 / 空注册表）：只有一条 system，正文逐字等于 prompt。

    这是"引入分段不改任何现有会话行为"的直接断言。
    """
    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], _task())

    systems = [m for m in messages if m.get("role") == "system"]
    assert len(systems) == 1
    assert systems[0]["content"] == SYSTEM
    assert systems[0].get("cache_breakpoint") is True


def test_harness_suffix_is_appended_exactly_as_before():
    """注册了 profile 时，正文与改造前 `apply_harness_suffix` 的结果逐字相同。"""
    register_harness_profile(
        HarnessProfile(
            name="ds",
            model_prefixes=("deepseek-",),
            system_prompt_suffix="DS",
        )
    )
    task = _task(llm_config={"model": "deepseek-v4-flash"})

    messages = _apply_system_prefix([{"role": "user", "content": "hi"}], task)

    systems = [m for m in messages if m.get("role") == "system"]
    assert systems[0]["content"] == f"{SYSTEM}\n\nDS"


def test_standalone_segments_keep_their_message_boundaries():
    """逐轮会变的段仍然各自独立成消息 —— 拼进稳定前缀会让整段缓存失效。"""
    messages = _apply_system_prefix(
        [{"role": "user", "content": "hi"}],
        _task(memory_context="MEM"),
        tool_change_notice="NOTICE",
    )

    systems = [m.get("content") for m in messages if m.get("role") == "system"]
    assert systems[0] == SYSTEM
    assert "MEM" in systems
    assert "NOTICE" in systems
    # 稳定前缀那条仍是唯一带断点的
    assert [m for m in messages if m.get("role") == "system"][0].get("cache_breakpoint") is True


def test_standalone_order_is_goal_memory_hook_report_notice():
    """顺序是语义的一部分：续跑框架 → 记忆 → hook → 汇报 → 工具变更。

    独立段的顺序可以改，但不能"随手改" —— 它是 `STANDALONE_ORDER` 这个单一事实源的产物。
    """
    task = _task(memory_context="MEM", report_context="REP")
    messages = _apply_system_prefix(
        [{"role": "user", "content": "hi"}],
        task,
        tool_change_notice="NOTICE",
    )

    systems = [m.get("content") for m in messages if m.get("role") == "system"]
    assert systems == [SYSTEM, "MEM", "REP", "NOTICE"]


# ── 按名 diff（约束 2）──────────────────────────────────


def test_only_changed_section_appears_in_patch():
    """只改一段 ⇒ patch 只含该段。这是 G-P1 全部诊断价值的落点。"""
    before = {STABLE_PREFIX: "P", MEMORY: "old", TOOL_CHANGE_NOTICE: ""}
    after = {STABLE_PREFIX: "P", MEMORY: "new", TOOL_CHANGE_NOTICE: ""}

    assert diff_sections(before, after) == {MEMORY: "new"}


def test_unchanged_sections_produce_empty_patch():
    """逐字未变 ⇒ 空 patch（不是"包含全部段、值相同" —— 那等于没做 diff）。"""
    same = {STABLE_PREFIX: "P", MEMORY: "M"}

    assert diff_sections(same, dict(same)) == {}


def test_removed_section_maps_to_none():
    """本轮不存在的段用 `None` 表达（= 删除），与"内容变成空串"合流成同一含义。"""
    assert diff_sections({MEMORY: "M"}, {}) == {MEMORY: None}


def test_empty_string_and_missing_key_are_equivalent():
    """`""` 与缺键都表示"这一段没有内容"，不该因为写法不同就判成变化。"""
    assert diff_sections({MEMORY: "M"}, {MEMORY: ""}) == {MEMORY: None}
    assert diff_sections({}, {MEMORY: ""}) == {}


def test_new_section_is_reported():
    """新出现一段同样是变化（技能目录第一次建起来时就是这种形态）。"""
    assert diff_sections({}, {STABLE_PREFIX: "P", MEMORY: "M"}) == {
        STABLE_PREFIX: "P",
        MEMORY: "M",
    }


# ── 分段视图的装配 ──────────────────────────────────────


def test_out_sections_exposes_every_named_segment():
    """`out_sections` 必须填全所有段名（含空段），否则跨轮 diff 会因为"缺键"失真。"""
    out: dict[str, str] = {}

    _apply_system_prefix(
        [{"role": "user", "content": "hi"}],
        _task(memory_context="MEM"),
        extra_suffix="TEN",
        controls_suffix="CTL",
        planning_suffix="PLAN",
        tool_change_notice="NOTICE",
        out_sections=out,
    )

    assert out[STABLE_PREFIX] == SYSTEM
    assert out[TENANT_SUFFIX] == "TEN"
    assert out[CONTROLS_SUFFIX] == "CTL"
    assert out[PLANNING_SUFFIX] == "PLAN"
    assert out[MEMORY] == "MEM"
    assert out[GOAL_CONTEXT] == ""
    assert out[TOOL_CHANGE_NOTICE] == "NOTICE"


def test_out_sections_omitted_is_zero_behavior_change():
    """不传 `out_sections` 时调用方的用法与改造前完全一样（可选参数，不是必需接线）。"""
    plain = _apply_system_prefix([{"role": "user", "content": "hi"}], _task())
    with_out = _apply_system_prefix(
        [{"role": "user", "content": "hi"}], _task(), out_sections={}
    )

    assert plain == with_out


def test_standalone_texts_skips_empty():
    """standalone 段同样跳过空串（空段不能变成一条空 system 消息）。"""
    sections = PromptSections(
        standalone={GOAL_CONTEXT: "", MEMORY: "M", REPORT: "", TOOL_CHANGE_NOTICE: ""}
    )

    assert standalone_texts(sections) == ["M"]


def test_single_section_change_is_isolated_in_full_pipeline():
    """端到端地：只有记忆变了 ⇒ patch 只含 `memory`，稳定前缀不在里面。

    这条把"装配 → 渲染 → diff"串起来，防止分段视图与真实渲染脱节。
    """
    from app.agent.runtime import _build_system_sections

    before = _build_system_sections(_task(memory_context="old")).merged()
    after = _build_system_sections(_task(memory_context="new")).merged()

    assert diff_sections(before, after) == {MEMORY: "new"}
