# 隐私模式中间件 — 企业合规：X-Privacy-Mode 请求头透传
#
# Go 网关透传 X-Privacy-Mode 头；no_retention 模式下 Python 引擎跳过数据留存
# （session 落 Redis / trace 写 Redis Stream），内存态会话仍正常工作。
#
# 实现为纯 ASGI 中间件（而非 BaseHTTPMiddleware）：session/trace 写入发生在
# SSE 流式响应体内，纯 ASGI 形态保证 contextvar 在流式迭代期间始终可见。
from __future__ import annotations

import contextvars

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# 隐私模式常量
NO_RETENTION = "no_retention"

# 请求作用域隐私模式（middleware 层注入，session_store / trace 读取）
privacy_mode_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "privacy_mode", default=""
)


def is_no_retention() -> bool:
    """当前请求是否处于隐私模式（no_retention）。

    缺省头或未知值均视为正常模式（fail-open 到正常留存）。
    供 session_store / trace 在持久化写入点调用。
    """
    return privacy_mode_var.get() == NO_RETENTION


class PrivacyModeMiddleware:
    """读取 X-Privacy-Mode 请求头并注入 contextvar（请求作用域）。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        mode = Headers(scope=scope).get("x-privacy-mode", "")
        token = privacy_mode_var.set(mode)
        try:
            await self.app(scope, receive, send)
        finally:
            privacy_mode_var.reset(token)
