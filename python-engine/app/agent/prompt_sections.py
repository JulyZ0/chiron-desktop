"""system 段的分段命名与按名 diff（差距 G-P1）。

## 为什么需要

`_apply_system_prefix` 把 system 段定形成「一条稳定前缀 + 若干独立消息」。它能工作，
但每段内容只是**拼进字符串里的一段文本**：没有名字，于是

* 无法单独断言某一段 —— 改一句话，测试只能整段比字符串，失败信息指向整个 system 段，
  看不出是哪一段被动过；
* 无法按名 diff ——「这一段变了」与「整段变了」是同一件事，任何一处变动都表现为
  「前缀全变」；
* 段落顺序散落在函数体的 if 分支里，新增一段要手工挑位置并就地重复解释"为什么放这里"。

pi 的对位做法是 `buildSystemPromptSections` + `diffSystemPromptSections`
（`packages/coding-agent/src/core/system-prompt.ts:127`、`:212`）：每段有名字，按名产出
最小 patch（`null` = 删除该段）。

## 边界：这里**不改语义**

命名与 diff 是表示层的重构，不是协议层改造。`render_inline` 的拼接方式与改造前逐字
相同（`"\\n\\n".join` 非空段），因此默认配置下产出的 system 段与改造前**逐字节相同** ——
前缀缓存命中率不受影响。这条由 `tests/test_prompt_sections.py` 钉住，比"缓存命中率"
更容易测，也更该守。

真正的「只发改动项」需要协议层的 section patch（只有 Anthropic 的 text block 数组
支持按块替换）。本模块只提供**判定**（`diff_sections`），不假设某个 provider 能消费它；
把 sections 带进请求体是后续独立的一步，不属于这里。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# ── 段名（稳定标识，改文案不影响它）────────────────────────

#: 稳定前缀本体（系统提示 + 技能目录）。唯一带 `cache_breakpoint` 的那段。
STABLE_PREFIX = "stable_prefix"
#: 按**模型家族**追加的 harness suffix（`harness_profile`）。
HARNESS_SUFFIX = "harness_suffix"
#: 租户/用户级 harness 塑形（`harness_store`）。
TENANT_SUFFIX = "tenant_suffix"
#: 用户可见的**输出控制**（风格 / 语言偏好，`output_controls`）。
CONTROLS_SUFFIX = "controls_suffix"
#: **执行规范**（先规划 / 该委派就委派 / 清单不空不收工，`planning_guidance`）。
PLANNING_SUFFIX = "planning_suffix"
#: **数据面声明**（外部来源文本是资料、不是指令，`data_plane`）。只依赖本轮工具集是否
#: 含外部来源工具，因此同模式同工具集下逐字稳定 ⇒ 属于稳定前缀。
DATA_PLANE_POLICY = "data_plane_policy"
#: 宿主续跑段的目标上下文（`goal_context`）。
GOAL_CONTEXT = "goal_context"
#: 本轮召回的**记忆块**。
MEMORY = "memory"
#: hook 通过 `additional_context` 提供的参考资料（`hooks/inject`）。
HOOK_CONTEXT = "hook_context"
#: 子 Agent 的主动汇报（`subagent/reporting`）。
REPORT = "report"
#: 工具表变更声明（`tool_change_notice`）。
TOOL_CHANGE_NOTICE = "tool_change_notice"

#: 拼进**同一条** system 消息的段，顺序即拼接顺序。
#: 这几段共同构成"逐字稳定"的那一条 —— 它们只依赖模型家族 / 租户 / 输出偏好 /
#: 工具集有无，同一会话内几乎不变，所以 `cache_breakpoint` 命中的是这一整条。
INLINE_ORDER: tuple[str, ...] = (
    STABLE_PREFIX,
    HARNESS_SUFFIX,
    TENANT_SUFFIX,
    CONTROLS_SUFFIX,
    PLANNING_SUFFIX,
    DATA_PLANE_POLICY,
)

#: **各自独立成一条** system 消息的段，顺序即消息顺序。
#: 这些内容逐轮会变，拼进稳定前缀会让整段缓存失效 —— 所以它们的"边界"就是消息边界。
STANDALONE_ORDER: tuple[str, ...] = (
    GOAL_CONTEXT,
    MEMORY,
    HOOK_CONTEXT,
    REPORT,
    TOOL_CHANGE_NOTICE,
)


@dataclass(frozen=True)
class PromptSections:
    """一轮 system 段的分段视图。

    两个字典都按 `INLINE_ORDER` / `STANDALONE_ORDER` 的语义解释；缺键等同空串
    （"这一段本轮不存在"），因此调用方可以只填非空段。
    """

    inline: dict[str, str] = field(default_factory=dict)
    standalone: dict[str, str] = field(default_factory=dict)

    def merged(self) -> dict[str, str]:
        """按渲染顺序合并成一张「段名 → 内容」表（空段保留为空串）。

        这是 `diff_sections` 的输入形态：缺键与空串语义相同，合并后不必再区分。
        """
        out: dict[str, str] = {}
        for name in INLINE_ORDER:
            out[name] = self.inline.get(name, "")
        for name in STANDALONE_ORDER:
            out[name] = self.standalone.get(name, "")
        return out


def render_inline(sections: PromptSections) -> str:
    """把 inline 各段拼成**一条** system 消息的正文。

    拼接方式必须与 `_apply_system_prefix` 改造前逐字相同：`"\\n\\n".join` 跳过空段。
    这条约束是"G-P1 不改语义"的全部内容 —— 一旦换成别的分隔符，前缀缓存会整段失效。
    """
    return "\n\n".join(
        text for name in INLINE_ORDER if (text := sections.inline.get(name, ""))
    )


def standalone_texts(sections: PromptSections) -> list[str]:
    """把 standalone 各段摊成**独立消息正文**列表（跳过空段）。"""
    return [
        text for name in STANDALONE_ORDER if (text := sections.standalone.get(name, ""))
    ]


def diff_sections(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> dict[str, str | None]:
    """按段名比较两轮 system 段，返回最小 patch。

    值为 `None` 表示该段本轮**不存在**（删除），否则是新的内容。内容未变的段不出现在
    patch 里 —— 这正是"只改一段 ⇒ patch 只含该段"这条验收标准的判定依据。

    空串与"缺键"同等对待：两者都表示"这一段没有内容"，不该因为一个用 `""`、一个不写
    就判成变化（那会让 patch 在语义没变时非空）。
    """
    patch: dict[str, str | None] = {}
    for name in sorted(set(previous) | set(current)):
        before = previous.get(name) or ""
        after = current.get(name) or ""
        if before != after:
            patch[name] = after or None
    return patch


__all__ = [
    "CONTROLS_SUFFIX",
    "GOAL_CONTEXT",
    "HARNESS_SUFFIX",
    "HOOK_CONTEXT",
    "INLINE_ORDER",
    "MEMORY",
    "PLANNING_SUFFIX",
    "PromptSections",
    "REPORT",
    "STABLE_PREFIX",
    "STANDALONE_ORDER",
    "TENANT_SUFFIX",
    "TOOL_CHANGE_NOTICE",
    "diff_sections",
    "render_inline",
    "standalone_texts",
]
