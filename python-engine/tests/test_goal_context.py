"""宿主续跑轮的目标上下文：`workbench_context["goal_enforce"]` 必须**真的被消费**。

这条链上此前有一个空洞：网关每段都打了 `goal_enforce` 标记，但引擎侧**没有任何
消费者** —— 续跑指令与用户提问在模型看来完全一样。于是续跑段被当成新话题：复述进度、
重读仓库、重做已完成的部分，上一段省下的上下文又烧光。

四条约定：

* 是续跑段时，模型能从 **system 段**知道"这不是用户的新提问"（不是靠猜）；
* `system_prompt` **逐字不变** —— 那是前缀缓存的命中条件；
* 非续跑段**零行为变化**（不多出一条 system 消息）；
* 轮次 / 撞墙轴 / 未完成数**如实回灌**，坏值不编造。
"""

from __future__ import annotations

from typing import Any

from app.agent.goal_context import _HEADER, render_goal_context
from app.agent.runtime import AgentTask, _apply_system_prefix

SYSTEM = "SYS PROMPT 逐字稳定"


def _task(goal_enforce: Any = None) -> AgentTask:
    ctx = {} if goal_enforce is None else {"goal_enforce": goal_enforce}
    return AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="继续完成上一轮未满足的义务",
        system_prompt=SYSTEM,
        workbench_context=ctx,
        max_turns=2,
    )


def _shape(task: AgentTask) -> list[dict[str, Any]]:
    return _apply_system_prefix([{"role": "user", "content": task.content}], task)


# ── 渲染 ────────────────────────────────────────────────────────────────


def test_nothing_is_rendered_without_the_marker():
    """不是续跑段 ⇒ 空串。任何形状的"没说"都不该凭空造出一段系统指令。"""
    for raw in (None, False, "", "true", 0, [], {}):
        assert render_goal_context(raw) == "", f"{raw!r} 不该被当成续跑段"


def test_legacy_true_marker_still_counts():
    """旧版网关只传 `true`：仍然要认出"这是续跑段"，只是不编造轮次与预算。"""
    out = render_goal_context(True)

    assert _HEADER in out
    assert "第 " not in out, "没说第几段就不要编"
    assert "额度用完而中止" not in out, "没说撞了哪一档，就不要说它撞了墙"
    assert "还有 " not in out, "没给未完成数就不要报数字"


def test_renders_round_axis_and_pending():
    out = render_goal_context({"round": 3, "budget_axis": "tokens", "pending": 2})

    assert "第 3 个续跑段" in out
    assert "tokens" in out
    assert "额度用完" in out and "不是失败" in out.replace("那不是失败", "不是失败")
    assert "还有 2 项" in out


def test_bad_values_are_not_trusted():
    """坏值当作"不知道"：宁可少说一句，也不能给模型一个编出来的数字。"""
    out = render_goal_context({"round": "3", "budget_axis": 42, "pending": -1})

    assert "第 " not in out
    assert "额度用完" not in out
    assert "还有 " not in out


def test_booleans_are_not_counts():
    """`round: true` 是 JSON 里常见的手滑，不能被当成"第 1 段"。"""
    assert "第 " not in render_goal_context({"round": True, "pending": True})


# ── 注入形态 ────────────────────────────────────────────────────────────


def test_continuation_round_is_stated_in_the_system_segment():
    msgs = _shape(_task({"round": 1, "budget_axis": "tokens", "pending": 2}))

    assert [m["role"] for m in msgs] == ["system", "system", "user"]
    assert msgs[0]["content"] == SYSTEM, "续跑摘要**不许**混进稳定前缀"
    assert msgs[0].get("cache_breakpoint") is True, "缓存断点仍标在稳定前缀上"
    assert _HEADER in msgs[1]["content"]
    assert "tokens" in msgs[1]["content"]
    assert msgs[-1] == {"role": "user", "content": "继续完成上一轮未满足的义务"}


def test_normal_round_changes_nothing():
    """普通轮：消息形态与引入前**完全一致**（零行为变化）。"""
    for raw in (None, False, {}):
        msgs = _shape(_task(raw))
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert msgs[0]["content"] == SYSTEM


def test_prefix_stays_byte_identical_across_continuation_rounds():
    """两段不同的续跑（轮次/欠项都变）⇒ 第一条 system 消息逐字相同。

    否则每段都换一次前缀，整段提示词缓存白攒 —— 那正是这条链想省的那笔钱。
    """
    first = _shape(_task({"round": 1, "budget_axis": "tokens", "pending": 3}))
    second = _shape(_task({"round": 2, "budget_axis": "steps", "pending": 1}))

    assert first[0] == second[0]
    assert first[1] != second[1], "摘要本来就该变，只是不许污染前缀"


def test_goal_context_is_ordered_before_memory():
    """续跑摘要排在记忆之前：先知道"这一段是接上的"，再读记忆。"""
    task = _task({"round": 1})
    task.memory_context = "记忆正文 MEM"

    bodies = [m["content"] for m in _shape(task) if m["role"] == "system"]

    assert len(bodies) == 3
    assert _HEADER in bodies[1]
    assert "MEM" in bodies[2]


def test_goal_marker_is_not_frozen_into_the_checkpoint():
    """`goal_enforce` **不进** checkpoint 快照。

    它是"这一段是宿主接上的"这个**一次性**事实：轮次 / 撞墙轴 / 未完成数都只对当时
    那一段成立。冻进快照的话，续跑恢复出来的第一轮会带着上一段的数字继续自称
    "第 N 个续跑段"，而网关那边已经下发了新的摘要。
    """
    from app.agent.runtime import _resume_task_meta

    task = _task({"round": 2, "budget_axis": "tokens", "pending": 1})
    task.workbench_context["kb_id"] = "kb1"

    meta = _resume_task_meta(task)

    assert "goal_enforce" not in meta["workbench_context"], "一次性事实不该进快照"
    assert meta["workbench_context"]["kb_id"] == "kb1", "其余工作台上下文仍要保留"
    assert "goal_enforce" in task.workbench_context, "剔除只作用于快照，不改 task 本身"
