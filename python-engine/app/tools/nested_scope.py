"""`run_code` 嵌套工具调用的能力收口（H4）。

**问题**（PRD 9.1 H4）：`run_code` 的子进程此前注入**全部** registry 工具，且每次嵌套
调用在主进程直接 `registry.execute(name, args)` —— 主循环那一整套逐工具授权
（ToolGuard 分级、Tool Broker、人工审批、插件归属校验）在嵌套路径上被整体绕过。
程序里写 `await tools.subagent(...)` / `await tools.use_capability(...)` /
`await tools.delete_file(...)`，效果等于拿到一张"免检通行证"。

**收口三件套，缺一不可**

1. **只注入显式 scoped capabilities**（:data:`RUN_CODE_CAPABILITIES`）。这里用**白名单相加**
   而不是在全部工具上做减法：仓库里新注册一个工具，默认**不进**作用域 —— 漏登记只会
   "少给能力"（模型收到明确的拒绝），不会"多给授权"。
2. **每次嵌套调用重新授权**（:func:`authorize_nested_call`）：先用注入时那一份作用域复核
   名字，再按 :mod:`app.agent.tool_policy` 的**副作用等级**复核参数 —— `delete` /
   `external` 属于"外层主循环本该逐次审批"的动作，嵌套里没有审批面，默认拒绝；只有会话
   显式下发 grant（:data:`GRANT_CONTEXT_KEY`）才放行。名字在作用域内、但参数把它降级成
   危险动作的（`shell_exec(command='rm -rf ...')`）同样在这里被拦下。
3. **拒绝必须留审计**：本模块只负责判定并给出原因，`run_code` 把原因写进
   `app.tools.exec_audit`（`outcome=blocked`）—— 审计要能回答"谁在哪个 run_code 里
   试图调用哪个工具、被哪一条规则拒绝"。

**两个只能收窄的上下文钩子**

- :data:`SCOPE_CONTEXT_KEY`（`run_code_capabilities`）：runtime 按会话/Profile 再收窄一次；
- :data:`GRANT_CONTEXT_KEY`（`run_code_grants`）：会话显式批准的嵌套工具名。

两者都**只能收窄**：即使调用方传入超出白名单的名字，交集运算也会把它削回白名单。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry

#: 会话级收窄钩子（contextvar 里的键）：值为工具名集合，只会与白名单求交。
SCOPE_CONTEXT_KEY = "run_code_capabilities"

#: 会话级显式授权钩子：值为工具名集合，用于放行 `delete` / `external` 级别的嵌套调用。
GRANT_CONTEXT_KEY = "run_code_grants"

#: `run_code` 程序里能看到的**全部**能力 —— 显式声明，且默认拒绝一切未列名者。
#:
#: 分组注释即"为什么它在里面"；**不在**里面的每一族都有明确理由（见下方 _EXCLUDED_*）。
RUN_CODE_CAPABILITIES: frozenset[str] = frozenset(
    {
        # ── 文件读写：路径已由 fs_guard/sandbox 夹在 workspace 内 ──
        "read_file",
        "write_file",
        "edit_file",
        "replace_in_files",
        "read_image",
        "read_archive",
        "file_analyzer",
        "outline_file",
        # ── 检索 / 列目录 ──
        "ls",
        "glob_files",
        "grep_files",
        "search_files",
        # ── 结果与日志回读（只读，且只读自己这一次 run 的产物）──
        "read_tool_result",
        "read_subagent_result",
        "list_subagent_runs",
        "job_output",
        # ── 批量执行：命令本身仍受沙箱白名单 + 本模块的等级复核约束 ──
        "shell_exec",
        # ── git：只读三件套 + 提交（仓库逃逸与配置执行由 git_tools 收口）──
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_commit",
        # ── 上下文 / 知识 / 记忆 ──
        "compact_conversation",
        "kb_list",
        "kb_search",
        "memory_search",
        "recall",
        "remember",
        "todo_write",
        # ── 技能发现（只读；`skill_run` / `skill_install` 不在其中）──
        "skill_list",
        "skill_discover",
    }
)

#: 明确**排除**的能力族与其理由（写在这里是为了让后来者看懂"这是决定，不是遗漏"）：
#:
#: * `delete_file` / `forget` / `job_kill`：不可逆动作，必须走外层逐次审批；
#: * `web_fetch` / `web_search` / `browser_*`：触达外部系统，审批面在外层；
#: * `use_capability` / `mcp_*` / 插件工具：隐藏来源，其授权在代理层与插件归属校验里，
#:   绕过代理直接 `registry.execute` 正是 H24 的另一面；
#: * `subagent` / `rerun_subagent` / `resume_subagent` / `best_of_n`：委派会重开一条带
#:   写权限的执行链，父层至少要确认一次（见 tool_policy 的 WRITE 分组）；
#: * `skill_run` / `skill_install` / `skill_generate`：技能包执行与安装属外部触达；
#: * `persistent_shell` / `execute_python` / `run_code`：另外三条**命令/代码执行**入口，
#:   嵌套它们等于绕过沙箱槽位与递归上限；
#: * `media_create` / `image_generate` / `text_to_speech` / `speech_to_text` /
#:   `video_analyze` / `vision_analyze`：外部媒体服务与计费动作；
#: * `ask_user`：交互面在会话层，子进程里没有可用的问答通道；
#: * `tool_search`：它只改会话工具清单，对一次已固定的 run_code 作用域无意义；
#: * 工作流/PRD 类（`prd_generate` / `requirement_validate` / `task_decompose` /
#:   `tech_design` / `run_in_background`）：编排动作，不是"一次程序化调用"的语义。
_EXCLUDED_FAMILIES_NOTE = (
    "delete_file/forget/job_kill, web_fetch/web_search/browser_*, use_capability/mcp_*/, "
    "subagent/rerun_subagent/resume_subagent/best_of_n, skill_run/skill_install/skill_generate, "
    "persistent_shell/execute_python/run_code, media_*, ask_user, tool_search, workflow/PRD tools"
)


def _context_names(key: str) -> frozenset[str] | None:
    """读上下文里的工具名集合；未设置/形状不对时返回 None（= 不额外收窄）。"""
    from app.tools.context import get_tool_context

    raw = get_tool_context(key, None)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return None
    return frozenset(str(item).strip() for item in raw if str(item).strip())


def scoped_tool_names(available: list[str] | frozenset[str] | None = None) -> frozenset[str]:
    """本次 `run_code` 可以注入的工具名集合。

    ``available`` 是当前注册表里的名字（默认现取）。结果 = 白名单 ∩ 注册表 ∩ 会话收窄。
    交集方向固定：**任何一方都只能减小结果**，不存在"传得更多就拿到更多"的路径。
    """
    names = frozenset(registry.list_names() if available is None else available)
    scope = RUN_CODE_CAPABILITIES & names
    narrowed = _context_names(SCOPE_CONTEXT_KEY)
    if narrowed is not None:
        scope &= narrowed
    return frozenset(scope)


def _level_denial(name: str, args: dict[str, Any] | None) -> str | None:
    """副作用等级复核：delete / external 需要会话显式 grant 才放行。"""
    from app.agent.tool_policy import DELETE, EXTERNAL, tool_level

    try:
        level = tool_level(name, args)
    except Exception:  # noqa: BLE001 — 分级失败按最严处理（fail-closed）
        return f"nested call to '{name}' cannot be leveled; refusing (fail-closed)"
    if level not in {DELETE, EXTERNAL}:
        return None
    grants = _context_names(GRANT_CONTEXT_KEY) or frozenset()
    if name in grants:
        return None
    return (
        f"nested call to '{name}' requires per-tool authorization (level={level}); "
        "run_code does not inherit the outer approval"
    )


def authorize_nested_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    allowed: frozenset[str] | None = None,
) -> str | None:
    """嵌套调用的**重新授权**入口。返回 ``None`` 表示放行，否则返回拒绝原因。

    ``allowed`` 是本次 run 实际注入给子进程的那一份作用域（由 `run_code` 传入）：
    判定必须以"子进程被告知的能力面"为准，而不是"此刻注册表里有什么" ——
    否则 run 期间新注册的工具会出现在判定里却从未注入，两种口径迟早漂移。
    """
    scope = allowed if allowed is not None else scoped_tool_names()
    if name not in scope:
        return (
            f"tool '{name}' is outside the run_code capability scope "
            f"({len(scope)} scoped capabilities available)"
        )
    return _level_denial(name, args)
