# GenAI 语义的模型调用 span（R9）。
#
# 只记录**元数据**：provider / 模型 / token / finish_reason / 租户。提示词与模型输出
# 一律不写入 span —— 队友接 Langfuse 时默认不该看到用户对话内容（需要时由采样/脱敏策略
# 单独决定）。没有配置 OTLP 时 OpenTelemetry 返回 no-op tracer，这里零开销。
from __future__ import annotations

from typing import Any

from opentelemetry import trace

_TRACER_NAME = "chiron.gateway"


def _tracer() -> Any:
    return trace.get_tracer(_TRACER_NAME)


def start_model_span(
    *, model: str, provider: str = "", tenant_id: int = 0, stream: bool = False
) -> Any:
    """开始一个 `chat` span。属性名遵循 OTel GenAI 语义约定（gen_ai.*）。"""
    span = _tracer().start_span(f"chat {model or 'unknown'}")
    try:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model or "")
        if provider:
            span.set_attribute("gen_ai.system", provider)
        if tenant_id:
            span.set_attribute("chiron.tenant_id", int(tenant_id))
        span.set_attribute("chiron.stream", bool(stream))
    except Exception:  # noqa: BLE001 - 观测失败不影响推理
        pass
    return span


def record_model_usage(
    span: Any,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    finish_reason: str = "",
    response_model: str = "",
) -> None:
    try:
        if input_tokens:
            span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
        if output_tokens:
            span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
        if cached_tokens:
            span.set_attribute("chiron.usage.cached_tokens", int(cached_tokens))
        if finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [finish_reason])
        if response_model:
            span.set_attribute("gen_ai.response.model", response_model)
    except Exception:  # noqa: BLE001
        pass


def record_model_error(span: Any, exc: BaseException) -> None:
    try:
        span.record_exception(exc)
        span.set_attribute("error.type", type(exc).__name__)
    except Exception:  # noqa: BLE001
        pass


def end_model_span(span: Any) -> None:
    try:
        span.end()
    except Exception:  # noqa: BLE001
        pass
