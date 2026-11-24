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

from contextvars import ContextVar, Token

#: 当前请求所属的会话标识（空串 = 未知，此时不注入该头，行为与不带头的旧版本一致）
_llm_session_id: ContextVar[str] = ContextVar("chiron_llm_session_id", default="")


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


#: 当前请求的思考档位（**归一化前的原始值**）。
#: 归一化与"到底发不发"由 app/providers/effort.py 决定 —— 这里只负责把**请求级**的值
#: 送到 provider 层，避免为它改 chat_stream 的三层签名。
_llm_effort: ContextVar[str] = ContextVar("chiron_llm_effort", default="")


def set_llm_effort(value: str) -> None:
    """由 AgentRuntime 在开始一次运行前设置（取自 `llm_config.effort`）。"""
    _llm_effort.set(str(value or ""))


def get_llm_effort() -> str:
    return _llm_effort.get() or ""
