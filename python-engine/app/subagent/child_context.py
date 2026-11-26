"""子 Agent 首轮上下文组装（差距 G30）。

## 修前会怎样

子 Agent 的第一条消息里只有任务文本（外加可选的继承 / 续跑上下文）。它读不到三件事：

1. **工作目录**是哪个绝对路径 —— 于是它按自己的猜测拼相对路径；
2. **文件策略**是只读还是可写 —— 只读会话里它反复 `write_file` 被拒（每一次都是一整次
   往返，用户看到的是"子 Agent 莫名失败"）；可写会话里它又常自我推断"没被授权"，
   明明能改却只写一份报告；
3. **审批模式**与**自己的权限面在启动时就固定了** —— 审批被自动拒绝时它重试同一个操作，
   而不是把限制写进结论让派发方处理。

## 注入通道

这四段（加上"委派与返回"）走**首条用户消息**，不进 `system_prompt`：
父子 system prompt 必须逐字一致，否则"子 Agent 是同一人格的延伸"这件事在提示词层面
就不成立了（DSH 同样把这些走 runtime context）。工作目录、沙箱、审批随**会话**变化，
而 system prompt 是**人格**，两者生命周期不同。

## 顺序

按 :data:`SLOT_ORDER` 的固定序号拼装（100 工作目录 → 110 文件策略 → 115 审批 →
120 身份 → 125 委派）。序号与 DSH 的 `dsh-system-prompt` 顺序常量对齐，调用方**不能**
靠传参顺序决定拼装顺序 —— 顺序是契约的一部分（先读到环境，再读到自己的权限，
最后读到"这一轮怎么收尾"）。

## 与 `app/subagent/inherit.py` 的区别

`inherit.py` 渲染的是**父会话的消息**（可能很长、需要脱敏与配额）；这里渲染的是
**当前环境的事实**（短、无隐私、可枚举）。两者都用"标记块 + 当数据不当指令"的外形，
但来源不同，不能合并：环境段的每个字都是我们写的，继承段的每个字都是历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 环境段的外层标记。子 Agent 侧据此知道"这是运行环境，不是任务"。
CONTEXT_TAG = "child-context"

#: 沙箱档位（与 DSH 的 `SandboxPolicy` 三档同名；也是**唯一**三档）。
SANDBOX_READ_ONLY = "read-only"
SANDBOX_WORKSPACE_WRITE = "workspace-write"
SANDBOX_DANGER_FULL_ACCESS = "danger-full-access"

#: 各槽位的标题。测试与模型都按这些字符串定位段落，改文案等于改契约。
HEADING_WORKING_DIRECTORY = "## 工作目录"
HEADING_SANDBOX_POLICY = "## 文件与执行策略"
HEADING_APPROVAL_POLICY = "## 审批模式"
HEADING_IDENTITY = "## 你的身份与范围"
HEADING_DELEGATION = "## 委派与返回"

SLOT_WORKING_DIRECTORY = "working_directory"
SLOT_SANDBOX = "sandbox"
SLOT_APPROVAL = "approval"
SLOT_IDENTITY = "identity"
SLOT_DELEGATION = "delegation"

#: 固定序号 → 槽位名。序号与 DSH 的顺序常量对齐；拼装顺序**只由它决定**。
SLOT_ORDER: tuple[tuple[int, str], ...] = (
    (100, SLOT_WORKING_DIRECTORY),
    (110, SLOT_SANDBOX),
    (115, SLOT_APPROVAL),
    (120, SLOT_IDENTITY),
    (125, SLOT_DELEGATION),
)

#: 槽位名 → 标题（`SLOT_ORDER` 的每个 key 都必须在这里）。
SLOT_HEADINGS: dict[str, str] = {
    SLOT_WORKING_DIRECTORY: HEADING_WORKING_DIRECTORY,
    SLOT_SANDBOX: HEADING_SANDBOX_POLICY,
    SLOT_APPROVAL: HEADING_APPROVAL_POLICY,
    SLOT_IDENTITY: HEADING_IDENTITY,
    SLOT_DELEGATION: HEADING_DELEGATION,
}

#: 子 Agent 的返回约定（G19）。这是**首条任务消息**的最后一段，不是环境事实，
#: 因此不占槽位、不进 system prompt —— 它描述的是"这一轮怎么交付"。
#:
#: "发送成功 ≠ 对方已处理" / "发消息不结束回合" 两条刻意保留 DSH 的原话：子 Agent 若
#: 有向父 Agent 发消息或写共享状态的通道，最常见的误判是"消息一发出就以为交付完成"，
#: 于是回合提前结束、父 Agent 什么也没拿到。
RETURN_GUIDANCE = (
    "**返回方式**：你的最终回复就是交付物 —— 写成自包含的结论（结论、依据、改动位置、"
    "未完成项），父 Agent 只能看到它，看不到你的过程记录。"
    "如果你有向父 Agent 发消息或写共享状态的通道：发送成功 ≠ 对方已处理"
    "（对方可能还没读到）；发消息也不结束你的回合 —— 只有给出最终回复才算交付。"
)


@dataclass(frozen=True)
class SandboxState:
    """这一轮子 Agent 的文件 / 执行策略（三档之一）。"""

    mode: str = SANDBOX_WORKSPACE_WRITE
    #: `workspace-write` 下真正可写的路径（绝对路径，已去重）。
    writable_paths: tuple[str, ...] = ()
    #: 实际生效的隔离档位（`app.isolation.resolve_level()`；取不到时为空串）。
    isolation_level: str = ""


@dataclass(frozen=True)
class ApprovalState:
    """这一轮子 Agent 的审批语义。`prompts_enabled=False` 包含"无人值守"这一种。"""

    prompts_enabled: bool = True
    tools_mode: str = "auto"


@dataclass(frozen=True)
class ChildIdentity:
    """子 Agent 的身份与权限范围。`remaining` 为空时按 :func:`remaining_levels` 算。"""

    run_id: str = ""
    parent_agent_id: str = ""
    current_depth: int = 0
    max_depth: int = 0
    read_only: bool = False
    remaining: int | None = None
    #: 派发方给的 3–5 词展示名（`subagent` 工具的 `description`）；空 = 未命名。
    display_name: str = ""


def remaining_levels(current_depth: int, max_depth: int) -> int:
    """还能再往下委派几层。

    与 `SubAgentRunner._resolve_tools` 的 `block_delegate` **同一口径**：`max_depth=0`
    表示禁止再委派，否则"本层之后还剩 `max_depth - current_depth` 层"。
    负数归零 —— 深度已经越界时不该出现"-1 层"这种让模型困惑的说法。
    """
    return max(0, int(max_depth) - int(current_depth))


def _isolation_level() -> str:
    """当前实际生效的隔离档位；取不到时返回空串（不猜、不影响拼装）。"""
    try:
        from app.isolation import resolve_level

        return str(resolve_level().effective.value)
    except Exception:  # noqa: BLE001 - 隔离信息是增强，读不到不该让派发失败
        return ""


def read_sandbox_state(read_only: bool) -> SandboxState:
    """从**真实工作区**读本轮沙箱状态。

    可写路径只读 `app/tools/sandbox.workspace_dir()`（agent 文件操作的唯一根），
    不自己推断：那里的切换（用户项目根 vs 沙箱副本）决定了"agent 改的是哪一份"，
    子 Agent 与父 Agent 必须看到同一个答案。
    """
    from app.tools.sandbox import workspace_dir

    if read_only:
        return SandboxState(mode=SANDBOX_READ_ONLY, isolation_level=_isolation_level())

    root = str(workspace_dir())
    return SandboxState(
        mode=SANDBOX_WORKSPACE_WRITE,
        writable_paths=(root,) if root else (),
        isolation_level=_isolation_level(),
    )


def read_approval_state(tools_mode: Any, unattended: bool) -> ApprovalState:
    """从既有的单一来源归一化审批状态。

    `tools_mode` 的归一化复用 `app.agent.guards.resolve_tools_mode`（非法值 → auto，
    yolo 才关审批）；`unattended` 是"这一轮没人能应答"——与关审批同义，但它**不是**
    yolo：文案要区分"用户主动跳过"与"根本没有应答者"。
    """
    from app.agent.guards import SESSION_MODE_YOLO, resolve_tools_mode

    mode = resolve_tools_mode(tools_mode)
    return ApprovalState(
        prompts_enabled=(mode != SESSION_MODE_YOLO) and not unattended,
        tools_mode=mode,
    )


# ── 各段渲染 ──────────────────────────────────────────────────────────────


def _render_working_directory(working_directory: str) -> str:
    path = (working_directory or "").strip()
    if not path:
        return f"{HEADING_WORKING_DIRECTORY}\n工作目录未知；调用文件工具时使用相对路径，不要猜测绝对路径。"
    return (
        f"{HEADING_WORKING_DIRECTORY}\n"
        f'你的工作目录是 "{path}"。相对路径以它为根；不要假设工作区之外的目录可写。'
    )


def _render_sandbox(sandbox: SandboxState) -> str:
    lines = [HEADING_SANDBOX_POLICY]
    if sandbox.mode == SANDBOX_READ_ONLY:
        lines.append("模式：read-only（只读）。")
        lines.append(
            "不要仅凭\"只读\"就认定自己不能开工：读文件、搜索、grep、outline 等只读工具要"
            "**正常调用**；写文件与执行命令会被**拒绝**。被拒绝时不要重试同一个调用，"
            "把限制写进结论。"
        )
    elif sandbox.mode == SANDBOX_DANGER_FULL_ACCESS:
        lines.append("模式：danger-full-access。")
        lines.append("不限制文件修改与命令执行；系统约束、参数级硬拦截与审批仍然生效。")
    else:
        lines.append("模式：workspace-write（可写）。")
        if sandbox.writable_paths:
            paths = "、".join(f'"{p}"' for p in sandbox.writable_paths)
            lines.append(f"可写路径：{paths}。只有这些路径下的修改会生效；路径外会被拒绝。")
        else:
            lines.append("可写路径：当前工作区。路径外会被拒绝。")
    if sandbox.isolation_level:
        lines.append(f"实际隔离档位：{sandbox.isolation_level}。")
    return "\n".join(lines)


def _render_approval(approval: ApprovalState) -> str:
    lines = [HEADING_APPROVAL_POLICY]
    if approval.prompts_enabled:
        lines.append(f"tools_mode={approval.tools_mode}：写文件 / 执行命令等操作需要用户确认。")
        lines.append(
            "审批是 fail-closed：等待超时、被拒绝或没有可用的应答者时，一律按失败处理，"
            "不要假定\"应该已经批准\"，也不要空等。"
        )
    elif approval.tools_mode == "yolo":
        lines.append("tools_mode=yolo：本会话不弹审批。")
        lines.append(
            "不要请求批准：需要写或执行时直接做（参数级硬拦截仍然生效）。"
            "被自动拒绝的操作不要重试。"
        )
    else:
        lines.append(f"tools_mode={approval.tools_mode}：本次派发无人值守，没有人能应答审批。")
        lines.append(
            "审批会被自动拒绝：不要请求批准；需要写或执行时直接做，被自动拒绝的操作不要重试。"
        )
    return "\n".join(lines)


def _render_identity(identity: ChildIdentity) -> str:
    who = []
    if identity.run_id:
        who.append(f"run={identity.run_id}")
    if identity.parent_agent_id:
        who.append(f"父会话={identity.parent_agent_id}")
    if identity.display_name:
        who.append(f"任务名={identity.display_name}")
    suffix = f"（{'，'.join(who)}）" if who else ""
    lines = [
        HEADING_IDENTITY,
        f"你是父 Agent 派发的子 Agent{suffix}。",
        "你的权限范围（工具面、只读 / 可写、审批模式）在启动时就固定了，会话内**无法**扩大。",
        "被拒绝的操作不要重试 —— 把结论做完整，把限制说明白，让派发你的 Agent 处理。",
    ]
    return "\n".join(lines)


def _render_delegation(identity: ChildIdentity) -> str:
    remaining = (
        identity.remaining
        if identity.remaining is not None
        else remaining_levels(identity.current_depth, identity.max_depth)
    )
    depth = f"当前深度 {identity.current_depth}/{identity.max_depth}，剩余 {remaining} 层可继续委派。"
    lines = [HEADING_DELEGATION, depth]
    if remaining <= 0:
        lines.append(
            "不要调用 subagent 工具：直接自己完成；确实需要拆分时，先把可独立执行的部分做完。"
        )
    else:
        lines.append("需要时可把独立的子任务再派发；否则直接完成。")
    return "\n".join(lines)


def build_child_context(
    *,
    working_directory: str,
    sandbox: SandboxState,
    approval: ApprovalState,
    identity: ChildIdentity,
) -> str:
    """按 :data:`SLOT_ORDER` 拼出环境段（含外层标记）。

    拼装顺序**只由 `SLOT_ORDER` 决定**（不按 kwargs 顺序），因此新增段位只需追加序号，
    不会因为调用方调整参数顺序而改变模型读到的结构。
    """
    renderers = {
        SLOT_WORKING_DIRECTORY: lambda: _render_working_directory(working_directory),
        SLOT_SANDBOX: lambda: _render_sandbox(sandbox),
        SLOT_APPROVAL: lambda: _render_approval(approval),
        SLOT_IDENTITY: lambda: _render_identity(identity),
        SLOT_DELEGATION: lambda: _render_delegation(identity),
    }
    sections = [renderers[key]() for _number, key in sorted(SLOT_ORDER, key=lambda item: item[0])]
    body = "\n\n".join(sections)
    return f"<{CONTEXT_TAG}>\n{body}\n</{CONTEXT_TAG}>"


__all__ = [
    "CONTEXT_TAG",
    "HEADING_APPROVAL_POLICY",
    "HEADING_DELEGATION",
    "HEADING_IDENTITY",
    "HEADING_SANDBOX_POLICY",
    "HEADING_WORKING_DIRECTORY",
    "RETURN_GUIDANCE",
    "SANDBOX_DANGER_FULL_ACCESS",
    "SANDBOX_READ_ONLY",
    "SANDBOX_WORKSPACE_WRITE",
    "SLOT_HEADINGS",
    "SLOT_ORDER",
    "ApprovalState",
    "ChildIdentity",
    "SandboxState",
    "build_child_context",
    "read_approval_state",
    "read_sandbox_state",
    "remaining_levels",
]
