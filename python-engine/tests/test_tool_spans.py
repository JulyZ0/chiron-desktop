"""R9：工具 span 只记元数据（名称/租户/用户/耗时/错误类型）。"""
from __future__ import annotations

from app.observability import tool_spans


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}
        self.ended = False

    def set_attribute(self, key, value):  # noqa: ANN001
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.names: list[str] = []

    def start_span(self, name: str) -> _FakeSpan:
        self.names.append(name)
        span = _FakeSpan()
        self.spans.append(span)
        return span


def test_tool_span_success_and_error(monkeypatch) -> None:
    tracer = _FakeTracer()
    monkeypatch.setattr(tool_spans.trace, "get_tracer", lambda _name: tracer)

    span = tool_spans.start_tool_span(
        name="read_file", source="builtin", tenant_id=3, user_id="u1"
    )
    tool_spans.finish_tool_span(span, started_at=0.0)

    assert tracer.names == ["execute_tool read_file"]
    assert span.attributes["tool.name"] == "read_file"
    assert span.attributes["tool.source"] == "builtin"
    assert span.attributes["chiron.tenant_id"] == 3
    assert span.attributes["chiron.user_id"] == "u1"
    assert span.attributes["tool.success"] is True
    assert span.attributes["tool.duration_ms"] >= 0
    assert span.ended is True

    err_span = tool_spans.start_tool_span(name="shell_exec")
    tool_spans.finish_tool_span(err_span, started_at=0.0, error="boom")
    assert err_span.attributes["error.type"] == "ToolError"
    assert err_span.attributes["error.message"] == "boom"
    assert "tool.success" not in err_span.attributes
