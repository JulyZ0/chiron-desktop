"""OpenAI 兼容对话端点（``POST /v1/chat/completions``）。

为什么需要它：用户拿到一把 ``sk-`` 开头的网关 API Key 之后，此前**没有任何可以真正
调用它的推理端点** —— ``GET /v1/models`` 只列模型清单，``POST /v1/desktop/infer``
是本系统私有的片段推理形状（``{model, messages}`` → ``{content, usage}``），
``POST /v1/chat/submit`` 是 Agent 运行时的异步 SSE 协议。三者都不是 OpenAI 客户端
能直接接入的形状，于是"界面发了 key，却没有任何接口能用它"。

本端点只做**形状适配**：推理仍然走 ``GatewayRouter``（同一套 provider 选择、缓存、
预算），不新写一条引擎逻辑 —— 多一条推理路径就多一处口径漂移。

支持的参数：``model`` / ``messages`` / ``temperature`` / ``max_tokens``
（``max_completion_tokens`` 同义）/ ``stream`` / ``response_format``。

**其余参数一律忽略**（``top_p`` / ``n`` / ``stop`` / ``logprobs`` …）：兼容层的价值
在于"现有客户端能直接接上"，对调优参数吹毛求疵只会把本来能用的请求挡在门外。
唯一例外是 ``tools`` —— 它不是调优参数，静默忽略会让调用方收到一段"该调工具却在
闲聊"的回答，所以显式 400，而不是假装支持。

只接受纯文本 ``content``：多模态 parts 的转写路径没有在本端点验证过，接受它等于把
"没测过的形状"暴露成契约。

计费：**引擎不记账**。用量随响应体回到 Go 网关，由网关写 ``usage_events``（与
``/v1/chat/submit`` 同一张表、同一套 ``cost_cents`` 定价）。两处各记一份就会重复
计费，这也是 ``desktop_infer`` 的既有口径（contracts.md §9）。

身份：复用 ``AuthMiddleware`` 的网关代理路径（``X-Internal-Token`` +
``?tenant_id=&user_id=``），这里只读中间件已经设好的 ``request.state.tenant_id``。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openai"])

#: 单次请求的消息总字符上限。与 ``desktop_infer`` 同口径：整本文档塞进来既会撞上游
#: 上下文上限，也会把一次请求的账单算到用户头上。
MAX_TOTAL_CHARS = 2_000_000

#: 单次输出上限。对话回答比片段推理长，但仍要有个上界 —— 它同时压住"一次误操作
#: 掏空额度"的风险。
MAX_OUTPUT_TOKENS = 8192

#: OpenAI 的 ``finish_reason`` 取值。引擎内部的 ``""``（未明确）按 ``stop`` 处理：
#: 客户端只认这一组值，透传空串会让 SDK 的 finish_reason 分支落空。
_ALLOWED_FINISH = {"stop", "length", "content_filter", "tool_calls"}


class ChatMessageIn(BaseModel):
    """一条输入消息：**只允许纯文本**（理由见模块 docstring）。

    ``content`` 允许为 ``None``：OpenAI 客户端在 assistant 消息里带
    ``"content": null``（该轮只有工具调用的占位）是常态。这里接受它并按空串处理，
    而不是 422 —— 那会把一条完全合法的历史记录判成非法请求。
    """

    role: Literal["system", "user", "assistant"]
    content: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _reject_parts(cls, v: Any) -> Any:
        if isinstance(v, list):
            raise ValueError(
                "multimodal content parts are not supported on this endpoint; "
                "send a plain string"
            )
        return v


class ChatCompletionRequest(BaseModel):
    """``POST /v1/chat/completions`` 的请求体（只解析本端点真正会用的字段）。"""

    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=200)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    #: OpenAI 的两种写法都接受：新 SDK 发 ``max_completion_tokens``，旧客户端发
    #: ``max_tokens``。两者都给时以 ``max_tokens`` 为准（旧字段是更明确的"我要多少"）。
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    stream: bool = False
    response_format: dict[str, Any] | None = None
    #: 只为**显式拒绝**而声明（见模块 docstring）；值本身不参与任何逻辑。
    tools: list[Any] | None = None
    tool_choice: Any | None = None

    @model_validator(mode="after")
    def _enforce_limits(self) -> ChatCompletionRequest:
        total = sum(len(m.content or "") for m in self.messages)
        if total > MAX_TOTAL_CHARS:
            # 只报长度，不回显内容 —— 消息里可能整段是用户的私有文本。
            raise ValueError(f"messages too large: {total} chars > {MAX_TOTAL_CHARS}")
        return self

    @property
    def effective_max_tokens(self) -> int:
        """实际下发给出上游的输出上限。"""
        if self.max_tokens:
            return self.max_tokens
        if self.max_completion_tokens:
            return self.max_completion_tokens
        return 1024


def _completion_id() -> str:
    """OpenAI 风格的响应 id。客户端只当作不透明字符串，不需要可解析。"""
    return "chatcmpl-" + uuid.uuid4().hex


def _finish_reason(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return "stop"
    return value if value in _ALLOWED_FINISH else "stop"


def _usage_block(input_tokens: int, output_tokens: int, cached_tokens: int) -> dict[str, Any]:
    """OpenAI 的 ``usage`` 形状。

    ``prompt_tokens`` 是**输入总量**（缓存命中部分是其子集），所以不能把
    ``cached_tokens`` 再加一遍 —— 那会让客户端算出的成本虚高。
    """
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
    }


def _error(status: int, message: str, err_type: str, code: str) -> JSONResponse:
    """OpenAI 形状的错误体。

    为什么不用 FastAPI 默认的 ``{"detail": ...}``：OpenAI SDK 只认
    ``error.message``，别的形状它会把整个 body 塞进异常文本 —— 用户看到一串
    JSON，却看不到"为什么失败"。
    """
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


async def _gateway_or_502() -> Any:
    from app.main import get_gateway  # 局部导入：避免 main ←→ api 的循环导入

    try:
        return await get_gateway()
    except RuntimeError:
        logger.warning("openai compat: engine gateway unavailable")
        raise _upstream_error() from None


def _upstream_error() -> HTTPException:
    """上游不可用/出错时的统一出口。

    细节只进日志：异常原文可能带上游 URL、key 片段或内部主机名，而那些不该出现在
    一个对外开放的 API 响应里。
    """
    return HTTPException(status_code=502, detail="upstream inference failed")


def _to_messages(body: ChatCompletionRequest) -> list[dict[str, Any]]:
    """转成 gateway 认识的裸 dict（``normalize_messages`` 会再规范化为 ChatMessage）。"""
    return [{"role": m.role, "content": m.content or ""} for m in body.messages]


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request) -> Any:
    """OpenAI 兼容的对话补全：非流式返回 ``chat.completion``，流式返回 SSE chunk。"""
    tenant_id = int(getattr(request.state, "tenant_id", 0) or 0)

    # 只打元信息：模型、消息条数、总字符数。**content 一个字都不进日志**。
    logger.info(
        "openai compat: model=%s messages=%d chars=%d stream=%s tenant=%s",
        body.model,
        len(body.messages),
        sum(len(m.content or "") for m in body.messages),
        body.stream,
        tenant_id,
    )

    if body.tools or body.tool_choice:
        # 显式拒绝而不是静默忽略：调用方期待的是 tool_calls，拿到一段普通回答会
        # 在它自己的解析分支里静默出错，比这里直接说清楚难查得多。
        return _error(
            400,
            "tools/tool_choice are not supported by this endpoint yet; "
            "send a plain chat request",
            "invalid_request_error",
            "tools_not_supported",
        )

    gateway = await _gateway_or_502()
    messages = _to_messages(body)
    completion_id = _completion_id()
    created = int(time.time())

    if not body.stream:
        return await _complete(
            gateway, body, messages, tenant_id, completion_id, created
        )
    return _stream(
        gateway, body, messages, tenant_id, completion_id, created
    )


def _session_id_for(messages: list[dict[str, Any]], model: str) -> str:
    """OpenAI 兼容端点的稳定会话标识（给 provider 的 ``x-opencode-session`` 用）。

    取**首条 user 消息 + 模型名**的哈希而不是每次请求新生成的 ``completion_id``：
    同一个 OpenAI 客户端连续多轮会把完整历史再来一遍，哈希稳定 ⇒ 上游路由与 prompt
    缓存能命中；`completion_id` 每轮都变，等于每轮换一条路由。

    空消息历史返回空串 —— 此时交给 provider 层兜底（trace / 进程常量），不会 400。
    """
    import hashlib

    first_user = next(
        (str(m.get("content") or "") for m in messages if m.get("role") == "user"),
        "",
    )
    if not first_user:
        return ""
    digest = hashlib.sha1(f"{model}\n{first_user[:2000]}".encode()).hexdigest()[:16]
    return f"compat-{digest}"


async def _complete(
    gateway: Any,
    body: ChatCompletionRequest,
    messages: list[dict[str, Any]],
    tenant_id: int,
    completion_id: str,
    created: int,
) -> Any:
    """非流式：一次性拿到完整回答，包成 ``chat.completion``。"""
    from app.providers.session_context import reset_llm_session_id, set_llm_session_id

    _token = set_llm_session_id(_session_id_for(messages, body.model))
    try:
        resp = await gateway.chat(
            messages,
            body.model,
            tenant_id=tenant_id,
            max_tokens=body.effective_max_tokens,
            temperature=body.temperature,
            response_format=body.response_format,
        )
    except Exception as exc:  # noqa: BLE001 — 上游任何异常都归一到 502（细节只进日志）
        logger.warning(
            "openai compat: upstream failure: %s: %s", type(exc).__name__, exc
        )
        raise _upstream_error() from None
    finally:
        reset_llm_session_id(_token)

    if resp.finish_reason == "error":
        # gateway 把 provider 的失败包成 error 响应（不抛异常），这里补上状态码。
        logger.warning(
            "openai compat: provider error (provider=%s): %s",
            resp.provider or "-",
            resp.message or "-",
        )
        if resp.error_code == "rate_limited":
            raise HTTPException(
                status_code=429, detail=resp.message or "model rate limit exceeded"
            )
        raise _upstream_error()

    message: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
    if resp.reasoning_content:
        # 思考型模型的过程文本：OpenAI 兼容客户端会忽略未知字段，但保留它便于
        # 排查"为什么回答是这个"。
        message["reasoning_content"] = resp.reasoning_content

    return JSONResponse(
        content={
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": resp.model or body.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": _finish_reason(resp.finish_reason),
                    "logprobs": None,
                }
            ],
            "usage": _usage_block(
                resp.input_tokens, resp.output_tokens, resp.cached_tokens
            ),
        }
    )


def _sse(payload: dict[str, Any]) -> bytes:
    """一帧 SSE。``ensure_ascii=False`` 让中文按 UTF-8 直出（帧体积小一半）。"""
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _stream(
    gateway: Any,
    body: ChatCompletionRequest,
    messages: list[dict[str, Any]],
    tenant_id: int,
    completion_id: str,
    created: int,
) -> StreamingResponse:
    """流式：把 gateway 的增量转成 ``chat.completion.chunk`` 帧。

    最后一帧带 ``usage``（``choices`` 为空，与 OpenAI 的 ``include_usage`` 行为一致），
    再以 ``data: [DONE]`` 收尾 —— 少了这一行，OpenAI SDK 会一直等下一帧。
    """

    def _frame(
        delta: dict[str, Any],
        finish: str | None = None,
        usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            # 上游可能把请求的别名解析成真实模型名（如 ``gpt-4o`` → ``gpt-4o-2024-…``）。
            # 一旦拿到就改用真实值：客户端会把它当"这次到底是谁在回答"的依据。
            "model": model or body.model,
            "choices": []
            if usage is not None
            else [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}],
        }
        if usage is not None:
            payload["usage"] = usage
        return _sse(payload)

    async def _generate() -> AsyncIterator[bytes]:
        # 首帧只带 role：OpenAI 的第一个 chunk 就是它，部分 SDK 靠它初始化消息对象。
        yield _frame({"role": "assistant", "content": ""})

        total_input = 0
        total_output = 0
        total_cached = 0
        finish = "stop"
        model = body.model

        # 与 `_complete` 同一条：provider 需要稳定的会话标识（OpenCode 缺失即 400）。
        # 生成器在**自己的**上下文里跑（StreamingResponse 换 task），所以必须在这里设/复位。
        from app.providers.session_context import (
            reset_llm_session_id,
            set_llm_session_id,
        )

        _token = set_llm_session_id(_session_id_for(messages, body.model))

        try:
            async for chunk in gateway.chat_stream(
                messages,
                body.model,
                tenant_id=tenant_id,
                max_tokens=body.effective_max_tokens,
                temperature=body.temperature,
                response_format=body.response_format,
            ):
                if chunk.model:
                    model = chunk.model
                if chunk.input_tokens:
                    total_input += chunk.input_tokens
                if chunk.output_tokens:
                    total_output += chunk.output_tokens
                if chunk.cached_tokens:
                    total_cached += chunk.cached_tokens

                if chunk.finish_reason == "error":
                    # 流已经开始，改不了状态码。按 OpenAI 的错误帧形状发一条，
                    # 让 SDK 抛出的异常带上真实原因（而不是"流意外结束"）。
                    logger.warning(
                        "openai compat: stream provider error: %s", chunk.message or "-"
                    )
                    rate_limited = chunk.error_code == "rate_limited"
                    yield _sse(
                        {
                            "error": {
                                "message": chunk.message or "upstream inference failed",
                                "type": "rate_limit_error" if rate_limited else "upstream_error",
                                "code": "rate_limited" if rate_limited else "upstream_failed",
                            }
                        }
                    )
                    return
                if chunk.finish_reason:
                    finish = _finish_reason(chunk.finish_reason)

                if chunk.reasoning_content:
                    yield _frame({"reasoning_content": chunk.reasoning_content}, model=model)
                if chunk.content:
                    yield _frame({"content": chunk.content}, model=model)
        except Exception as exc:  # noqa: BLE001 — 流中途异常同样只能走错误帧
            logger.warning(
                "openai compat: stream failed: %s: %s", type(exc).__name__, exc
            )
            yield _sse(
                {
                    "error": {
                        "message": "upstream inference failed",
                        "type": "upstream_error",
                        "code": "upstream_failed",
                    }
                }
            )
            return
        finally:
            # 客户端断开（GeneratorExit / CancelledError）时这里**不能** await，
            # 但记账由 Go 网关按已收到的用量完成，引擎侧无额外副作用。
            # 会话上下文必须复位（同步操作，不涉及 await）。
            reset_llm_session_id(_token)

        yield _frame({}, finish=finish, model=model)
        yield _frame(
            {}, usage=_usage_block(total_input, total_output, total_cached), model=model
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（nginx / 云 LB）默认会在缓冲满之前压住响应体，流式体验会退化成
            # "等半天、然后一次性吐完"。
            "X-Accel-Buffering": "no",
        },
    )
