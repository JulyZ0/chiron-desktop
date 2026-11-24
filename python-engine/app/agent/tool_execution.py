"""工具执行编排（从 `runtime.py` 的 `_guarded_execute_tool` 一族整块搬出，P2-11 拆分）。

## 这里装的是"一次工具调用从裁决到执行完"的全过程

顺序是刻意的，且**不可重排**：

1. **参数解析** —— 半截 JSON（被 max_tokens 截断）直接拒绝并回灌可行动的出路，
   绝不用空参数蒙混过关（空参数还会绕过按参数匹配的栅栏规则）。
2. **栅栏裁决**（`ToolGuard`）—— block / confirm / allow。
3. **服务端授权**（Tool Broker）—— 危险工具的判定权威在服务端，**fail-closed**：
   拿不到判定就拒绝执行，而不是按引擎自己的判定放行。
4. **LLM 安全评审**（guardian）—— 只给 confirm 用，目的在于"省掉每一次弹窗"，
   不是放宽：评审 allow 但级别不许免人工 ⇒ **仍走人工审批**，只有明确 deny 才拒。
5. **人工审批**（`runtime._begin_approval`）—— 返回审批事件而**不在此等待**：
   调用方必须先 yield 给前端，否则前端收不到确认卡片、任务永久挂起。
6. **执行**（`execute_tool`：PreToolUse hook → 分发 → PostToolUse hook）——
   hook 只能**进一步收紧**，没有放行语义，因此不会成为绕过分级的新通道。
7. **分发**（`dispatch_tool` / `dispatch_tool_inner`）—— 压入调用身份上下文（子进程
   嵌套调用靠它认父）、并行写占用裁决、本地注册表 / 外部执行器。

## 为什么搬到模块里

`runtime.py` 已经有 6000 行，而这一段是**唯一**一处"工具调用与其裁决"的收口 ——
它只依赖 runtime 的少数成员（`_tool_guard` / `_current_mode` / `_guardian_review` /
`_begin_approval` / `_execute_tool` / `_recovery` / `_tool_executor`），搬出来之后
`runtime` 以**同名转发**保持调用点不变（含测试对 `_execute_tool` / `_dispatch_tool`
的 monkeypatch —— 转发里一律经 `runtime.<name>(…)` 取属性，补丁照旧生效）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from app.agent.runtime_types import AgentEvent, AgentTask
from app.tools.context import to_tenant_int
from app.tools.registry import registry as local_tool_registry

logger = logging.getLogger(__name__)

#: 子 Agent 派发工具名：它的生命周期 hook 与普通工具不同（Start/Stop 各一次）。
SUBAGENT_TOOL = "subagent"


def _truncation_recovery_hint(tool_name: str) -> str:
    """截断之后给模型的**可执行**出路。

    原来的文案是 "re-issue the tool call" —— 对截断**根本无效**：截断意味着内容超出了
    输出预算，原样重发必然再截断。模型于是照做、再截断、再照做，直到耗尽轮数，
    外表现就是"工具失败后不会自动修复、跑不下去"。提示必须指向**能真正收敛**的动作。
    """
    if tool_name == "write_file":
        return (
            "Do NOT resend the same content — it exceeds the output budget and will be "
            "truncated again. Split the file: send the first chunk with "
            "`write_file(path, content)` and each remaining chunk with "
            "`write_file(path, content, append=true)`."
        )
    if tool_name == "edit_file":
        return (
            "Do NOT resend the same edit — it exceeds the output budget. Split it into "
            "several smaller `edit_file` calls, each replacing a smaller unique snippet, "
            "or write the file in chunks via `write_file(..., append=true)`."
        )
    if tool_name == "todo_write":
        return (
            "Do NOT resend the same list. Keep the checklist short (one line per item) and send fewer items per call."
        )
    return (
        "Do NOT resend the same call — it exceeds the output budget and will be truncated "
        "again. Reduce the payload (smaller chunk, narrower scope), or split the work into "
        "several smaller calls."
    )


def _record_recovery(state: Any, tool_name: Any, verdict: Any, targs: dict[str, Any], result: Any) -> None:
    """P5：把一次工具执行的结果并入恢复状态。

    失败 ⇒ 记下**这次操作的指纹**；成功 ⇒ 清除 active failure（失败已被克服）。
    指纹只看"工具 + 等价参数"，因此换个参数再试会被判为"不同操作"而放行。
    """
    from app.agent import recovery as _recovery_mod

    if isinstance(result, dict) and result.get("error"):
        state.record_failure(
            _recovery_mod.Failure(
                fingerprint=_recovery_mod.fingerprint(str(tool_name), targs),
                tool_name=str(tool_name),
                level=str(getattr(verdict, "level", "") or ""),
                reason=str(result.get("error"))[:200],
            )
        )
    else:
        state.record_success()


async def guarded_execute_tool(
    runtime: Any, tool_call: dict[Any, Any], task: AgentTask, *, allow_approval: bool = True
) -> tuple[dict[Any, Any] | None, AgentEvent | None]:
    """工具栅栏三态裁决：block（拒绝）/ confirm（需要用户确认）/ allow（直接执行）。

    返回 ``(tool_result, approval_event_or_None)``：
    - block/allow：``(tool_result, None)``
    - confirm：``(None, approval_event)`` —— **不在此等待**，调用方必须先 yield
      approval 事件给前端（否则前端收不到确认卡片、任务永久挂起），
      再调用 ``_await_approval`` 等待用户批准/拒绝/超时。

    ``allow`` 分支里还有**第三处**产生审批的来源：`PreToolUse` hook 返回 `ask`
    （2026-10-09）—— 它复用**同一套**手续（`_begin_approval`），因此调用方的处理
    逻辑不必分叉。

    ``allow_approval=False``：调用方**无法交互**（并发批 `_run_parallel_batch`）——
    此时"需要确认"降级为**显式错误**，而不是把审批事件丢掉（那会静默变成
    "什么都没发生"，而并发批的 gather 前提是"组内不产生审批"）。
    """
    tool_name = tool_call["name"]
    try:
        targs = (
            json.loads(tool_call["arguments"]) if isinstance(tool_call["arguments"], str) else tool_call["arguments"]
        )
    except (json.JSONDecodeError, TypeError):
        # 参数不是合法 JSON：可能是被 max_tokens 截断的半截 arguments。
        # 不用空参数蒙混过关（空参数还会绕过按参数匹配的栅栏规则），直接拒绝，
        # 把错误回灌给模型让它重发完整调用。
        logger.warning(
            "Tool %s has invalid JSON arguments (possibly truncated); refusing to execute",
            tool_name,
        )
        return {
            "error": (
                f"Tool '{tool_name}' arguments are not valid JSON (possibly "
                f"truncated by max_tokens); not executed. "
                f"{_truncation_recovery_hint(tool_name)}"
            )
        }, None
    # `ToolVerdict` 必须**无条件**导入：下面两条分支（服务端授权收紧、guardian 放行）
    # 都要用它重建裁决。写进 `if verdict.second_check:` 里会让 Python 在编译期就把
    # 它判定为整个函数的局部变量（import 也是赋值），于是 second_check 为假、
    # 但又走到 confirm + guardian 放行时，抛
    # `cannot access local variable 'ToolVerdict' where it is not associated with a value`。
    # `SESSION_MODE_ASK` 要在**函数内**导入：它在 `__init__` 里是局部导入，
    # 模块级并不存在，直接引用会 NameError。与 `ToolVerdict` 同理，
    # 必须无条件执行（不能藏在下面某个分支里）。
    from app.agent.guards import SESSION_MODE_ASK, ToolVerdict

    verdict = runtime._tool_guard.evaluate(tool_name, targs or {}, runtime._current_mode)

    # ── 服务端授权（Tool Broker）：危险工具的判定权威在服务端 ──
    #
    # 引擎侧的 ToolGuard 与执行在**同一进程**（而该进程正在处理不可信内容）——判定与执行
    # 同处一个信任域。所以 delete / external 在执行前必须向 Broker 要一次授权，并按
    # **服务端**的判定执行：服务端可以收紧引擎/用户的模式选择（典型：yolo 下仍要求
    # 不可逆操作必须确认）。
    # **fail-closed**：拿不到服务端判定就拒绝执行这类工具 —— 按引擎自己的判定放行，
    # 等于把这一层抹掉。
    #
    # `broker_requires_approval`：服务端明确要求"这一步要人"。guardian 随后**不得**
    # 把它放行 —— 服务端是判定的权威，引擎侧的旁路评审不能覆盖它（否则 delete 级
    # 的"免人工"就变成绕过服务端策略的另一条路）。
    broker_requires_approval = False
    if verdict.second_check:
        from app.tools import broker

        try:
            decision = await broker.authorize(
                tool_name,
                targs or {},
                tenant_id=to_tenant_int(getattr(task, "tenant_id", 0)),
                user_id=getattr(task, "user_id", ""),
                session_id=getattr(task, "session_id", ""),
                tool_call_id=tool_call.get("id") or tool_name,
                tools_mode=runtime._current_mode,
            )
        except Exception as exc:  # noqa: BLE001 - 拿不到授权就不执行
            logger.error("tool broker unavailable, refusing %s: %s", tool_name, exc)
            return {"error": (f"Tool '{tool_name}' was NOT executed: authorization service unavailable ({exc})")}, None

        if not decision.get("allowed", False):
            reason = decision.get("reason") or "not allowed by server policy"
            logger.warning("tool broker denied %s: %s", tool_name, reason)
            return {"error": f"Tool '{tool_name}' denied by server policy: {reason}"}, None

        if decision.get("requires_user_approval"):
            broker_requires_approval = True
        if decision.get("requires_user_approval") and verdict.action == "allow":
            # 服务端把"放行"收紧成"要确认"（典型：yolo 下的不可逆操作）
            logger.info(
                "tool broker tightened %s to confirm (mode=%s enforced=%s)",
                tool_name,
                runtime._current_mode,
                decision.get("enforced"),
            )
            verdict = ToolVerdict(
                "confirm",
                reason=f"server requires approval (level={decision.get('level')})",
                risk_level="high",
                level=verdict.level,
                second_check=True,
            )

    if verdict.action == "block":
        logger.warning("Tool guard blocked %s reason=%s", tool_name, verdict.reason)
        return {"error": f"Tool '{tool_name}' blocked by guard: {verdict.reason}"}, None
    # ── P2：confirm 之前先过 **LLM 安全评审**（guardian）──
    #
    # 它替代"每次都弹窗"：写类可被放行（免人工）；名字**声明**为 delete / external 的
    # 仍必须人（见 app/agent/guardian.py 的 may_release）。
    #
    # 三条必须守住的语义（2026-11 修正）：
    # 1. **只按本次调用的参数/代码判定**。工具名与动作级别是启发式标签（`execute_python`
    #    无法归类时保守停在 delete），不能成为 deny 的依据 —— 否则"只打印问候语"
    #    也会因为标签被拒，而理由里还写着"未发现风险"，自相矛盾。
    # 2. **评审 allow 但级别不许免人工 ⇒ 走人工审批，不是 deny**。以前这里落到
    #    `elif not failed` 的 deny 分支，于是"评审说没风险"和"被拒绝"同时发生。
    # 3. 评审没做成 / 解析失败 / 服务端要求人工 ⇒ fail-open 回退人工审批，绝不 deny。
    if verdict.action == "confirm":
        from app.agent.guardian import may_release as guardian_may_release
        from app.agent.guardian import summarize_reason

        # **`ask` 模式必须跳过 guardian**。
        #
        # guardian 存在的意义是"auto 模式下省掉每一次弹窗"：write 级先让一个独立
        # 评审判一次，通过了就免人工。但在 `ask` 模式下，用户选择的恰恰是"每一次
        # 写/删/外部操作都由我拍板" —— 此时放行 guardian 等于把用户显式选的 `ask`
        # 悄悄降级成 `auto`，界面从此一次确认都不弹（用户报的正是这个：设成"询问"
        # 却从未发生询问，日志里连一条 approval 都没有）。
        #
        # 同理也没必要为它花一次评审的 token：结论注定是"保持 confirm"。
        assessment = (
            None
            if runtime._current_mode == SESSION_MODE_ASK
            else await runtime._guardian_review(tool_call, task, verdict, targs)
        )
        # 命令类工具由 `tool_policy` 静态解析分级：只有单条安全命令能降为 write，
        # 且 Guardian 还要求 allow + low；复合命令、重定向与解释器源码保持 delete，
        # 必须人工审批。普通 edit/write 的名字声明级别仍走原有 write 放行路径。
        from app.agent.tool_policy import COMMAND_TOOLS

        level_is_declared = tool_name not in COMMAND_TOOLS
        if (
            assessment is not None
            and not broker_requires_approval
            and guardian_may_release(
                verdict.level,
                assessment,
                targs,
                level_is_declared=level_is_declared,
            )
        ):
            logger.info(
                "guardian released %s (level=%s declared=%s risk=%s): %s",
                tool_name,
                verdict.level,
                level_is_declared,
                assessment.risk_level,
                summarize_reason(assessment.rationale),
            )
            verdict = ToolVerdict(
                "allow",
                reason=f"released by security review (risk={assessment.risk_level})",
                risk_level=assessment.risk_level,
                level=verdict.level,
                second_check=verdict.second_check,
            )
        elif assessment is not None and assessment.outcome == "deny":
            # 真正 deny 的唯一条件：评审**明确**给出 deny（且不是"自述无风险"的
            # 矛盾判词 —— 那种已在 parse_assessment 里归一为 allow）。
            logger.warning(
                "guardian denied %s (level=%s risk=%s): %s",
                tool_name,
                verdict.level,
                assessment.risk_level,
                summarize_reason(assessment.rationale),
            )
            return {"error": (f"Tool '{tool_name}' denied by security review: {assessment.rationale}")}, None
        else:
            # 其余情况一律**保持人工审批**（fail-open，不猜测）：
            # - assessment is None：未启用 / 无 gateway / `ask` 模式；
            # - assessment.failed：评审没做成或输出不可解析；
            # - 评审 allow 但级别/服务端要求人工（典型：标签把无害代码判成 delete）。
            logger.info(
                "security review kept %s for human approval "
                "(level=%s declared=%s broker_requires_approval=%s "
                "assessment=%s failed=%s reason=%s)",
                tool_name,
                verdict.level,
                level_is_declared,
                broker_requires_approval,
                "none" if assessment is None else assessment.outcome,
                bool(assessment is not None and assessment.failed),
                "n/a" if assessment is None else summarize_reason(assessment.rationale, 120),
            )
    if verdict.action == "confirm":
        if not allow_approval:
            # 不可交互的调用方（并发批）：显式失败，而不是交出无人认领的审批事件。
            # 读级工具本不该走到这里（`requires_confirmation` 只对 write/delete/external
            # 为真）—— 所以这条是**防御性**的：一旦将来有读级工具被判 confirm，
            # 也只是报错，而不是静默丢掉一次确认。
            return {
                "error": (
                    f"Tool '{tool_name}' requires user confirmation, but it ran inside a "
                    "concurrent batch where approval is impossible — re-issue it as a "
                    "standalone call"
                )
            }, None
        return None, await runtime._begin_approval(tool_call, task, level=str(verdict.level or ""), targs=targs)
    # ── P5：恢复路由 —— 同一个失败操作不要反复重试 ──
    #
    # 判据是"这次调用是不是**同一个失败操作**"（指纹），不是"试了几次"（对齐 Reasonix
    # `recovery.Decide`）。命中 REVIEW 时把失败事实与"已被拒"一起回灌给模型，
    # 逼它换做法 —— 而不是继续烧预算重试同一个必然失败的操作。
    recovery_state = getattr(runtime, "_recovery", None)
    if recovery_state is not None:
        from app.agent import recovery as _recovery_mod

        rdec = _recovery_mod.decide(
            tool_name=str(tool_name),
            level=str(verdict.level or ""),
            args=targs,
            state=recovery_state,
        )
        if rdec.action == _recovery_mod.Action.REVIEW:
            logger.warning("recovery gate (REVIEW): %s", rdec.reason)
            return {"error": f"Recovery required: {rdec.reason}"}, None

    # allow：正常执行。执行里还可能被 `PreToolUse` hook 的 `ask` 拦成"需要确认" ——
    # 那就走与 confirm **完全同一套**手续（`_begin_approval`），调用方不必分叉。
    logger.info("Executing tool %s (id=%s)", tool_name, tool_call.get("id"))
    result, ask_reason = await runtime._execute_tool(tool_call, task)
    if recovery_state is not None:
        _record_recovery(recovery_state, tool_name, verdict, targs, result)
    if ask_reason is None:
        return result, None

    # `PreToolUse` hook 要求人工确认（`ask` 档，2026-10-09）：**工具尚未执行**。
    logger.info("PreToolUse hook asks for confirmation on %s — routing to approval", tool_name)
    if not allow_approval:
        # 并发批不能交互：给**显式**失败，而不是把审批事件丢掉
        # （丢掉 = 静默变成"什么都没发生"，而并发批的 gather 前提是组内不产生审批）
        return {
            "error": (
                f"PreToolUse hook requires confirmation for '{tool_name}' ({ask_reason}), "
                "but it ran inside a concurrent batch where approval is impossible — "
                "re-issue it as a standalone call"
            )
        }, None
    return None, await runtime._begin_approval(
        tool_call, task, level=str(verdict.level or ""), targs=targs, reason=ask_reason
    )


async def execute_tool(
    runtime: Any, tool_call: dict[Any, Any], task: AgentTask, *, approved: bool = False
) -> tuple[dict[Any, Any], str | None]:
    """执行工具（含批 G 生命周期 hook）。

    分层顺序是刻意的：本方法在 `_guarded_execute_tool`（工具策略 / 服务端授权 /
    审批）**之后**才被调用，因此 `PreToolUse` hook 只能进一步**收紧** —— 它没有
    "放行"语义，也就不会成为绕过 `tool_policy` 分级的新通道（方案 03 §3.2）。

    - `PreToolUse`：**可阻断**（`deny` = 拒绝；`ask` = 要求用户确认，2026-10-09）；
    - `PostToolUse` / `PostToolUseFailure` / `SubagentStart` / `SubagentStop`：
      fire-and-forget，失败、超时、崩溃都不改变这里的返回值。
    默认 `hooks_enabled=False` 时以上调用全部是空操作（零行为变化）。

    Returns:
        `(结果, 待确认原因)`。**第二项非空表示"工具尚未执行、hook 要求用户确认"**
        （`ask` 档）—— 调用方（`_guarded_execute_tool`）据此走既有的审批通道。
        `approved=True`（审批通过后由 `_await_approval` 再次调用）时 `ask` **不再生效**
        —— 否则用户批一次、hook 问一次，会无限循环；而 `deny` 依然生效
        （hook 只能收紧，不会被"批准过"抵消）。
    """
    # 同样延迟导入（见 `run()` 中的说明）。
    from app.hooks import DECISION_ASK, hooks

    tool_name = str(tool_call["name"])

    decision = await hooks.before_tool_use(task=task, tool_call=tool_call)
    if decision is not None:
        if decision.kind == DECISION_ASK:
            if not approved:
                return {}, str(decision)
            logger.info("PreToolUse hook asked again after approval for %s — proceeding", tool_name)
        else:
            return {"error": decision}, None

    is_subagent = tool_name == SUBAGENT_TOOL
    if is_subagent:
        await hooks.subagent_start(task=task, tool_call=tool_call)

    result = await runtime._dispatch_tool(tool_call, task)

    if is_subagent:
        await hooks.subagent_stop(task=task, tool_call=tool_call, result=result)
    await hooks.after_tool_use(task=task, tool_call=tool_call, result=result)
    return result, None


async def dispatch_tool(runtime: Any, tool_call: dict[Any, Any], task: AgentTask) -> dict[Any, Any]:
    """把"这次是哪个工具调用"压进上下文，再交给实际分发。

    G-T4：`run_code` 在子进程里发起的嵌套调用**不经过这里**，于是主循环既不会
    为它记 span，`loop_guard` 也看不见它 —— 执行图上那一整段是空白。嵌套调用
    要知道"我是谁发起的"，只能靠上下文（`registry.execute` 的签名
    `(name, params)` 没有位置传）。这里的 `span_id` 与下面主循环 `record_span`
    用的那个**逐字相同**，所以子节点挂上去之后父子可以互相定位。
    """
    from app.tools.context import get_tool_context, tool_call_scope

    call_id = str(tool_call.get("id") or tool_call.get("name") or "")
    # 本轮节点 id（`run()` 每轮 `set_tool_context(span_id=turn_span_id)` 写下的）。
    turn_span = str(get_tool_context("span_id", "") or "")
    tool_span = f"{turn_span}:tool:{call_id}" if turn_span else ""
    with tool_call_scope(call_id, span_id=tool_span):
        return cast(dict[Any, Any], await runtime._dispatch_tool_inner(tool_call, task))


async def dispatch_tool_inner(runtime: Any, tool_call: dict[Any, Any], task: AgentTask) -> dict[Any, Any]:
    """工具分发的实际实现（无 hook 包裹）—— 由 `_dispatch_tool` 调用。"""
    tool_name = tool_call["name"]
    tool_arguments = tool_call["arguments"]

    # 解析参数
    try:
        params = json.loads(tool_arguments) if isinstance(tool_arguments, str) else tool_arguments
    except json.JSONDecodeError:
        params = {"raw": tool_arguments}

    # ── P4：并行写占用裁决 —— 同一文件同一时刻只能有一个写者 ──
    #
    # 只在**跨 owner**（不同 agent / 子任务）时拦截；同一 owner 反复写自己正在迭代的
    # 文件是正常的。没有它时，两个并行子 agent 写同一文件会**静默互相覆盖**且都报成功，
    # 最终内容取决于完成顺序（竞态），而没有任何一处报错。
    from app.agent.write_claim import FILE_WRITE_TOOLS, claim, path_of, release
    from app.tools.context import get_session_id

    claimed_path = ""
    claim_owner = ""
    if tool_name in FILE_WRITE_TOOLS:
        claimed_path = path_of(tool_name, params if isinstance(params, dict) else None)
        claim_owner = get_session_id() or ""
        if claimed_path and claim_owner:
            ok, holder = claim(claimed_path, claim_owner)
            if not ok:
                logger.warning("write claim denied: %s is being written by %s", claimed_path, holder)
                return {
                    "error": (
                        f"file '{claimed_path}' is currently being written by another "
                        f"agent ({holder}); wait for it to finish or edit a different file"
                    )
                }

    # 优先走本地 Python 工具注册表
    if local_tool_registry.get(tool_name) is not None:
        try:
            return await local_tool_registry.execute(tool_name, params or {})
        except Exception as e:
            logger.error("Local tool execution failed (%s): %s", tool_name, e)
            return {"error": str(e)}
        finally:
            # 写操作是短暂、逐次的 —— 占用只为阻止**同时**的另一个写者，执行完即释放。
            if claimed_path and claim_owner:
                release(claimed_path, claim_owner)

    # 如果有外部工具执行器，使用它（兼容旧 Go 调用链）
    if runtime._tool_executor:
        try:
            result: dict[Any, Any] = await runtime._tool_executor.execute(
                tool_name=tool_name,
                params=params,
                tenant_id=task.tenant_id,
                user_id=task.user_id,
            )
            return result
        except Exception as e:
            logger.error("Tool execution failed (%s): %s", tool_name, e)
            return {"error": str(e)}

    # 默认返回未实现
    return {"error": f"Tool '{tool_name}' not implemented"}
