"""工具执行 span（R9）。

只记**元数据**：工具名、来源、租户/用户、耗时、成功/错误类型。参数与返回内容不写 ——
工具参数里经常有路径、代码片段甚至密钥，span 不是日志。

无 OTLP 时 OpenTelemetry 返回 no-op tracer，这里零开销；所有属性写入都吞异常，
观测失败不影响工具本身。
"""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import trace

_TRACER_NAME = "chiron.tools"


def start_tool_span(
    *, name: str, source: str = "", tenant_id: int = 0, user_id: str = ""
) -> Any:
    span = trace.get_tracer(_TRACER_NAME).start_span(f"execute_tool {name}")
    try:
        span.set_attribute("tool.name", name)
        if source:
            span.set_attribute("tool.source", source)
        if tenant_id:
            span.set_attribute("chiron.tenant_id", int(tenant_id))
        if user_id:
            span.set_attribute("chiron.user_id", user_id)
    except Exception:  # noqa: BLE001
        pass
    return span


def finish_tool_span(
    span: Any, *, started_at: float, error: str = "", error_type: str = ""
) -> None:
    try:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        span.set_attribute("tool.duration_ms", round(elapsed_ms, 2))
        if error:
            span.set_attribute("error.type", error_type or "ToolError")
            span.set_attribute("error.message", error[:500])
        else:
            span.set_attribute("tool.success", True)
    except Exception:  # noqa: BLE001
        pass
    try:
        span.end()
    except Exception:  # noqa: BLE001
        pass
