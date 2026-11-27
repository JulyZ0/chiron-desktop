"""system 段的分段装配（从 `runtime._build_system_sections` 整块搬出，P2-1）。

## 这里只做装配，不做判断

system 段的**唯一定形点**是 `runtime._apply_system_prefix`：它先摘掉已有的 system
消息，再按「稳定前缀 → 独立段」的顺序插回去。哪些段该出现、出现时写什么，判断都在
调用方（那里有为什么这么排的完整理由）；本模块只负责把给定的内容摆进**有名字的段**里，
让"为什么放这里"与"这里有什么"分开。

## 为什么要有名字

此前每段内容只是拼进字符串里的一段文本 —— 改一句话只能整段比字符串，测试失败信息
指向整个 system 段，看不出是哪一段被动过；也无从产出"只改了这一段"的 patch。

分段边界与抽取前**完全一致**（`render_inline` + `standalone_texts` 的产物逐字节相同），
`runtime` 以 `_build_system_sections` 的名字再导出，既有调用与测试一字不改。
"""

from __future__ import annotations

from app.agent.goal_context import render_goal_context
from app.agent.harness_profile import match_harness_profile
from app.agent.prompt_sections import (
    CONTROLS_SUFFIX,
    DATA_PLANE_POLICY,
    GOAL_CONTEXT,
    HARNESS_SUFFIX,
    HOOK_CONTEXT,
    KNOWLEDGE,
    MEMORY,
    NARRATION_SUFFIX,
    PERSONA,
    PLANNING_SUFFIX,
    REPORT,
    STABLE_PREFIX,
    TENANT_SUFFIX,
    TOOL_CHANGE_NOTICE,
    PromptSections,
)
from app.agent.runtime_types import AgentTask


def build_system_sections(
    task: AgentTask,
    *,
    extra_suffix: str = "",
    controls_suffix: str = "",
    planning_suffix: str = "",
    narration_suffix: str = "",
    tool_change_notice: str = "",
    data_plane_policy: str = "",
) -> PromptSections:
    """把本轮 system 段拆成**有名字**的分段视图（差距 G-P1）。

    为什么要有名字：`_apply_system_prefix` 是 system 段的唯一定形点，此前每段内容只是
    拼进字符串里的一段文本 —— 改一句话只能整段比字符串，测试失败信息指向整个 system 段，
    看不出是哪一段被动过；也无从产出"只改了这一段"的 patch。

    分段边界与改造前**完全一致**（`INLINE_ORDER` 是那条稳定前缀内部的拼接顺序，
    `STANDALONE_ORDER` 是各自独立成消息的顺序），因此本函数只是表示层重构：
    `render_inline` + `standalone_texts` 的产物与改造前**逐字节相同**。

    各段的取舍理由留在调用处（`_apply_system_prefix` 的注释里），这里只负责装配 ——
    让"为什么放这里"与"这里有什么"分开，新增一段时不必再读一遍全部理由。
    """
    inline: dict[str, str] = {}
    if task.system_prompt:
        # B1：按**模型家族**追加 harness suffix。注册表默认为空 ⇒ 恒等变换。
        # 这里显式取出 suffix 而不是调 `apply_harness_suffix` 拼成一个字符串，是为了让它
        # 成为**独立可比的段**：换模型导致 suffix 变化时，diff 只说 `harness_suffix` 变了。
        profile = match_harness_profile(
            str((task.llm_config or {}).get("model", "") or "")
        )
        inline[STABLE_PREFIX] = task.system_prompt
        # 人格（后台「人格管理」）：服务端按 id/默认解析后随 context 下发。
        # 空 = 未选人格 ⇒ 不产出该段，system 段与引入前逐字相同。
        persona = (task.workbench_context or {}).get("persona")
        if isinstance(persona, dict):
            persona_prompt = str(persona.get("prompt") or "").strip()
            if persona_prompt:
                persona_name = str(persona.get("name") or "").strip()
                inline[PERSONA] = (
                    f"## 人格：{persona_name}\n\n{persona_prompt}"
                    if persona_name
                    else f"## 人格\n\n{persona_prompt}"
                )
        if profile is not None and profile.system_prompt_suffix:
            inline[HARNESS_SUFFIX] = profile.system_prompt_suffix
        # P1-④：租户/用户级 harness 塑形 —— deepagents **没有**这一层（只有部署级注册）。
        # 空串 = 未配置 / 未启用 ⇒ 逐字不变。位置在注册表 suffix **之后**、记忆之前，
        # 因此它落在**稳定前缀的末尾**：同租户同模型下逐字稳定 ⇒ 前缀缓存仍然命中。
        inline[TENANT_SUFFIX] = extra_suffix
        # 差距B：用户可见的**输出控制**（风格 / 语言偏好）。默认态（default + auto）为空串 ⇒
        # system 段与引入前**逐字相同**；非默认时它落在**稳定前缀的末尾**（同会话同选择下
        # 逐字稳定 ⇒ 前缀缓存仍命中）。
        inline[CONTROLS_SUFFIX] = controls_suffix
        # 差距E：**执行规范**（先规划 / 该委派就委派 / 清单不空不收工）。同位置 ——
        # 稳定前缀的**末尾**：同模式同工具集下逐字稳定 ⇒ 前缀缓存仍命中；而它盖在
        # persona 之上，所以 "Reply briefly" 那类措辞压不住"该做的步骤别省"。
        # 空串 = 该模式没有 todo_write / subagent ⇒ 逐字不变。
        inline[PLANNING_SUFFIX] = planning_suffix
        # 回合间叙述（后台「Agent 运行配置」的 narration）：``off``（默认）为空串 ⇒
        # system 段与引入前**逐字相同**；非默认时同位置 —— 稳定前缀末尾，同选择下逐字
        # 稳定 ⇒ 前缀缓存仍命中。
        inline[NARRATION_SUFFIX] = narration_suffix
        # 数据面声明（autoloop §1）：只在**本轮工具集真的含外部来源工具**时非空。
        # 与上面几段同位置 —— 稳定前缀末尾，同模式同工具集下逐字稳定 ⇒ 缓存仍命中；
        # 不含外部工具的会话逐字不变。
        inline[DATA_PLANE_POLICY] = data_plane_policy

    standalone: dict[str, str] = {}
    # 宿主续跑段的目标上下文（`workbench_context["goal_enforce"]`）。**独立成 system 消息**、
    # 位置在稳定前缀之后：续跑摘要每段都不同，拼进 `system_prompt` 会让逐字稳定前缀的缓存
    # 整段失效。它排在记忆**之前**，因为"这一段是宿主接上的"是解读后面所有上下文的框架。
    # 非续跑段渲染空串 ⇒ 零行为变化。
    standalone[GOAL_CONTEXT] = render_goal_context(
        (task.workbench_context or {}).get("goal_enforce")
    )
    standalone[MEMORY] = task.memory_context
    # 本轮知识库检索结果（用户 `@` 选中的库）。**空串 = 没选库**，与"选了但没命中"
    # 严格区分 —— 后者注入的是一段"已检索、无命中"的说明（见 knowledge_context 的注释）。
    standalone[KNOWLEDGE] = getattr(task, "knowledge_context", "") or ""
    # 批 G+ 批次 4：hook 的**上下文注入**。与记忆同样独立成消息，且只走**结构化字段**
    # （`additional_context`），不消费 stdout 正文 —— 见 `app/hooks/inject.py`。
    # 默认关 ⇒ 渲染返回空串 ⇒ 这里零行为变化。
    from app.hooks.inject import render_hook_context

    standalone[HOOK_CONTEXT] = render_hook_context(getattr(task, "hook_contexts", None))
    # 子 Agent 的主动汇报（`app/subagent/reporting.py`）。汇报是"自上次交互以来结束的
    # 子任务"，每轮都可能不同，拼进稳定前缀会让整段前缀缓存失效。空串 ⇒ 零行为变化。
    standalone[REPORT] = getattr(task, "report_context", "") or ""
    # 差距 G-P3：**工具表变更声明**。位置在 system 段**末尾**，因为它是"这一轮你看到的
    # 工具表"的即时说明，离本轮请求越近越不容易被前面的框架段落冲淡。
    # 空串 ⇒ 工具表未变 ⇒ system 段与引入前**逐字相同**（前缀缓存照常命中）。
    standalone[TOOL_CHANGE_NOTICE] = tool_change_notice
    return PromptSections(inline=inline, standalone=standalone)
