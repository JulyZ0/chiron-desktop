"""把「当前会话标识」暴露给 provider 层（contextvar）。

**为什么需要它**：OpenCode（Zen / Go）要求请求携带 `x-opencode-session`，否则返回

    400 MissingSessionID: Request is missing x-opencode-session and cannot be routed
    efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it

这个头只影响对方的路由与 prompt 缓存（不影响可用性，但缺失就直接 400），
所以用**稳定的会话 ID** 最合适：同一对话的请求走同一路由，prompt 缓存命中更好。

用 contextvar 而不是改调用链签名：provider 是长生命周期对象、每次请求复用，
而会话 ID 是**每次请求**的属性 —— 与 app/tools/context.py 的 set_tool_context 同一模式。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

#: 当前请求所属的会话标识（空串 = 未知；provider 层会用 trace / 进程常量兜底，
#: 见 :func:`opencode_session_id` —— 宁可多一个稳定值，也不要让 OpenCode 400）。
_llm_session_id: ContextVar[str] = ContextVar("chiron_llm_session_id", default="")

#: 进程级兜底（进程启动时生成一次）：既没有会话也没有 trace 时用它。
#: 它不提供"同对话同路由"的亲和性，但能保证**请求可被路由** —— 而空值会直接 400。
_PROCESS_SESSION = f"chiron-{uuid.uuid4().hex[:12]}"


def set_llm_session_id(value: str) -> Token[str]:
    """设置当前请求会话 ID，并返回供 finally 精确复位的 token。

    AgentRuntime 用 task.session_id（缺省 task.id）；quick/auto 路由用会话 ID
    （缺省 task.id）。调用方若持有 token，必须在请求结束时 reset，避免复用长生命周期
    执行上下文时把上一请求的会话标识带给下一请求。
    """
    return _llm_session_id.set(str(value or ""))


def reset_llm_session_id(token: Token[str]) -> None:
    """恢复 set_llm_session_id 之前的 ContextVar 值。"""
    _llm_session_id.reset(token)


def get_llm_session_id() -> str:
    return _llm_session_id.get() or ""


def opencode_session_id() -> str:
    """OpenCode 请求头 ``x-opencode-session`` 要用的**永不为空**的稳定标识。

    优先级：显式设置的会话 ID → 请求 trace id → 进程级常量。

    **为什么要有兜底**：这个头一旦缺失，OpenCode 直接 400（`MissingSessionID`），
    而"设置会话 ID"依赖调用链上的入口自觉 —— 历史上 `desktop_infer` / `openai_compat`
    这类新入口就漏了，导致同一个 provider 在主干能用、在这些入口必然失败。把兜底放在
    provider 层之后，"漏设"的最坏结果退化为"路由亲和性差一点"，不再是 400。

    兜底值同样满足 OpenCode 的要求：非空、稳定、无空格/PII。
    """
    explicit = get_llm_session_id().strip()
    if explicit:
        return explicit
    try:
        from app.observability.trace_context import current_trace_id

        trace = current_trace_id().strip()
        if trace:
            return trace
    except Exception:  # noqa: BLE001 - trace 不可用不该影响请求
        pass
    return _PROCESS_SESSION


#: 当前请求的思考档位（**归一化前的原始值**）。
#: 归一化与"到底发不发"由 app/providers/effort.py 决定 —— 这里只负责把**请求级**的值
#: 送到 provider 层，避免为它改 chat_stream 的三层签名。
_llm_effort: ContextVar[str] = ContextVar("chiron_llm_effort", default="")


def set_llm_effort(value: str) -> None:
    """由 AgentRuntime 在开始一次运行前设置（取自 `llm_config.effort`）。"""
    _llm_effort.set(str(value or ""))


def get_llm_effort() -> str:
    return _llm_effort.get() or ""
