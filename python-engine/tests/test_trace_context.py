"""R9：X-Chiron-Trace-Id → contextvar 的请求级传递。"""
from __future__ import annotations

import pytest

from app.observability.trace_context import (
    TraceHeaderMiddleware,
    current_trace_id,
)


@pytest.mark.asyncio
async def test_middleware_sets_and_resets_trace_id() -> None:
    seen: list[str] = []

    async def app(scope, receive, send):  # noqa: ANN001, ARG001
        seen.append(current_trace_id())

    middleware = TraceHeaderMiddleware(app)
    scope = {"type": "http", "headers": [(b"x-chiron-trace-id", b"abc123")]}
    await middleware(scope, None, None)

    assert seen == ["abc123"]
    assert current_trace_id() == "", "请求结束后必须还原，避免串到下一个请求"


@pytest.mark.asyncio
async def test_middleware_truncates_and_ignores_bad_headers() -> None:
    seen: list[str] = []

    async def app(scope, receive, send):  # noqa: ANN001, ARG001
        seen.append(current_trace_id())

    await TraceHeaderMiddleware(app)(
        {"type": "http", "headers": [(b"x-chiron-trace-id", b"x" * 100)]}, None, None
    )
    await TraceHeaderMiddleware(app)({"type": "http", "headers": []}, None, None)

    assert len(seen[0]) == 64
    assert seen[1] == ""
