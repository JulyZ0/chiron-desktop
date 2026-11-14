"""长任务的**执行规范**：先规划、该委派就委派、清单不空就不收工。

## 为什么需要单独一段，而不是写进工具 description

工具的 `description` 只在模型**决定要看工具**时才有影响，而"什么时候该建清单"
恰恰是模型在**动手之前**要做的判断。两侧都写才算闭环：工具描述回答"怎么调"，
这段回答"什么时候必须调"。

## 为什么放在这里而不是 `prompt_engine`

`PromptEngine.assemble()` 曾是唯一拼接「可用工具 / 技能 / RAG / 规划」的地方，
但生产路径（`main.py` → `AgentRuntime.run`）**从未实例化它** —— 相关代码只在
`tests/` 里被调用过。于是默认 system prompt 实际只剩下 `main.py` 里那句
"You are Chiron. Reply briefly in Chinese."，里面既没有规划引导也没有委派引导。
模型因此把 `todo_write` 当成可选风格，长任务做到一半就跑偏；`subagent` 尽管在
工具白名单里，却从未被主动唤起。

## 为什么按可用工具过滤

极简模式只放 `read_file` / `edit_file` / `shell_exec`。此时提 `todo_write`、
`subagent` 就是在教模型去调不存在的工具 —— 反而诱发无效调用与"能力缺失"的道歉。
两个开关都为假时渲染空串，保证该模式的行为与引入前**逐字相同**。
"""

from __future__ import annotations

_HEADER = "## Planning and delegation"

_PLAN_RULE = (
    "- **Plan before you act, when the work needs more than about three steps or "
    "touches more than one file.** Call `todo_write` once with the full list, then keep "
    "the same list current: mark an item `in_progress` when you start it and switch it "
    "to `completed` the moment it is actually done. Do not re-plan on every message and "
    "do not batch several completions into one late update — the list is what you "
    "re-read to know what is still owed."
)

_DELEGATE_RULE = (
    "- **Delegate work that is independent and self-contained.** Use `subagent` for side "
    "quests that would otherwise flood this conversation with material you will not need "
    "afterwards: reading and summarising several files, researching an area you know "
    "nothing about, drafting a document, or a read-only review pass. Give it the goal and "
    "the constraints, not a script. Do **not** delegate work on your own critical path, "
    "work that needs the context you already hold, or a task that is a single tool call — "
    "the handoff costs more than it saves."
)

_FINISH_RULE = (
    "- **A turn is not finished while the list still has `pending` or `in_progress` "
    "items.** If you are blocked, say which item is blocked and what you need, instead of "
    "stopping quietly. Reporting a partial result as if it were complete is worse than "
    "reporting that you stopped."
)

_CONCISE_NOTE = (
    '- Instructions to be brief ("Reply briefly", "be concise", "简短回答") constrain '
    "wording only. They never mean skipping a step you already know the task needs."
)


def render_planning_guidance(*, has_todo: bool, has_subagent: bool) -> str:
    """渲染执行规范；两个能力都没有时返回空串（调用点据此保持原始 prompt 不变）。

    参数是两个**能力开关**而不是模式名：模式的工具集可由 `mode_overrides.json`
    覆盖，按模式名判断会在覆盖之后与实际工具清单不一致。
    """
    rules: list[str] = []
    if has_todo:
        rules.append(_PLAN_RULE)
    if has_subagent:
        rules.append(_DELEGATE_RULE)
    if not rules:
        return ""
    # 收工条件只对"有清单"的模式成立：没有 todo_write 时模型无从表达"剩余项"，
    # 硬要求它做不到的事只会换来一轮辩解。
    if has_todo:
        rules.append(_FINISH_RULE)
    rules.append(_CONCISE_NOTE)
    return "\n\n".join([_HEADER, "\n\n".join(rules)])


__all__ = ["render_planning_guidance"]
