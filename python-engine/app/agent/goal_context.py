"""宿主续跑轮的目标上下文（`workbench_context["goal_enforce"]` 的消费端）。

**为什么需要这一层**：网关有一条按证据判定的续跑链 —— 一轮没做完（债务未清、清单
未完成、或额度用完）就自动再跑一段，并把欠项写进 `content` 当指令发下来。但在引擎侧
那条指令与用户提问**长得一模一样**：都是 `task.content`，都会变成一条 user 消息。

模型因此没有任何依据判断"这是宿主在续跑，不是用户在追问"。实测的后果是它把续跑轮
当成新一轮对话：先复述进度、再重新浏览仓库、把已完成的部分重做一遍 —— 上一段省下的
上下文就这样又烧掉了，长任务于是每段都在原地打转。

这里把网关注入的结构化摘要渲染成一条**独立的 system 消息**，让"我在被宿主续跑"
成为模型一开始就知道的事实，而不是它要从上下文里猜出来的事。

三条约束：

* **不拼进 `system_prompt`**。那是逐字稳定的前缀，各家 provider 的前缀缓存都靠它命中；
  每段都变的续跑摘要拼进去，等于每段换一次前缀（与 `AgentTask.memory_context` 同款原则）。
* **不改 `content`**。指令正文是网关写的、模型逐条执行的那份，引擎只补充"谁在下令"。
* **没有 `goal_enforce` 时渲染空串**，`_apply_system_prefix` 据此零行为变化。
"""

from __future__ import annotations

from typing import Any

#: 渲染出的 system 消息开头。测试与排查都靠这一行认出"这一段是续跑段"。
_HEADER = "## 本轮是宿主自动续跑"


def _as_int(value: Any) -> int:
    """把网关给的计数安全地转成非负整数（坏值当作"不知道"，即 0）。"""
    if isinstance(value, bool):  # bool 是 int 的子类，但 True 不该被当成 1 段
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else 0
    return 0


def render_goal_context(raw: Any) -> str:
    """把 `workbench_context["goal_enforce"]` 渲染成一条 system 消息的正文。

    `raw` 必须是一个**非空 dict** 才算"这一段是续跑段"：网关用 `true` 标记过旧版本，
    这里一并认（只取"是续跑段"这一个事实，不编造轮次与预算）。
    其它任何形状（`None` / `False` / 字符串 / 空 dict）都渲染空串 —— 不是续跑段。
    """
    if raw is None or raw is False:
        return ""
    if isinstance(raw, dict):
        if not raw:
            return ""
    elif raw is not True:
        # 旧版网关传 `true`；其余非 dict 值一律当作"没说"。
        return ""

    meta = raw if isinstance(raw, dict) else {}
    lines = [
        _HEADER,
        "这不是用户的新提问，而是宿主在你上一段没做完之后自动接上的下一段。"
        "用户那句话仍然是这一整条任务的目标，你不需要重新确认需求，也不要把它当成新话题。",
    ]

    round_no = _as_int(meta.get("round"))
    if round_no:
        lines.append(f"这是同一条任务的第 {round_no} 个续跑段。")

    axis = meta.get("budget_axis")
    if isinstance(axis, str) and axis.strip():
        lines.append(
            f"上一段是因为 `{axis.strip()}` 这一档额度用完而中止的 —— 那不是失败，"
            "已完成的改动与工具结果都已保存。新的一段同样有额度上限，所以请优先收尾，"
            "不要重做已完成的部分，也不要为了确认状态而重读整个仓库。"
        )

    pending = _as_int(meta.get("pending"))
    if pending:
        lines.append(
            f"任务清单上还有 {pending} 项没有完成。宿主已把具体欠项写在用户消息里："
            "照着那份清单推进，每完成一项就把它的 status 改成 completed。"
        )

    lines.append(
        "如果这一段的额度再次用完，宿主还会接下一段 —— 你只要如实说明做到哪一步、"
        "还差什么，不需要为中止道歉，也不要假装任务已经完成。"
    )
    return "\n".join(lines)
