"""H11：Provider 流式终态必须是"合法终态"，否则失败（不提交半成品回合）。

缺陷形态（`app/providers/responses.py::_parse_stream`）：
  1. `except json.JSONDecodeError: continue` —— **畸形事件被静默跳过**：一帧损坏的
     `function_call` 就等于"少了一个工具调用"，模型这一回合看起来正常结束；
  2. 流结束若既没有 `completed` 也没有工具调用，函数**什么都不产出** ——
     调用方拿到空流，要么当成成功、要么卡住（取决于上层写法）；
  3. 已经收集到 tool_calls 但流被截断（没有 `completed`）时，仍然会把工具调用发出去
     ⇒ **partial attempt 执行了后续工具**（副作用发生在一次"没成功"的回合里）。

修法：任何"非合法终态"都产出 `finish_reason="error"`，并在 message 里写清原因
（畸形帧数 / 被截断），让上层按失败处理、不提交、不执行工具。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest

from app.providers.responses import ResponsesProvider


class _FakeResponse:
    """只实现 `aiter_lines()` 的最小替身。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _provider() -> ResponsesProvider:
    return ResponsesProvider("k", "https://api.openai.com/v1")


async def _drain(lines: list[str]) -> list[Any]:
    out = []
    async for chunk in _provider()._parse_stream(_FakeResponse(lines)):  # noqa: SLF001
        out.append(chunk)
    return out


def _data(obj: Any) -> str:
    return f"data: {json.dumps(obj)}"


@pytest.mark.asyncio
async def test_completed_stream_yields_stop() -> None:
    chunks = await _drain([
        _data({"type": "response.output_text.delta", "delta": "hi"}),
        _data({"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 1}}}),
    ])
    assert chunks[0].content == "hi"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_tool_calls_require_completed_event() -> None:
    """有工具调用但**没有** completed ⇒ 视为截断，不得把工具调用交出去。"""
    chunks = await _drain([
        _data({
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
        }),
    ])
    assert all(not c.tool_calls for c in chunks), "截断的回合不得返回工具调用"
    assert chunks[-1].finish_reason == "error"


@pytest.mark.asyncio
async def test_tool_calls_with_completed_event_are_returned() -> None:
    chunks = await _drain([
        _data({
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
        }),
        _data({"type": "response.completed", "response": {"usage": {}}}),
    ])
    calls = [c for c in chunks if c.tool_calls]
    assert calls, "合法终态下必须返回工具调用"
    assert calls[-1].tool_calls[0].name == "read_file"
    assert calls[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_empty_stream_is_an_error_not_silence() -> None:
    """一条事件都没有 ⇒ 必须有明确失败，而不是空流（空流会被上层当成成功）。"""
    chunks = await _drain([])
    assert chunks, "空流必须产出失败的终态"
    assert chunks[-1].finish_reason == "error"


@pytest.mark.asyncio
async def test_malformed_frames_are_counted_not_silently_skipped() -> None:
    chunks = await _drain([
        "data: {this is not json",
        _data({"type": "response.output_text.delta", "delta": "ok"}),
    ])
    assert chunks[-1].finish_reason == "error", "畸形帧存在且无合法终态 ⇒ 失败"
    assert "malformed" in (chunks[-1].message or "").lower()


@pytest.mark.asyncio
async def test_malformed_frames_do_not_block_a_good_terminal() -> None:
    """有畸形帧但**终态合法**：仍然成功（否则一次小损坏会废掉整回合）。"""
    chunks = await _drain([
        "data: {oops",
        _data({"type": "response.completed", "response": {"usage": {}}}),
    ])
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_failed_event_is_reported_as_error() -> None:
    chunks = await _drain([
        _data({"type": "response.failed", "response": {"error": {"message": "boom"}}}),
    ])
    assert chunks[-1].finish_reason == "error"
    assert "boom" in (chunks[-1].message or "")


@pytest.mark.asyncio
async def test_usage_only_emitted_with_terminal() -> None:
    """token 计数只在合法终态随结果给出 —— 截断回合不该产生"用量正常"的印象。"""
    chunks = await _drain([
        _data({"type": "response.output_text.delta", "delta": "x"}),
        _data({"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 2}}}),
    ])
    assert chunks[-1].input_tokens == 7
    assert chunks[-1].output_tokens == 2
