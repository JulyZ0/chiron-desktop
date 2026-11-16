"""OpenAI 兼容对话端点（``POST /v1/chat/completions``）的契约测试。

盯三类东西：

1. **形状是契约** —— 响应必须是 OpenAI 的 ``chat.completion``（``choices[].message``
   / ``usage.prompt_tokens`` …），**不能**包本系统的 ``{success,data}`` 外壳：任何
   OpenAI SDK 都不认那层壳，包了就等于"兼容层只对本仓库前端有效"。
2. **闸门要显式** —— 不支持的参数（``tools`` / 多模态 parts / ``n>1``）一律 4xx 说清
   原因，而不是静默忽略后返回一段形状不对的回答；调用方会在自己的解析分支里静默出错。
3. **错误不泄露上游** —— provider 失败、gateway 抛异常、gateway 未初始化，对外都是
   固定文案；异常原文可能带上游 URL / key 片段 / 内部主机名，只进日志。

全部用 mock gateway + ASGITransport 发真实 HTTP 请求（与 ``test_desktop_infer.py``
同型）：不碰网络、不花钱、不需要真实 provider key。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.gateway.provider import ChatResponse
from app.main import create_app

URL = "/v1/chat/completions"

# 网关代理路径的身份（``?user_id=&tenant_id=`` + conftest 注入的 X-Internal-Token）。
IDENTITY = {"user_id": "test-user", "tenant_id": "1"}

#: 断言"日志里不能出现它"用的哨兵。
SECRET_TEXT = "TOP-SECRET-PROMPT-TEXT"


class _FakeGateway:
    """记录调用参数的 gateway 替身（非流式路径）。"""

    def __init__(
        self,
        response: ChatResponse | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or ChatResponse(
            content="pong",
            finish_reason="stop",
            input_tokens=11,
            output_tokens=7,
            cached_tokens=3,
            provider="openai",
            model="gpt-4o-mini",
        )
        self._exc = exc

    async def chat(
        self, messages: list[Any], model: str, **kwargs: Any
    ) -> ChatResponse:
        self.calls.append({"messages": messages, "model": model, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._response

    async def chat_stream(self, messages: list[Any], model: str, **kwargs: Any) -> Any:
        raise AssertionError("本用例只走非流式路径")


class _FakeStreamGateway:
    """流式替身：按给定顺序吐出 chunk。"""

    def __init__(self, chunks: list[ChatResponse]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = chunks

    async def chat(self, messages: list[Any], model: str, **kwargs: Any) -> ChatResponse:
        raise AssertionError("本用例只走流式路径")

    async def chat_stream(  # type: ignore[override]
        self, messages: list[Any], model: str, **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": messages, "model": model, **kwargs})
        for chunk in self._chunks:
            yield chunk


def _payload(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "ping"}],
    }
    body.update(over)
    return body


async def _post(
    payload: dict[str, Any],
    *,
    params: dict[str, str] | None = None,
) -> Any:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            URL,
            json=payload,
            params=params if params is not None else IDENTITY,
        )


# ── 响应形状 ──────────────────────────────────────────────────────────


async def test_success_returns_openai_shape() -> None:
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 顶层键就是 OpenAI 契约：多一层 {success,data} 会让 SDK 直接解不出来
    assert set(body) == {"id", "object", "created", "model", "choices", "usage"}
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "gpt-4o-mini"

    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant", "content": "pong"}

    # prompt_tokens 是输入总量（缓存命中是其子集），不能把 cached 再加一遍
    assert body["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 3},
    }


async def test_request_is_forwarded_to_gateway() -> None:
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(
            _payload(
                temperature=0.2,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
        )
    assert resp.status_code == 200, resp.text
    call = fake.calls[0]

    # 身份必须来自中间件解析出的 tenant，而不是请求体里自报的字段
    assert call["tenant_id"] == 1
    assert call["model"] == "gpt-4o-mini"
    assert call["max_tokens"] == 256
    assert call["temperature"] == pytest.approx(0.2)
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"] == [{"role": "user", "content": "ping"}]


async def test_max_completion_tokens_is_accepted_as_alias() -> None:
    """新 SDK 发 ``max_completion_tokens``，旧客户端发 ``max_tokens``，两者都认。"""
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(_payload(max_completion_tokens=64))
    assert resp.status_code == 200, resp.text
    assert fake.calls[0]["max_tokens"] == 64


async def test_assistant_message_with_null_content_is_accepted() -> None:
    """``"content": null`` 是 OpenAI 历史记录里的常态，判成 422 会挡掉合法请求。"""
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(
            _payload(
                messages=[
                    {"role": "user", "content": "ping"},
                    {"role": "assistant", "content": None},
                ]
            )
        )
    assert resp.status_code == 200, resp.text
    assert fake.calls[0]["messages"][1] == {"role": "assistant", "content": ""}


# ── 闸门：显式 4xx，而不是静默忽略 ────────────────────────────────────


async def test_tools_are_rejected() -> None:
    """静默忽略 ``tools`` 会让调用方收到"该调工具却在闲聊"的回答，比报错难查得多。"""
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(
            _payload(tools=[{"type": "function", "function": {"name": "f"}}])
        )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "tools_not_supported"
    # 没被上游碰过
    assert fake.calls == []


async def test_multimodal_content_parts_are_rejected() -> None:
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(
            _payload(
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "ping"}],
                    }
                ]
            )
        )
    assert resp.status_code == 422, resp.text
    assert fake.calls == []


@pytest.mark.parametrize(
    "over",
    [
        {"messages": []},
        {"model": ""},
        {"messages": [{"role": "tool", "content": "x"}]},
    ],
)
async def test_invalid_requests_are_rejected(over: dict[str, Any]) -> None:
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(_payload(**over))
    assert resp.status_code == 422, resp.text
    assert fake.calls == []


# ── 错误映射：固定文案，不泄露上游 ────────────────────────────────────


async def test_provider_error_maps_to_502() -> None:
    """gateway 不抛异常、而是返回 error 响应时的补位（它封装了 provider 失败）。"""
    fake = _FakeGateway(
        response=ChatResponse(
            content="",
            finish_reason="error",
            message="provider openai failed: ConnectError https://internal.example",
            provider="openai",
        )
    )
    with patch("app.main._gateway", fake):
        resp = await _post(_payload())
    assert resp.status_code == 502, resp.text
    assert "upstream inference failed" in resp.text
    assert "ConnectError" not in resp.text
    assert "internal.example" not in resp.text


async def test_gateway_exception_maps_to_502() -> None:
    fake = _FakeGateway(exc=RuntimeError("connect to https://internal.example/v1 failed"))
    with patch("app.main._gateway", fake):
        resp = await _post(_payload())
    assert resp.status_code == 502, resp.text
    assert "internal.example" not in resp.text


async def test_gateway_not_initialized_maps_to_502() -> None:
    with patch("app.main._gateway", None):
        resp = await _post(_payload())
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "upstream inference failed"


# ── 流式：帧序与收尾 ──────────────────────────────────────────────────


async def _frames(resp: Any) -> list[Any]:
    """把 SSE 响应拆成帧（``[DONE]`` 保留为字符串）。"""
    out: list[Any] = []
    for line in resp.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


async def test_stream_frames_follow_openai_protocol() -> None:
    fake = _FakeStreamGateway(
        [
            ChatResponse(content="po", input_tokens=5, model="gpt-4o-mini"),
            ChatResponse(content="ng", output_tokens=2, finish_reason="stop"),
        ]
    )
    with patch("app.main._gateway", fake):
        resp = await _post(_payload(stream=True))
    assert resp.status_code == 200, resp.text

    frames = await _frames(resp)
    assert frames[-1] == "[DONE]", "缺少 [DONE]，OpenAI SDK 会一直等下一帧"

    chunks = frames[:-1]
    # 首帧只带 role：部分 SDK 靠它初始化消息对象
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c["choices"]
    )
    assert text == "pong"

    # 倒数第二帧是 usage 帧：choices 为空，与 OpenAI 的 include_usage 行为一致
    usage_frame = chunks[-1]
    assert usage_frame["choices"] == []
    assert usage_frame["usage"]["prompt_tokens"] == 5
    assert usage_frame["usage"]["completion_tokens"] == 2
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


async def test_stream_provider_error_emits_error_frame() -> None:
    """流已开始，改不了状态码 —— 按 OpenAI 的错误帧形状发一条。"""
    fake = _FakeStreamGateway(
        [
            ChatResponse(content="po"),
            ChatResponse(finish_reason="error", message="upstream 500"),
        ]
    )
    with patch("app.main._gateway", fake):
        resp = await _post(_payload(stream=True))
    assert resp.status_code == 200, resp.text
    frames = await _frames(resp)
    errors = [f for f in frames if isinstance(f, dict) and "error" in f]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "upstream_failed"
    assert "[DONE]" not in frames, "错误帧之后不该再发 [DONE]（那表示正常结束）"


# ── 隐私：内容不进日志 ────────────────────────────────────────────────


async def test_message_content_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    fake = _FakeGateway()
    with patch("app.main._gateway", fake):
        resp = await _post(
            _payload(messages=[{"role": "user", "content": SECRET_TEXT}])
        )
    assert resp.status_code == 200, resp.text
    # 元信息要留下（排查"为什么慢 / 为什么 502"够用）
    assert "openai compat" in caplog.text
    # 对话正文一个字都不能进日志
    assert SECRET_TEXT not in caplog.text
