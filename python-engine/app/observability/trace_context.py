"""请求级 trace id（R9 贯通）。

Go 网关对每个请求生成 trace_id（内部 Span），并通过 `X-Chiron-Trace-Id` 传给引擎；
引擎此前在 `submit_task` 里各生成一个 12 位随机串，两边日志无法对应。这里把 header
放进 contextvar，调用方缺省取它。

不用 ContextVar 存 OTel span：OTel 自己已有 traceparent 传播；这个头只是"网关没有
配置 OTLP 时也能对齐日志/事件"的兜底，两者可以并存。
"""

from __future__ import annotations

import contextvars

_MAX_TRACE_LEN = 64

_request_trace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "chiron_request_trace_id", default=""
)


def current_trace_id() -> str:
    return _request_trace.get()


def set_request_trace_id(value: str) -> contextvars.Token:
    text = str(value or "").strip()[:_MAX_TRACE_LEN]
    return _request_trace.set(text)


def reset_request_trace_id(token: contextvars.Token) -> None:
    try:
        _request_trace.reset(token)
    except ValueError:
        # 跨 task 传递时 token 可能不属于当前上下文；忽略即可。
        pass


class TraceHeaderMiddleware:
    """纯 ASGI 中间件：读取 `X-Chiron-Trace-Id` 并设置 contextvar。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        trace_id = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"x-chiron-trace-id":
                trace_id = value.decode("latin-1", "ignore")
                break
        token = set_request_trace_id(trace_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_trace_id(token)
