"""R9：模型调用 span 的属性口径（只记元数据，不写提示词/回答）。"""
from __future__ import annotations

from app.observability import genai


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.exceptions: list[BaseException] = []
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    def start_span(self, name: str) -> _FakeSpan:
        span = _FakeSpan(name)
        self.spans.append(span)
        return span


def test_model_span_attributes_and_usage(monkeypatch) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr(genai, "_tracer", lambda: tracer)

    span = genai.start_model_span(model="gpt-x", provider="openai", tenant_id=7, stream=True)
    genai.record_model_usage(
        span,
        input_tokens=120,
        output_tokens=30,
        cached_tokens=64,
        finish_reason="stop",
        response_model="gpt-x",
    )
    genai.end_model_span(span)

    assert tracer.spans == [span]
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "gpt-x"
    assert span.attributes["gen_ai.system"] == "openai"
    assert span.attributes["chiron.tenant_id"] == 7
    assert span.attributes["chiron.stream"] is True
    assert span.attributes["gen_ai.usage.input_tokens"] == 120
    assert span.attributes["gen_ai.usage.output_tokens"] == 30
    assert span.attributes["chiron.usage.cached_tokens"] == 64
    assert span.attributes["gen_ai.response.finish_reasons"] == ["stop"]
    assert span.ended is True


def test_model_span_records_error(monkeypatch) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr(genai, "_tracer", lambda: tracer)
    span = genai.start_model_span(model="m", provider="p")

    err = TimeoutError("provider timeout")
    genai.record_model_error(span, err)
    genai.end_model_span(span)

    assert span.exceptions == [err]
    assert span.attributes["error.type"] == "TimeoutError"
    assert "gen_ai.usage.input_tokens" not in span.attributes
