# Anthropic Provider — 使用 anthropic SDK
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.gateway.key_retry import call_with_key_retry
from app.gateway.key_ring import NoUsableKeyError
from app.gateway.provider import ChatMessage, ChatResponse, EmbeddingResponse, LLMProvider, ToolCall
from app.providers.sdk_compat import drop_unsupported_params

logger = logging.getLogger(__name__)

#: Anthropic 协议把 `max_tokens` 定为必填，无法像 OpenAI 那样"不发即不限"。
#: 这里的值只作为**协议下限**：当上游没有给出模型输出上限时，用它让输出只受模型
#: 自身约束，而不是被我们设一个预算提前截断（Claude 各代模型的输出上限即此量级）。
ANTHROPIC_MAX_OUTPUT_TOKENS = 64000


def _anthropic_usage(usage: object) -> tuple[int, int]:
    """把 Anthropic 的 usage 归一成 ``(总输入 token, 其中命中缓存的 token)``。

    ## 为什么必须归一

    Anthropic 的计数口径与 OpenAI **不同**：``input_tokens`` 只算"未命中缓存"的
    那部分，命中缓存与写入缓存的 token 在 ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` 两个独立字段里。直接把 ``input_tokens`` 落库
    会让总输入**少报** —— 长会话里缓存命中常占输入的大半，于是「输入 token」
    与按 token 折算的成本同时系统性偏低，而会话统计里缓存命中率恒为 0
    （``cached_tokens`` 从来没被填过)。

    归一后与 OpenAI 口径一致：总量 + 其中命中的子集。下游（截断判定、成本换算、
    缓存命中率）就只需要一套规则：``cached / input``。

    取不到就回 0 —— 统计宁可少算，也不要因为某个字段缺失让整条流式响应失败
    （与 ``cached_tokens_from_usage`` 的取舍一致）。
    """
    if usage is None:
        return 0, 0

    def _int(name: str) -> int:
        value = getattr(usage, name, 0)
        return int(value) if isinstance(value, int) and value > 0 else 0

    plain = _int("input_tokens")
    created = _int("cache_creation_input_tokens")
    read = _int("cache_read_input_tokens")
    # 兼容别的 SDK 版本把命中量放在嵌套 details 里
    if read == 0:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            nested = getattr(details, "cached_tokens", None)
            if isinstance(nested, int) and nested > 0:
                read = nested
    return plain + created + read, read


def _anthropic_cache_write(usage: object) -> int:
    """为**写**缓存多付的那部分输入 token（``cache_creation_input_tokens``）。

    单列而不并进 ``_anthropic_usage`` 的返回值，是因为那个两元组的语义
    ``(总输入, 命中)`` 已被下游（截断判定、成本换算、命中率）依赖，改签名会让
    「写入」悄悄混进「命中」——命中率恒高、差额算不出来，而 G-L3 要判的正是这个差额。
    """
    if usage is None:
        return 0
    created = getattr(usage, "cache_creation_input_tokens", 0)
    return int(created) if isinstance(created, int) and created > 0 else 0


#: 半截 arguments 回放时的预览长度。取 2000 是因为它约等于一个中等文件的头部 ——
#: 模型据此能认出"这是我上次被截断的那次 write_file"，而不会把整段内容再塞一遍。
_TRUNCATED_INPUT_PREVIEW = 2000

#: 已经告警过的 tool_call id —— 历史每轮重发，同一个坏参数会重复告警到刷屏。
#: **有界**：call id 每个请求都不同，无界集合会在长会话里一直长（进程常驻）。
_WARNED_TRUNCATED_LIMIT = 256
_warned_truncated_ids: set[str] = set()


def _warn_once_for(call_id: str) -> bool:
    """这个 call id 是否**首次**见到（是 ⇒ 调用方应当告警）。"""
    if not call_id:
        return False
    if call_id in _warned_truncated_ids:
        return False
    if len(_warned_truncated_ids) >= _WARNED_TRUNCATED_LIMIT:
        _warned_truncated_ids.clear()
    _warned_truncated_ids.add(call_id)
    return True


def _truncated_tool_input(raw: str, call_id: str) -> dict[str, Any]:
    """把**不是合法 JSON** 的 arguments 包成一个合法 object（协议要求 input 是 object）。

    ## 为什么不能抛、也不能丢

    `tool_use.input` 之于 Anthropic 是**结构化对象**，历史里必须与 `tool_result` 配对。
    半截 JSON（`finish_reason=length` 留下的、或压缩链标注过的）有两个错误方向：

    - 抛 `JSONDecodeError`：整轮对话在建请求阶段就失败，用户看到
      ``provider opencode-go-anthropic stream failed: JSONDecodeError: Unterminated
      string``，且此后**每一次**请求都会再炸一遍 —— 历史没有自愈能力。
    - 丢这条 tool_use：与它的 tool_result 变成孤儿，直接 400
      （`unexpected tool_result`），同样整轮不可用，还丢了上下文。

    所以保留成 object，并把原文头部放进 ``_raw``：结构合法、配对完整、模型看得出
    "这里当时是被截断的"。工具本就不会再执行它 —— 执行发生在当时那一轮。
    """
    if _warn_once_for(call_id):
        logger.warning(
            "Anthropic history has non-JSON tool arguments (call=%s, %d chars); "
            "replaying as placeholder instead of failing the request",
            call_id,
            len(raw),
        )
    return {
        "_raw": raw[:_TRUNCATED_INPUT_PREVIEW],
        "_note": (
            "arguments were not valid JSON (truncated by max_tokens or repaired); "
            "this call was not executed"
        ),
    }


def _tool_arguments_to_input(raw: Any, *, call_id: str = "") -> dict[str, Any]:
    """历史 tool_call 的 arguments → Anthropic 的 ``tool_use.input``（**绝不抛**）。

    合法对象 → 原样；合法但非对象（数组/标量）→ 包一层 ``_raw``（协议要求 object）；
    空串/None → ``{}``（无参调用）；**坏 JSON → 占位 object**（见
    :func:`_truncated_tool_input`，这是"经常触发 Unterminated string"的根因）。
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _truncated_tool_input(text, call_id)
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": text[:_TRUNCATED_INPUT_PREVIEW]}


def _schema_to_input_schema(raw: Any, *, tool_name: str = "") -> dict[str, Any]:
    """工具定义的 ``parameters`` → Anthropic 的 ``input_schema``（**绝不抛**）。

    同一类炸点：``func.get("parameters", json.loads(parameters_json))`` 的默认值是
    **提前求值**的 —— 哪怕 ``parameters`` 已存在，坏掉的 ``parameters_json`` 也会让整轮
    请求在建参数时抛 `JSONDecodeError`。缺/坏一律退化成"接受任意对象"，与 Anthropic
    对缺失 schema 的默认行为一致：宁可少一层约束，也不要整轮不可用。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "tool %s has non-JSON parameter schema; falling back to permissive schema",
                tool_name or "<unnamed>",
            )
            return {"type": "object", "properties": {}}
        if isinstance(parsed, dict):
            return parsed
    return {"type": "object", "properties": {}}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, api_key: str, base_url: str = "", *, key_ring: Any = None
    ) -> None:
        from app.config import settings as _settings  # 延迟导入：避免 providers ← config 循环

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        # 具名 UA：部分网关前置 Cloudflare 反滥用（如 opencode.ai），SDK 默认 UA 会 403。
        kwargs["default_headers"] = {"User-Agent": _settings.llm_http_user_agent}
        self._base_kwargs = dict(kwargs)
        self._client = anthropic.AsyncAnthropic(**kwargs)
        # 多 key(DR 集中派),与 OpenAIProvider 同型;key_ring 为 None 时单 key 直连。
        self._key_ring = key_ring
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        # 0 = 不设人为上限：`_apply_max_tokens` 会落成协议要求的固定值。
        max_tokens: int = 0,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatResponse]:
        # Anthropic 协议没有 OpenAI 式的 `response_format`（它是用 tool_choice 强制 JSON），
        # 所以本类 `supports_response_format` 保持 False，网关不会把该参数发过来；
        # 这里保留同名参数只是为了让接口形状与基类一致（子类不能改签名）。
        system_prompt, msgs = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
        }
        self._apply_max_tokens(kwargs, max_tokens)
        # OpenCode 的 Anthropic 协议端点同样强制要求 x-opencode-session
        # （缺失即 400 MissingSessionID）；用**永不为空**的稳定 ID 注入
        # （显式会话 > trace > 进程常量，见 `session_context.opencode_session_id`）。
        if self.name.startswith("opencode"):
            from app.providers.session_context import opencode_session_id  # 延迟导入：避免循环

            kwargs["extra_headers"] = {"x-opencode-session": opencode_session_id()}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        input_tokens = 0
        output_tokens = 0
        # M23 边界：流式这里**不做换 key 重试**。`client.messages.stream()` 是把"打开流"
        # 与"消费事件"包在一起的上下文管理器，而换 key 只允许发生在"还没 yield 过任何东西"
        # 之前；安全拆开它得改用手动 __aenter__/__aexit__（异常传播易错），收益不抵风险。
        # 非流式路径（chat）与 OpenAI 的三条路径都已支持重试。
        client, key_item = await self._resolve_client()
        # 只发 SDK 认的参数：anthropic 1.x 已移除采样参数（temperature/top_p/top_k），
        # 透传会得到 Python 层 TypeError 而非上游 400（见 providers/sdk_compat.py）。
        drop_unsupported_params(client.messages.stream, kwargs)
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            yield ChatResponse(content=delta.text)
                        elif hasattr(delta, "partial_json"):
                            # tool_use delta — accumulate in parent
                            pass
                    elif event.type == "message_start":
                        usage = getattr(event.message, "usage", None)
                        if usage:
                            input_tokens = getattr(usage, "input_tokens", 0)
                    elif event.type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage:
                            output_tokens = getattr(usage, "output_tokens", 0)

                # 获取完整消息以提取 tool_calls
                final = await stream.get_final_message()
                tool_calls = []
                if final.content:
                    for block in final.content:
                        if block.type == "tool_use":
                            tool_calls.append(
                                ToolCall(
                                    id=block.id,
                                    name=block.name,
                                    arguments=(
                                        block.input
                                        if isinstance(block.input, str)
                                        else __import__("json").dumps(block.input)
                                    ),
                                )
                            )
                finish = "tool_calls" if tool_calls else "stop"
                if final.stop_reason == "max_tokens":
                    finish = "length"

                # 用量口径统一在 _anthropic_usage 里：Anthropic 的 `input_tokens`
                # 只算**未命中缓存**的部分，命中/写入缓存是独立字段 —— 不合并回来，
                # 长会话的输入量与按 token 折算的成本都会系统性偏低。
                final_input, final_cached = _anthropic_usage(getattr(final, "usage", None))
                yield ChatResponse(
                    tool_calls=tool_calls,
                    finish_reason=finish,
                    input_tokens=final_input or input_tokens,
                    output_tokens=output_tokens or getattr(final.usage, "output_tokens", 0),
                    cached_tokens=final_cached,
                    # G-L3 埋点：写入侧单列（读 + 写才够算"保活值不值"）。
                    cache_write_tokens=_anthropic_cache_write(
                        getattr(final, "usage", None)
                    ),
                )
        except Exception as exc:
            # 只有**这把凭证自己**的错才上报（401/403/429）—— 与 `call_with_key_retry`
            # 的 `is_key_scoped_failure` 同一口径。
            #
            # 此前这里是**无条件** `await self._report_failure(key_item)`，于是上游的
            # 确定性 400（实测：`claude-sonnet-4-20250514 isn't included in OpenCode Go`）
            # 会被算到密钥头上 → 唯一那把 key 进入 30s 冷却 → 下一次请求直接
            # `NoUsableKeyError ... cooling=1`，真实原因被完全掩盖（子 Agent 全部 failed）。
            from app.gateway.key_retry import is_key_scoped_failure
            from app.gateway.key_ring import note_last_error

            note_last_error(self.name, exc)
            if is_key_scoped_failure(exc):
                await self._report_failure(key_item)
            else:
                logger.warning(
                    "provider %s stream failed with a non-credential error (%s); "
                    "key NOT cooled",
                    self.name,
                    type(exc).__name__,
                )
            raise

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        # 0 = 不设人为上限（同 `chat_stream`）。
        max_tokens: int = 0,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        import json as _json

        system_prompt, msgs = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
        }
        self._apply_max_tokens(kwargs, max_tokens)
        # OpenCode 的 Anthropic 协议端点同样强制要求 x-opencode-session
        # （缺失即 400 MissingSessionID）；用**永不为空**的稳定 ID 注入
        # （显式会话 > trace > 进程常量，见 `session_context.opencode_session_id`）。
        if self.name.startswith("opencode"):
            from app.providers.session_context import opencode_session_id  # 延迟导入：避免循环

            kwargs["extra_headers"] = {"x-opencode-session": opencode_session_id()}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # M23：非流式的提交点 = 这次 create() 返回；失败时换一把 key 重试（见 key_retry）。
        async def _create(client: Any) -> Any:
            # 同上：签名里没有的参数不发（新 anthropic SDK 已删除 sampling 参数）。
            drop_unsupported_params(client.messages.create, kwargs)
            return await client.messages.create(**kwargs)

        resp = await call_with_key_retry(
            provider=self.name,
            resolve=self._resolve_client,
            call=_create,
            report_failure=self._report_failure,
        )

        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=(
                            block.input
                            if isinstance(block.input, str)
                            else _json.dumps(block.input)
                        ),
                    )
                )

        finish = "tool_calls" if tool_calls else "stop"
        if resp.stop_reason == "max_tokens":
            finish = "length"

        # 与流式路径同一套口径（见 _anthropic_usage）：`input_tokens` 不含缓存部分。
        resp_input, resp_cached = _anthropic_usage(resp.usage)
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            input_tokens=resp_input,
            output_tokens=resp.usage.output_tokens,
            cached_tokens=resp_cached,
            # G-L3 埋点：写入侧单列（与流式路径同一口径）。
            cache_write_tokens=_anthropic_cache_write(resp.usage),
        )

    async def embed(self, text: str, model: str) -> EmbeddingResponse:
        # Anthropic 没有 embedding API，抛出异常让 Router 路由到 OpenAI
        raise NotImplementedError("Anthropic does not provide embedding API")

    async def close(self) -> None:
        clients = set(self._clients.values())
        clients.add(self._client)
        for c in clients:
            try:
                await c.close()
            except Exception:
                pass

    # ── 多 key helpers（与 OpenAIProvider 同型）──

    async def _resolve_client(self) -> tuple[Any, Any]:
        if self._key_ring is None:
            return self._client, None
        item = await self._key_ring.get_key(self.name)
        if item is None:
            # 与 openai.py 同名处同一口径：取不到 key 就 fail loud，绝不拿 placeholder
            # 打上游 —— 那会产出一句"看起来是上游说的"401，把有效 key 的用户推向改 key。
            diag = await self._key_ring.diagnose(self.name)
            logger.warning(
                "KeyRing 无可用 key for %s：%s（不回退到 placeholder，直接失败）",
                self.name,
                diag,
            )
            raise NoUsableKeyError(self.name, diag)
        key = item["key"]
        client = self._clients.get(key)
        if client is None:
            if len(self._clients) >= 8:
                self._clients.pop(next(iter(self._clients)))
            kwargs = dict(self._base_kwargs)
            kwargs["api_key"] = key
            client = anthropic.AsyncAnthropic(**kwargs)
            self._clients[key] = client
        return client, item

    async def _report_failure(self, item: Any) -> None:
        if self._key_ring is not None and item is not None:
            try:
                await self._key_ring.report_failure(self.name, item["key"])
            except Exception:
                logger.exception("key ring report_failure failed")

    # ── 转换 ──

    @staticmethod
    def _anthropic_content(
        content: str | list[dict[str, Any]],
    ) -> str | list[dict[str, Any]]:
        """消息 content → Anthropic 形态：字符串原样返回；parts 则逐块翻译。

        为什么要翻译：OpenAI 风格 parts 与 Anthropic blocks **不是同一种东西** —— 图片在
        前者是 `{"type":"image_url","image_url":{"url":"data:..."}}`，在后者是
        `{"type":"image","source":{"type":"base64","media_type":...,"data":...}}`。
        直接透传会让 Anthropic 收到 `text: [...]` 而 400。
        """
        if isinstance(content, str):
            return content
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": str(part.get("text", ""))})
                continue
            if part.get("type") != "image_url":
                continue
            url = str((part.get("image_url") or {}).get("url") or "")
            header, _, payload = url.partition(",")
            media_type = (
                (header[5:].split(";")[0] or "").strip().lower() if url.startswith("data:") else ""
            )
            if media_type not in ("image/png", "image/jpeg", "image/gif", "image/webp") or not payload:
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": payload},
                }
            )
        # Anthropic 不接受空的 content 数组：退化为空串（与旧行为一致）
        return blocks or ""

    @staticmethod
    def _system_text(content: str | list[dict[str, Any]]) -> str:
        """system 段的 content → **纯文本**。

        为什么要单独收口：Anthropic 的 `system` 只收文本（字符串或 `{"type":"text"}` 块），
        而 `ChatMessage.content` 是 `str | parts`。parts 形态原样透传的后果是轻则把 list 的
        Python repr 发给模型、重则 400（与 :meth:`_anthropic_content` 的注释同一件事）；
        `str.join` 则会直接 TypeError。非 text 的块（图片）在这里**如实丢弃** ——
        system 带图不是本仓库任何一条链路会产生的形态。
        """
        if isinstance(content, str):
            return content
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )

    @staticmethod
    def _apply_max_tokens(kwargs: dict[str, Any], max_tokens: int) -> None:
        """写入 Anthropic 的 `max_tokens`。

        与 OpenAI 协议的关键差别：这个字段在 Anthropic 是**必填项**，省略会被直接
        400 拒绝。所以"不限制输出"在这里不能靠"不发这个键"表达，只能落成一个足够
        大的值 —— 取该系列模型的输出上限，让输出只受模型本身约束，而不是被一个
        拍脑袋的常量提前截断。它是**协议下限**，不是我们设的预算。
        """
        kwargs["max_tokens"] = (
            max_tokens if max_tokens and max_tokens > 0 else ANTHROPIC_MAX_OUTPUT_TOKENS
        )

    @staticmethod
    def _convert_messages(
        messages: list[ChatMessage],
    ) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
        """分离 system prompt 并转换消息格式。

        C2 / 切口 2a：system 段可能是**多条**消息（稳定前缀 → 记忆 → hook 上下文，见
        `runtime._apply_system_prefix`）—— 必须**按序全部保留**。旧实现 `system =
        msg.content` 只留最后一条，会把稳定前缀整条丢掉（正确性缺陷），断点也因此无处
        可落（前缀缓存失效）。仅当存在 `cache_breakpoint` 时改用 Anthropic 的 block 数组
        并打 `cache_control: {"type": "ephemeral"}`；否则逐字保持原字符串形态（默认不变）。
        """
        system_parts: list[ChatMessage] = []
        result = []
        for msg in messages:
            if msg.role == "system":
                if msg.content:  # 空内容不入 system（与旧行为一致：空 system 不发送）
                    system_parts.append(msg)
            elif msg.role == "tool":
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            elif msg.role == "assistant" and msg.tool_calls:
                content: list[dict[str, Any]] = []
                # assistant 的 content 由 provider 产出（字符串）；这里加类型防御，
                # 免得将来多模态 parts 误入此分支被拼成 {"type":"text","text":[...]}
                if isinstance(msg.content, str) and msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            # **绝不抛**：历史里的半截 arguments 以前在这里炸成
                            # `JSONDecodeError: Unterminated string`，让整轮（且此后每一轮）
                            # 请求在建参数阶段就失败。见 `_tool_arguments_to_input`。
                            "input": _tool_arguments_to_input(
                                tc.arguments, call_id=tc.id
                            ),
                        }
                    )
                result.append({"role": "assistant", "content": content})
            else:
                result.append(
                    {
                        "role": msg.role,
                        "content": AnthropicProvider._anthropic_content(msg.content),
                    }
                )
        if any(m.cache_breakpoint for m in system_parts):
            system: str | list[dict[str, Any]] = [
                (
                    {
                        "type": "text",
                        "text": AnthropicProvider._system_text(m.content),
                        "cache_control": {"type": "ephemeral"},
                    }
                    if m.cache_breakpoint
                    else {"type": "text", "text": AnthropicProvider._system_text(m.content)}
                )
                for m in system_parts
            ]
        else:
            # 无断点：逐字保持原调用方的字符串形态（默认不变）
            system = "\n\n".join(AnthropicProvider._system_text(m.content) for m in system_parts)
        return system, result

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI tools 格式 → Anthropic tools 格式"""
        converted = []
        for t in tools:
            func = t.get("function", t)
            # **不能**写成 ``func.get("parameters", json.loads(func.get("parameters_json", "{}")))``：
            # `dict.get` 的默认值**提前求值** —— `parameters` 明明存在，坏掉的
            # `parameters_json` 也会让整轮请求在这里抛 `JSONDecodeError`。
            # 见 `_schema_to_input_schema`。
            raw_schema = func.get("parameters")
            if raw_schema is None:
                raw_schema = func.get("parameters_json")
            converted.append(
                {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": _schema_to_input_schema(
                        raw_schema, tool_name=str(func.get("name", ""))
                    ),
                }
            )
        return converted
