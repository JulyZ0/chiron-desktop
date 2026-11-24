# Gateway 路由器 — 多 Provider 加权选择 + 熔断 + 降级
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from app.gateway.budget import TokenBudget
from app.gateway.cache import SemanticCache
from app.gateway.circuit_breaker import CircuitBreaker
from app.gateway.errors import (
    is_deterministic_client_error,
    is_model_unavailable,
)
from app.gateway.key_ring import note_last_error
from app.gateway.model_compat import (
    DIRECT_PROVIDER_BY_FAMILY,
    model_family,
    plan_allowlist,
    provider_supports_model,
)
from app.gateway.provider import ChatMessage, ChatResponse, EmbeddingResponse, LLMProvider
from app.observability.genai import (
    end_model_span,
    record_model_error,
    record_model_usage,
    start_model_span,
)

logger = logging.getLogger(__name__)

#: 预算预检用的**代表值**。预检本来就是粗估（`budget.check` 只比 `used + est <= limit`），
#: 而 `max_tokens <= 0`（不限制输出）时它拿不到可用估计。退回历史默认额度，既不让预检
#: 退化成"永远放行"，也不假装知道真实输出量。
_PREFLIGHT_TOKENS = 4096


def normalize_messages(messages: list[Any]) -> list[ChatMessage]:
    """将裸 dict 消息统一规范化为 ChatMessage。

    历史上多处调用方（task_router/skill 工具/workflow 引擎等）直接传
    ``[{"role": ..., "content": ...}]``，而缓存层 ``_exact_key`` 调用
    ``m.to_dict()``、语义层访问 ``m.role/m.content`` — dict 会在入口处
    抛 AttributeError。网关入口统一转换，调用方无需感知。
    （冒烟测试 2026-08-21 实测踩坑后引入）
    """
    normalized: list[ChatMessage] = []
    for m in messages:
        if isinstance(m, ChatMessage):
            normalized.append(m)
        elif isinstance(m, dict):
            normalized.append(
                ChatMessage(
                    role=str(m.get("role", "user")),
                    content=m.get("content") or "",
                    tool_call_id=str(m.get("tool_call_id", "") or ""),
                    # C2：缓存断点标记要透传到 provider 边界（否则"标了没用"）
                    cache_breakpoint=bool(m.get("cache_breakpoint")),
                )
            )
        else:
            raise TypeError(f"unsupported message type: {type(m).__name__}")
    return normalized


class GatewayRouter:
    """
    核心路由器 — 所有 LLM 调用的统一入口。

    路由决策:
      1. provider_hint 指定 → 直接用（如果未熔断）
      2. 找到支持 model 的所有 providers
      3. 过滤已熔断的
      4. 加权随机选择: score = w_cost * (1/price) + w_latency * (1/latency) + w_quality
      5. 全部熔断 → 尝试 fallback provider
      6. 租户路由配置（从 Go 网关同步）覆盖默认选择

    缓存: chat 请求自动走 SemanticCache
    预算: chat 请求自动扣减 TokenBudget
    限流: 调用方在 middleware 层做，此处不重复
    """

    # Provider 默认定价 ($/1M tokens)，用于加权路由
    DEFAULT_COST: dict[str, float] = {
        "anthropic": 3.0,
        "openai": 5.0,
        "deepseek": 0.2,
    }
    DEFAULT_QUALITY: dict[str, float] = {
        "anthropic": 0.95,
        "openai": 0.90,
        "deepseek": 0.80,
    }

    # 探活模型兜底表（provider → 廉价模型名）。目录项的 probe_model 优先，
    # 未列出的 provider 不做探活（避免用错模型名触发 404 误开熔断）。
    PROBE_MODELS: dict[str, str] = {
        "openai": "gpt-3.5-turbo",
        "anthropic": "claude-3-haiku-20240307",
        "deepseek": "deepseek-chat",
        "google": "gemini-2.0-flash",
        "xai": "grok-beta",
        "groq": "llama-3.1-8b-instant",
        "mistral": "mistral-small-latest",
        "moonshot": "moonshot-v1-8k",
        "zhipu": "glm-4-flash",
        "dashscope": "qwen-turbo",
        "minimax": "abab6.5s-chat",
        "baichuan": "Baichuan4",
        "hunyuan": "hunyuan-lite",
        "stepfun": "step-1-8k",
        "openrouter": "openai/gpt-4o-mini",
        "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
        "ollama": "llama3.2",
    }

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        cache: SemanticCache | None = None,
        budget: TokenBudget | None = None,
        rate_limiter: Any | None = None,
        user_rate_limiter: Any | None = None,
        token_quota: Any | None = None,
        weights: dict[str, float] | None = None,
        gateway_url: str = "",
        internal_token: str = "",
        provider_catalog: list[dict[str, Any]] | None = None,
        provider_refresh: Callable[[], Awaitable[int]] | None = None,
    ):
        self._providers = providers
        # 服务提供商目录（app/providers/catalog.py，权威源在 Go 网关）：驱动
        # 「模型名 → provider」匹配与加权路由的成本/质量分；为空时回落内建默认。
        self._catalog: dict[str, dict[str, Any]] = {
            str(item.get("id")): item
            for item in (provider_catalog or [])
            if isinstance(item, dict) and item.get("id")
        }
        self._breakers = {
            name: CircuitBreaker(provider=name, kind="chat") for name in providers
        }
        # embed 用**独立**的熔断表：embeddings 打不通不该让聊天也不可用。
        # 真实故障：opencode-go 的 base_url 下没有 /embeddings（返回 HTML 404），
        # 该失败连续累积把**共用**的熔断器打到 15 次，于是连 chat_stream 都进不去 ——
        # 用户看到的却是 "no LLM provider available for model=…"，把方向引到了模型上。
        self._embed_breakers = {
            name: CircuitBreaker(provider=name, kind="embed") for name in providers
        }
        self._latencies: dict[str, float] = {
            name: 500.0 for name in providers
        }  # moving avg ms
        self._cache = cache
        self._budget = budget
        # 模型调用限流（租户级）：与 HTTP 请求限流分开计数，只在真正要打 provider 时消耗。
        # 未配置（None）时完全跳过；Redis 异常按放行处理，不让限流组件故障拖垮推理。
        self._rate_limiter = rate_limiter
        self._user_rate_limiter = user_rate_limiter
        self._token_quota = token_quota
        # 路由权重: cost / latency / quality
        self._weights = weights or {"cost": 0.3, "latency": 0.3, "quality": 0.4}
        # 租户路由配置（从 Go 网关同步）
        self._tenant_routes: dict[int, list[dict[str, Any]]] = {}  # tenant_id(int) -> [route, ...]
        self._gateway_url = gateway_url
        self._internal_token = internal_token
        # 运行期补注册 provider 的回调（由引擎在 lifespan 注入，见 register_provider）。
        # 没有它，"先起引擎、后配 key"就永久注册不上：provider 名单在 lifespan 里一次成型，
        # 打包桌面态还是**引擎先于网关**启动（local-stack.cjs），网关回填 keyset 时引擎已经在
        # 用一份空表了。
        self._provider_refresh = provider_refresh
        self._refresh_lock = asyncio.Lock()
        # 去抖：路由未命中会连续发生（每个请求一次），不去抖就等于每轮都扫一遍 Redis。
        self._refresh_min_interval = 2.0
        self._last_refresh_mono = 0.0

    # ── 公开 API ──

    def register_provider(
        self, name: str, provider: LLMProvider, preset: dict[str, Any] | None = None
    ) -> None:
        """运行期补注册一个 provider（keyset 迟到 / 管理端新增）。

        为什么这件事必须由 router 来做、而不是让 lifespan 直接往 `providers` 字典里塞：
        `_breakers` / `_embed_breakers` / `_latencies` 三张表都在构造时按**当时的** provider
        名单生成，而 `_select`、成功/失败上报用的是 `breakers[p.name]` 直接下标 ——
        只加字典不加表，第一次调用就 KeyError（表现是"配了 key 反而更糟"）。
        `_catalog` 是构造时从目录**列表**拷成的 dict，运行期往那个列表里 append 也传不进来，
        所以 preset 要显式带上，否则按 `provider/model` 前缀路由的自定义 provider 匹配不到。
        """
        self._providers[name] = provider
        # 先判存在再构造：`setdefault(name, CircuitBreaker(...))` 的第二参数是**先求值**的，
        # 而熔断器构造即上报 closed —— 重复注册会把一个已熔断 provider 的指标刷回 0。
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(provider=name, kind="chat")
        if name not in self._embed_breakers:
            self._embed_breakers[name] = CircuitBreaker(provider=name, kind="embed")
        self._latencies.setdefault(name, 500.0)
        if preset is not None:
            self._catalog.setdefault(name, preset)

    async def refresh_providers(self, *, force: bool = False, reason: str = "") -> int:
        """让 lifespan 的补注册逻辑跑一遍（去抖 + 单飞）。返回新增 provider 数。

        `force=True` 给广播用：管理端刚保存 key、网关刚回填完 keyset，这次必须真刷，
        不能因为"两秒前刚扫过"被去抖掉 —— 用户点完保存就立刻发消息，间隔常常不到 2s。
        """
        if self._provider_refresh is None:
            return 0
        async with self._refresh_lock:
            now = time.monotonic()
            if not force and now - self._last_refresh_mono < self._refresh_min_interval:
                return 0
            # 先占时间戳再 await：并发/递归都不会在这一次刷新里再触发刷新（路由未命中会重进 _select）。
            self._last_refresh_mono = now
            try:
                added = await self._provider_refresh()
            except Exception as exc:  # noqa: BLE001 - 补注册失败不该把这一轮请求变成异常
                logger.warning("provider refresh failed (%s): %s", reason or "lazy", exc)
                return 0
            if added:
                logger.info(
                    "LLM providers registered at runtime: +%d (total=%d, reason=%s)",
                    added,
                    len(self._providers),
                    reason or "lazy",
                )
            return added

    @staticmethod
    def _runtime_user_id() -> str:
        try:
            from app.tools.context import get_user_id

            return get_user_id()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _runtime_cache_identity(
        tenant_id: int, private: bool | None
    ) -> tuple[int, str, bool]:
        """Resolve the cache owner and fail closed when session visibility is unknown."""
        try:
            from app.tools.context import (
                get_tenant_id_int,
                get_tool_context,
                to_tenant_int,
            )

            cache_tenant = to_tenant_int(tenant_id) or get_tenant_id_int()
            raw_user = get_tool_context("user_id", "")
            user_id = (
                str(raw_user).strip()
                if raw_user is not None and not isinstance(raw_user, bool)
                else ""
            )
            visibility = get_tool_context("session_visibility")
            if visibility is None:
                visibility = get_tool_context("cache_scope")
            if isinstance(visibility, Mapping):
                visibility = visibility.get("visibility", "")
            normalized = str(visibility or "").strip().lower()
        except Exception:  # noqa: BLE001
            return 0, "", True

        if normalized:
            # An explicit caller flag may narrow a shared scope, never widen a private one.
            cache_private = normalized not in {"public", "shared", "tenant"}
            if private is True:
                cache_private = True
        else:
            # Internal callers can explicitly declare a shared scope. Missing scope stays private.
            cache_private = private is not False
        return cache_tenant, user_id, cache_private

    @staticmethod
    def _cache_runtime_policy() -> dict[str, Any]:
        """Snapshot serializable runtime controls that can change a no-tool response."""
        try:
            from app.tools.context import get_all

            context = get_all()
        except Exception:  # noqa: BLE001
            return {}
        policy: dict[str, Any] = {}
        for key in (
            "project",
            "project_locator",
            "plugin_names",
            "activated_tools",
            "tools_mode",
            "approval_mode",
            "cache_policy",
            "unattended",
            "takeover",
            "subagent_depth",
        ):
            if key not in context:
                continue
            value = context[key]
            if isinstance(value, (set, frozenset)):
                value = sorted(str(item) for item in value)
            elif isinstance(value, tuple):
                value = list(value)
            policy[key] = value
        return policy

    @classmethod
    def _cache_policy_digest(
        cls,
        messages: list[ChatMessage],
        model: str,
        provider: LLMProvider,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        caller_policy_digest: str,
    ) -> str:
        """Hash every effective request control, including controls outside cache.py's key."""
        payload = {
            "schema": 1,
            "messages": [message.to_dict() for message in messages],
            "model": model,
            "provider": provider.name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
            "response_format": response_format,
            "runtime_policy": cls._cache_runtime_policy(),
            "caller_policy_digest": (
                caller_policy_digest.strip()
                if isinstance(caller_policy_digest, str)
                else ""
            ),
        }
        try:
            raw = json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug("Cache policy digest computation failed: %s", exc)
            return ""
        return hashlib.sha256(raw).hexdigest()

    def _model_lineage(self, provider: LLMProvider, model: str) -> dict[str, Any]:
        """Describe the selected route without copying credentials into the cache domain."""
        preset = self._catalog.get(provider.name) or {}
        base_kwargs = getattr(provider, "_base_kwargs", {})
        endpoint = ""
        if isinstance(base_kwargs, Mapping):
            endpoint = str(base_kwargs.get("base_url") or "")
        if not endpoint:
            endpoint = str(getattr(provider, "_base", "") or "")
        if not endpoint:
            try:
                from app.providers.catalog import provider_base_url

                endpoint = provider_base_url(
                    {**preset, "id": str(preset.get("id") or provider.name)}
                )
            except Exception:  # noqa: BLE001
                endpoint = str(preset.get("base_url") or "")
        if endpoint:
            try:
                parts = urlsplit(endpoint)
                # Drop URL userinfo, query credentials, and fragments; keep host/port/path identity.
                endpoint = urlunsplit(
                    (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
                )
            except ValueError:
                endpoint = ""

        implementation = f"{type(provider).__module__}.{type(provider).__qualname__}"
        lowered = implementation.lower()
        if "responses" in lowered:
            protocol = "responses"
        elif "anthropic" in lowered:
            protocol = "anthropic"
        else:
            try:
                from app.providers.catalog import provider_kind

                protocol = provider_kind(preset)
            except Exception:  # noqa: BLE001
                protocol = str(preset.get("kind") or "openai")
        revision = (
            preset.get("revision")
            or preset.get("model_revision")
            or preset.get("version")
            or ""
        )
        return {
            "provider": provider.name,
            "model": model,
            "protocol": protocol,
            "endpoint": endpoint,
            "catalog_revision": revision,
            "implementation": implementation,
        }

    def _cache_domain_kwargs(
        self,
        *,
        user_id: str,
        private: bool,
        messages: list[ChatMessage],
        model: str,
        provider: LLMProvider,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        extra_kwargs: dict[str, Any],
        caller_policy_digest: str,
    ) -> dict[str, Any]:
        effective_response_format = extra_kwargs.get("response_format")
        digest = self._cache_policy_digest(
            messages,
            model,
            provider,
            max_tokens,
            temperature,
            tools,
            effective_response_format,
            caller_policy_digest,
        )
        if not digest:
            return {}
        return {
            "user_id": user_id,
            "private": private,
            "policy_digest": digest,
            "model_lineage": self._model_lineage(provider, model),
        }

    async def _model_rate_reason(self, tenant_id: int) -> str:
        """返回超限层级（tenant/user）；空串表示放行。

        用户身份取运行时已设置的 contextvar（`app.tools.context`），不新增调用参数：
        模型调用点有二十多处，逐个透传 user_id 既易漏也容易在子 Agent 里传丢。
        队列任务/后台摘要没有用户上下文时只受租户级约束。
        限流组件异常一律放行（fail-open），不让配额组件故障变成推理故障。
        """
        limiter = self._rate_limiter
        if limiter is not None and tenant_id:
            try:
                if not await limiter.allow(tenant_id):
                    return "tenant"
            except Exception as exc:  # noqa: BLE001
                logger.warning("model rate limiter failed; allowing call: %s", exc)

        user_id = self._runtime_user_id()
        user_limiter = self._user_rate_limiter
        if user_limiter is not None and user_id:
            try:
                if not await user_limiter.allow(user_id):
                    return "user"
            except Exception as exc:  # noqa: BLE001
                logger.warning("user model rate limiter failed; allowing call: %s", exc)

        quota = self._token_quota
        if quota is not None:
            try:
                scope = await quota.exceeded(tenant_id, user_id)
                if scope:
                    return scope
            except Exception as exc:  # noqa: BLE001
                logger.warning("token quota check failed; allowing call: %s", exc)
        return ""

    async def _record_model_tokens(self, tenant_id: int, tokens: int) -> None:
        quota = self._token_quota
        if quota is None or tokens <= 0:
            return
        try:
            await quota.add(tenant_id, self._runtime_user_id(), tokens)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响已完成的调用
            logger.warning("token quota add failed: %s", exc)

    async def chat_stream(
        self,
        messages: list[Any],
        model: str,
        *,
        tenant_id: int = 0,
        provider_hint: str = "",
        max_tokens: int = 0,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        private: bool | None = None,
        policy_digest: str = "",
    ) -> AsyncIterator[ChatResponse]:
        """Stream a response, consulting only an explicitly shared cache domain."""
        messages = normalize_messages(messages)
        provider: LLMProvider | None = None
        extra_kwargs: dict[str, Any] | None = None
        effective_response_format: dict[str, Any] | None = None
        cache_tenant = 0
        cache_kwargs: dict[str, Any] = {}

        if self._cache and not tools:
            cache_tenant, cache_user, cache_private = self._runtime_cache_identity(
                tenant_id, private
            )
            if not cache_private and cache_tenant and cache_user and model:
                # The final route is part of the domain, so resolve it before reading cache.
                provider = await self._select(
                    model, provider_hint, tenant_id=tenant_id or cache_tenant
                )
                if not provider:
                    yield ChatResponse(
                        content="",
                        finish_reason="error",
                        provider="none",
                        model=model,
                        message=self._no_provider_message(model),
                    )
                    return
                extra_kwargs = self._provider_extra_kwargs(provider, response_format)
                effective_response_format = extra_kwargs.get("response_format")
                cache_kwargs = self._cache_domain_kwargs(
                    user_id=cache_user,
                    private=cache_private,
                    messages=messages,
                    model=model,
                    provider=provider,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    extra_kwargs=extra_kwargs,
                    caller_policy_digest=policy_digest,
                )
                if cache_kwargs:
                    cached = await self._cache.lookup(
                        cache_tenant,
                        model,
                        messages,
                        tools,
                        temperature,
                        effective_response_format,
                        **cache_kwargs,
                    )
                    if cached:
                        logger.info(
                            "Stream cache HIT for model=%s provider=%s",
                            model,
                            provider.name,
                        )
                        yield cached
                        return

        rate_scope = await self._model_rate_reason(tenant_id)
        if rate_scope:
            yield ChatResponse(
                content="",
                finish_reason="error",
                provider="none",
                model=model,
                error_code="rate_limited",
                message=f"model calls rate limited for this {rate_scope}",
            )
            return

        if provider is None:
            provider = await self._select(model, provider_hint, tenant_id=tenant_id)
        if not provider:
            yield ChatResponse(
                content="",
                finish_reason="error",
                provider="none",
                model=model,
                message=self._no_provider_message(model),
            )
            return

        # 预算预检查。`max_tokens <= 0` 表示"不限制输出"，此时不能拿它当粗估值 ——
        # `budget.check` 收到 0 会永远放行，等于把预检关掉。这里换成一个保守的代表值，
        # 保持预检的原有语义（它本来就是粗估，不是精确账）。
        estimated = max_tokens if max_tokens and max_tokens > 0 else _PREFLIGHT_TOKENS
        if self._budget and tenant_id:
            ok = await self._budget.check(tenant_id, estimated)
            if not ok:
                yield ChatResponse(
                    content="",
                    finish_reason="error",
                    provider=provider.name,
                    model=model,
                    message=f"token budget exceeded for tenant {tenant_id}",
                )
                return

        start = time.monotonic()
        span = start_model_span(
            model=model, provider=provider.name, tenant_id=tenant_id, stream=True
        )
        full_content = ""
        full_tool_calls = []
        finish_reason = ""
        total_input = 0
        total_output = 0
        # Structured output is dropped per provider; shared cache keys use that effective value.
        if extra_kwargs is None:
            extra_kwargs = self._provider_extra_kwargs(provider, response_format)
        try:
            async for chunk in provider.chat_stream(
                messages,
                model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                **extra_kwargs,
            ):
                chunk.provider = provider.name
                chunk.model = model
                # 累积用于缓存
                if chunk.content:
                    full_content += chunk.content
                if chunk.tool_calls:
                    full_tool_calls.extend(chunk.tool_calls)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.input_tokens:
                    total_input += chunk.input_tokens
                if chunk.output_tokens:
                    total_output += chunk.output_tokens
                yield chunk

            self._breakers[provider.name].record_success()

            # Store only into the exact shared domain used by lookup.
            if (
                self._cache
                and cache_kwargs
                and not tools
                and finish_reason != "error"
                and full_content
            ):
                resp = ChatResponse(
                    content=full_content,
                    finish_reason=finish_reason,
                    provider=provider.name,
                    model=model,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )
                await self._cache.store(
                    cache_tenant,
                    model,
                    messages,
                    tools,
                    temperature,
                    resp,
                    effective_response_format,
                    **cache_kwargs,
                )

            # 缓存正常路径仅存结果；预算扣减统一在 finally 处理（见下）

        except Exception as e:
            # 确定性 4xx（模型不在套餐 / 不存在 / 参数非法）**不是 provider 故障**：
            # 记进熔断器会让"一个模型名配错"演变成"整个 provider 不可用"，把诊断方向
            # 从模型带到端点。这类错误只上报（note_last_error）以便下一次诊断能说出原因。
            if is_deterministic_client_error(e):
                logger.error(
                    "Provider %s rejected model=%s deterministically (not counted as failure): %s",
                    provider.name,
                    model,
                    str(e)[:300],
                )
            else:
                self._breakers[provider.name].record_failure()
                logger.error("Provider %s chat_stream failed: %s", provider.name, e)
            note_last_error(provider.name, e)
            record_model_error(span, e)
            # 不要将原始异常文本当作 assistant 回复内容发送（会显示成
            # "Connection error." 之类的假回复）；发 error chunk，
            # 由上层 AgentRuntime 转成 error 事件（message 携带真实原因）。
            yield ChatResponse(
                content="",
                finish_reason="error",
                provider=provider.name,
                model=model,
                message=self._stream_error_message(model, provider.name, e),
            )
        finally:
            # S 修复：预算扣减放入 finally——正常/异常路径都会按实际用量扣除。
            # GeneratorExit(客户端断开)取消时 await 不安全，跳过(少量少计可接受)。
            _exc = sys.exc_info()[1]
            if (
                not isinstance(_exc, GeneratorExit)
                and self._budget
                and tenant_id
                and (total_input + total_output) > 0
            ):
                try:
                    await self._budget.deduct(tenant_id, total_input + total_output)
                except Exception:
                    logger.warning("budget deduct failed (non-blocking)")
            elapsed = (time.monotonic() - start) * 1000
            self._latencies[provider.name] = (
                self._latencies[provider.name] * 0.8 + elapsed * 0.2
            )
            record_model_usage(
                span,
                input_tokens=total_input,
                output_tokens=total_output,
                finish_reason=finish_reason,
                response_model=model,
            )
            end_model_span(span)
            if not isinstance(_exc, GeneratorExit):
                await self._record_model_tokens(tenant_id, total_input + total_output)

    async def chat(
        self,
        messages: list[Any],
        model: str,
        *,
        tenant_id: int = 0,
        provider_hint: str = "",
        max_tokens: int = 0,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        private: bool | None = None,
        policy_digest: str = "",
    ) -> ChatResponse:
        """Return a response, consulting only an explicitly shared cache domain."""
        messages = normalize_messages(messages)
        provider: LLMProvider | None = None
        extra_kwargs: dict[str, Any] | None = None
        effective_response_format: dict[str, Any] | None = None
        cache_tenant = 0
        cache_kwargs: dict[str, Any] = {}

        if self._cache and not tools:
            cache_tenant, cache_user, cache_private = self._runtime_cache_identity(
                tenant_id, private
            )
            if not cache_private and cache_tenant and cache_user and model:
                provider = await self._select(
                    model, provider_hint, tenant_id=tenant_id or cache_tenant
                )
                if not provider:
                    return ChatResponse(
                        content="",
                        finish_reason="error",
                        message=self._no_provider_message(model),
                        provider="none",
                        model=model,
                    )
                extra_kwargs = self._provider_extra_kwargs(provider, response_format)
                effective_response_format = extra_kwargs.get("response_format")
                cache_kwargs = self._cache_domain_kwargs(
                    user_id=cache_user,
                    private=cache_private,
                    messages=messages,
                    model=model,
                    provider=provider,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    extra_kwargs=extra_kwargs,
                    caller_policy_digest=policy_digest,
                )
                if cache_kwargs:
                    cached = await self._cache.lookup(
                        cache_tenant,
                        model,
                        messages,
                        tools,
                        temperature,
                        effective_response_format,
                        **cache_kwargs,
                    )
                    if cached:
                        logger.info(
                            "Cache hit for model=%s provider=%s", model, provider.name
                        )
                        return cached

        rate_scope = await self._model_rate_reason(tenant_id)
        if rate_scope:
            return ChatResponse(
                content="",
                finish_reason="error",
                provider="none",
                model=model,
                error_code="rate_limited",
                message=f"model calls rate limited for this {rate_scope}",
            )

        if provider is None:
            provider = await self._select(model, provider_hint, tenant_id=tenant_id)
        if not provider:
            return ChatResponse(
                content="",
                finish_reason="error",
                message=self._no_provider_message(model),
                provider="none",
                model=model,
            )

        # 预算预检查（非流式同口径：`max_tokens <= 0` = 不限制输出，
        # 不能直接拿去预检 —— 0 会让 `check` 永远放行）。
        if self._budget and tenant_id:
            ok = await self._budget.check(
                tenant_id, max_tokens if max_tokens and max_tokens > 0 else _PREFLIGHT_TOKENS
            )
            if not ok:
                return ChatResponse(
                    content="",
                    finish_reason="error",
                    message="tenant budget exhausted",
                    provider=provider.name,
                    model=model,
                )

        start = time.monotonic()
        span = start_model_span(
            model=model, provider=provider.name, tenant_id=tenant_id, stream=False
        )
        # Structured output is dropped per provider; shared cache keys use that effective value.
        if extra_kwargs is None:
            extra_kwargs = self._provider_extra_kwargs(provider, response_format)
        try:
            resp = await provider.chat(
                messages,
                model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                **extra_kwargs,
            )
            resp.provider = provider.name
            resp.model = model
            record_model_usage(
                span,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cached_tokens=resp.cached_tokens,
                finish_reason=resp.finish_reason,
                response_model=model,
            )
            await self._record_model_tokens(
                tenant_id, resp.input_tokens + resp.output_tokens
            )

            self._breakers[provider.name].record_success()

            # Store only into the exact shared domain used by lookup.
            if self._cache and cache_kwargs and not tools and resp.finish_reason != "error":
                await self._cache.store(
                    cache_tenant,
                    model,
                    messages,
                    tools,
                    temperature,
                    resp,
                    effective_response_format,
                    **cache_kwargs,
                )

            # 预算扣减
            if (
                self._budget
                and tenant_id
                and resp.input_tokens + resp.output_tokens > 0
            ):
                await self._budget.deduct(
                    tenant_id, resp.input_tokens + resp.output_tokens
                )

            return resp
        except Exception as e:
            if is_deterministic_client_error(e):
                logger.error(
                    "Provider %s rejected model=%s deterministically (not counted as failure): %s",
                    provider.name,
                    model,
                    str(e)[:300],
                )
            else:
                self._breakers[provider.name].record_failure()
                logger.error("Provider %s chat failed: %s", provider.name, e)
            note_last_error(provider.name, e)
            record_model_error(span, e)
            return ChatResponse(
                content="",
                finish_reason="error",
                message=self._stream_error_message(model, provider.name, e),
                provider=provider.name,
                model=model,
            )
        finally:
            elapsed = (time.monotonic() - start) * 1000
            self._latencies[provider.name] = (
                self._latencies[provider.name] * 0.8 + elapsed * 0.2
            )
            end_model_span(span)

    async def delete_cache(
        self,
        messages: list[Any],
        model: str,
        *,
        tenant_id: int = 0,
        provider_hint: str = "",
        max_tokens: int = 0,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        private: bool | None = None,
        policy_digest: str = "",
    ) -> int:
        """Delete one shared request domain from L1/L2/L3; return removed tier count."""
        if not self._cache or tools or not model:
            return 0
        messages = normalize_messages(messages)
        cache_tenant, cache_user, cache_private = self._runtime_cache_identity(
            tenant_id, private
        )
        if cache_private or not cache_tenant or not cache_user:
            return 0
        provider = await self._select(
            model, provider_hint, tenant_id=tenant_id or cache_tenant
        )
        if not provider:
            return 0
        extra_kwargs = self._provider_extra_kwargs(provider, response_format)
        cache_kwargs = self._cache_domain_kwargs(
            user_id=cache_user,
            private=cache_private,
            messages=messages,
            model=model,
            provider=provider,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            extra_kwargs=extra_kwargs,
            caller_policy_digest=policy_digest,
        )
        if not cache_kwargs:
            return 0
        return await self._cache.delete(
            cache_tenant,
            model,
            messages,
            tools,
            temperature,
            extra_kwargs.get("response_format"),
            **cache_kwargs,
        )

    def _provider_extra_kwargs(
        self, provider: LLMProvider, response_format: dict[str, Any] | None
    ) -> dict[str, Any]:
        """把 ``response_format`` 转成“可以真的发给这个 provider”的额外参数。

        不支持的 provider 一律**置空**（返回 ``{}`` 而非 `None`）——这样调用点不需要
        分支，也不会把 provider 不认识的参数发过去（Anthropic SDK / Responses API
        收到 ``response_format`` 会直接报参数错误，而不是退化成普通文本）。

        这里用 info 而不是 warning：调用方（桌面端片段推理）本来就可能跨 provider
        路由，降级成纯文本是**设计内**行为，不是故障。
        """
        if not response_format:
            return {}
        if not getattr(provider, "supports_response_format", False):
            logger.info(
                "Provider %s does not support response_format; dropping it "
                "(the answer will be plain text)",
                provider.name,
            )
            return {}
        return {"response_format": response_format}

    async def embed(
        self, text: str, model: str, provider_hint: str = ""
    ) -> EmbeddingResponse:
        """文本嵌入 — 无缓存无预算。

        用**独立**的熔断表（embed=True）：某些网关（如 opencode-go）根本不提供
        /embeddings，那里失败不该把聊天也一起熔断。
        """
        provider = await self._select(model, provider_hint, embed=True)
        if not provider:
            return EmbeddingResponse()

        try:
            resp = await provider.embed(text, model)
            self._embed_breakers[provider.name].record_success()
            return resp
        except Exception as e:
            self._embed_breakers[provider.name].record_failure()
            logger.error("Provider %s embed failed: %s", provider.name, e)
            return EmbeddingResponse()

    def _probe_model(self, name: str) -> str:
        """探活用的廉价模型：目录 probe_model 优先，其次内建兜底表；空 = 不探活。"""
        preset = self._catalog.get(name) or {}
        model = str(preset.get("probe_model") or "").strip()
        if model:
            return model
        return self.PROBE_MODELS.get(name, "")

    async def health_check(self) -> dict[str, Any]:
        """返回各 Provider 健康状态。

        `circuit` 取的是**探活之后**的值。此前先取快照再探活：探活失败把熔断器打到
        OPEN，返回的却仍是 "closed" —— 管理端据此判定"健康"，而实际上下一次真实调用
        就会被拒。读数必须反映这次探活的结果，而不是探活前的样子。
        """
        result = {}
        for name, provider in self._providers.items():
            breaker = self._breakers[name]
            probe_model = self._probe_model(name)
            if not probe_model:
                # 目录未给探活模型（自定义端点等）：不探活，避免用错模型名
                # 触发 404 → 误开熔断；状态交由真实调用反馈。
                result[name] = {
                    "status": "ok",
                    "circuit": breaker.state.value,
                    "probe": "skipped",
                }
                continue
            try:
                # 尝试廉价模型调用 probe
                resp = await provider.chat(
                    [ChatMessage(role="user", content="ping")],
                    model=probe_model,
                    max_tokens=1,
                    temperature=0,
                )
                if resp.finish_reason != "error":
                    breaker.record_success()
                    result[name] = {"status": "ok", "circuit": breaker.state.value}
                else:
                    result[name] = {
                        "status": "error",
                        "circuit": breaker.state.value,
                        "message": "probe failed",
                    }
            except Exception as e:
                breaker.record_failure()
                result[name] = {
                    "status": "error",
                    "circuit": breaker.state.value,
                    "message": str(e),
                }
        return result

    async def reset_breaker(self, provider_name: str) -> bool:
        """重置指定 provider 的熔断器为 closed 状态。

        返回 True 表示成功，False 表示 provider 不存在。
        用于管理端手动恢复熔断的 provider。

        **两张表一起清**：chat 与 embed 是各自独立的熔断表（见 __init__）。只清 chat
        会留下一个"管理端点了恢复、聊天好了、但 embedding 仍被判熔断"的错位状态 ——
        而运维看到的是同一个 provider 名字，无从判断为什么一半可用。

        用 ``reset()`` 而不是新建实例：新建会丢掉 ``provider``/``kind`` 身份，之后的
        状态再也不会写进 ``llm_circuit_breaker_state``（指标停在上一次的 open 值上）。
        """
        if provider_name not in self._breakers:
            return False
        self._breakers[provider_name].reset()
        if provider_name in self._embed_breakers:
            self._embed_breakers[provider_name].reset()
        logger.info("Circuit breaker reset for provider=%s", provider_name)
        return True

    async def close(self) -> None:
        for p in self._providers.values():
            await p.close()

    def stats(self) -> dict[str, Any]:
        """返回路由器统计。

        `embed_circuit_state` 与 `circuit_state` 并列给出：chat 与 embed 是两张独立
        熔断表（见 `__init__`），而 `reset_breaker()` 会把两张一起清。只报 chat 的话，
        "embedding 被熔断了但聊天正常"这种真实形态在读数上完全看不出来 —— 而这正是
        当初把两张表拆开要解决的问题（opencode-go 缺 /embeddings 把整站聊天带崩）。

        新增字段而非改结构：Go 侧（`internal/api/admin_metrics.go` 的 `pyEngineInfo`）
        按字段名解析 JSON，多出来的字段会被忽略，现有读数不会因为这次加字段而失败。
        """
        return {
            "providers": {
                name: {
                    "circuit_state": self._breakers[name].state.value,
                    "embed_circuit_state": (
                        self._embed_breakers[name].state.value
                        if name in self._embed_breakers
                        else ""
                    ),
                    "avg_latency_ms": round(self._latencies[name], 1),
                }
                for name in self._providers
            },
            "cache": self._cache.stats() if self._cache else None,
            "tenant_routes": len(self._tenant_routes),
        }

    # ── 租户路由同步 ──

    async def sync_routes(self) -> int:
        """从 Go 网关同步租户模型路由配置，启动时调用。

        返回同步的路由条目数，失败返回 0（不阻断启动）。

        此方法也暴露给 POST /v1/gateway/sync-routes 端点，支持运行时热切换。
        """
        if not self._gateway_url or not self._internal_token:
            logger.info("sync_routes skipped: gateway_url or internal_token not set")
            return 0
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._gateway_url}/v1/internal/model-routes",
                    headers={"X-Internal-Token": self._internal_token},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("sync_routes HTTP %d from gateway", resp.status)
                        return 0
                    data = await resp.json()
                    routes = data.get("routes", [])
        except Exception as e:
            logger.warning("sync_routes failed: %s", e)
            return 0

        # 按 tenant_id 分组（键统一为 int：与 DB smallint 列 / 上游契约一致）
        from app.tools.context import to_tenant_int  # 函数内导入，避免循环导入

        tenant_map: dict[int, list[dict[str, Any]]] = {}
        for r in routes:
            tid = to_tenant_int(r.get("tenant_id", ""))
            if not tid:
                continue
            if tid not in tenant_map:
                tenant_map[tid] = []
            tenant_map[tid].append(r)
        self._tenant_routes = tenant_map
        logger.info("sync_routes loaded %d routes for %d tenants", len(routes), len(tenant_map))
        return len(routes)

    def _get_tenant_provider(self, model: str, tenant_id: int) -> str | None:
        """按租户路由配置获取首选 provider 名称。

        返回 None 表示无匹配路由（回退默认加权选择）。
        """
        if not tenant_id or tenant_id not in self._tenant_routes:
            return None
        for route in self._tenant_routes.get(tenant_id, []):
            if route.get("model_id") == model and route.get("enabled", True):
                primary = route.get("primary_provider")
                return str(primary) if primary else None
        return None

    # ── 内部路由 ──

    def _no_provider_message(self, model: str) -> str:
        """路由彻底失败时给用户的一句话。

        旧文案是 `check API keys in .env` —— 在打包桌面态里根本没有用户可见的 `.env`
        （密钥在管理后台，落 PG、镜像进 Redis），于是用户按提示去查环境变量，越查越偏。
        这里按**两种真不同的故障**分开说：
        表是空的 ⇒ 没有任何 provider 被注册（没配 key / 配的 key 还没同步进来）；
        表非空却选不出来 ⇒ provider 都在，是端点/熔断/**模型-供应商不兼容**的问题。

        最后一种是最容易被误诊的（实测：子 Agent 全部 failed）：模型是 Anthropic 直连的
        `claude-sonnet-4-…`，而唯一配了 key 的是订阅网关 OpenCode Go（套餐不含它）。
        所以当**所有已注册 provider 都被兼容判定拒绝**时，必须把"模型需要哪个 provider"
        直接写出来，而不是让用户去猜"是不是端点配错了"。
        """
        configured = sorted(self._providers.keys())
        if not configured:
            return (
                f"no LLM provider available for model={model}: 引擎当前没有注册任何提供商。"
                "请在「管理后台 → API 密钥」为某个服务提供商添加密钥并保存（保存后自动生效，"
                "无需重启）；用环境变量部署的则设置对应的 *_API_KEY 后重启服务。"
            )
        compatible = [n for n in configured if self._provider_accepts(n, model)]
        if not compatible:
            family = model_family(model)
            needed = DIRECT_PROVIDER_BY_FAMILY.get(family, "")
            parts = [
                f"no LLM provider available for model={model}: 已注册 provider="
                f"{','.join(configured)}，但**没有一个支持这个模型**"
            ]
            detail = []
            for name in configured:
                allow = plan_allowlist(name, self._catalog.get(name))
                if allow:
                    detail.append(f"{name}（套餐仅含 {', '.join(allow[:6])}…）")
            if detail:
                parts.append("：" + "、".join(detail))
            if needed:
                parts.append(
                    f"。该模型需要 provider={needed}（直连），当前未配置 —— "
                    f"请在「管理后台 → API 密钥」为 {needed} 添加密钥，"
                    "或在设置里改选套餐内的模型"
                )
            else:
                parts.append("。请为该模型配置对应的 provider，或改选已配置 provider 支持的模型")
            return "".join(parts)
        return (
            f"no LLM provider available for model={model}: 已注册 "
            f"provider={','.join(configured)}，但没有一个可用 —— 请检查该 provider 的端点（BASE_URL）"
            "与密钥是否有效，或稍后重试（熔断中）。"
        )

    def _stream_error_message(
        self, model: str, provider: str, exc: BaseException
    ) -> str:
        """把上游异常转成**可行动**的错误文案（模型不可用时点名该配什么）。"""
        if is_model_unavailable(exc):
            family = model_family(model)
            needed = DIRECT_PROVIDER_BY_FAMILY.get(family, "")
            allowed = plan_allowlist(provider, self._catalog.get(provider)) or []
            parts = [
                f"provider {provider} 不支持 model={model}（该模型不在其套餐/清单内）"
            ]
            if allowed:
                parts.append(f"套餐内可用模型族：{', '.join(allowed[:8])}")
            if needed:
                parts.append(
                    f"该模型需要 provider={needed}（直连），请配置它的密钥或改选套餐内模型"
                )
            parts.append(f"upstream: {str(exc)[:300]}")
            return "；".join(parts)
        return f"provider {provider} stream failed: {type(exc).__name__}: {str(exc)[:600]}"

    def _provider_accepts(self, name: str, model: str) -> bool:
        """该 provider 是否能接收这个模型（套餐白名单 + 显式禁用，见 model_compat）。"""
        return provider_supports_model(name, self._catalog.get(name), model)

    def _compatible_providers(self, model: str) -> list[LLMProvider]:
        """所有**兼容**该模型的已注册 provider（不含熔断判定）。"""
        return [
            provider
            for name, provider in self._providers.items()
            if self._provider_accepts(name, model)
        ]

    async def _select(
        self, model: str, hint: str = "", tenant_id: int = 0, embed: bool = False
    ) -> LLMProvider | None:
        """选择 Provider — 支持租户路由覆盖。

        embed=True 时使用**独立**的熔断表：embed 与 chat 的可用性互不牵连
        （见 __init__ 里 _embed_breakers 的注释）。

        **先判兼容、再判熔断**：租户路由 / provider_hint 都是"用户偏好"，不是"上游能力"。
        偏好指向一个不支持该模型的 provider 时（实测：会话 provider=opencode-go 而模型
        是 Anthropic 直连模型），必须让位给兼容的 provider，而不是把请求发过去换一个 400。
        """
        breakers = self._embed_breakers if embed else self._breakers
        # 租户路由优先（但必须兼容该模型）
        if tenant_id:
            tenant_provider = self._get_tenant_provider(model, tenant_id)
            if tenant_provider and tenant_provider in self._providers:
                if not self._provider_accepts(tenant_provider, model):
                    logger.warning(
                        "Tenant route provider %s does not support model=%s; "
                        "falling back to a compatible provider",
                        tenant_provider,
                        model,
                    )
                elif breakers[tenant_provider].allow():
                    return self._providers[tenant_provider]
                else:
                    logger.warning(
                        "Tenant route provider %s circuit-open for tenant=%s model=%s, falling back",
                        tenant_provider,
                        tenant_id,
                        model,
                    )
        if hint and hint in self._providers:
            if not self._provider_accepts(hint, model):
                # 显式 hint 也不该压过"模型能否被这个 provider 接收"这条硬约束。
                logger.warning(
                    "Hinted provider %s does not support model=%s; choosing a "
                    "compatible provider instead",
                    hint,
                    model,
                )
            elif breakers[hint].allow():
                return self._providers[hint]
            else:
                logger.warning("Hinted provider %s is circuit-open, falling back", hint)

        # 找到所有支持该 model 的 provider（目录前缀 + 兼容判定）
        candidates = self._find_candidates(model)
        if not candidates:
            # 一个都匹配不到 ⇒ 很可能是"key 是引擎启动之后才配的"（打包桌面态必然如此：
            # 桌面壳先起引擎、再起网关回填 keyset）。补注册一次再选，别直接判死。
            if await self.refresh_providers(reason="route-miss"):
                return await self._select(model, hint, tenant_id=tenant_id, embed=embed)
            # 只取**兼容**的那些：退回"全部 provider"正是"claude 被发给 opencode-go"
            # 那条错误路由的来源（见 app/gateway/model_compat.py 模块文档）。
            candidates = self._compatible_providers(model)
            if not candidates:
                logger.error(
                    "No registered provider accepts model=%s (providers=%s)",
                    model,
                    ",".join(sorted(self._providers.keys())),
                )
                return None

        # 过滤熔断
        available = [p for p in candidates if breakers[p.name].allow()]

        if not available:
            # 全熔断也可能是"新配的 provider 还没进表"（老 provider 正好都坏了）：
            # 补注册后再看一次，仍然坏才报错。
            if await self.refresh_providers(reason="all-circuit-open"):
                return await self._select(model, hint, tenant_id=tenant_id, embed=embed)
            logger.error(
                "All providers circuit-open for %s model=%s",
                "embed" if embed else "chat",
                model,
            )
            return None

        # 加权选择
        return self._weighted_select(available)

    def _find_candidates(self, model: str) -> list[LLMProvider]:
        """按 model 匹配候选 provider。

        优先用服务提供商目录的 ``model_prefixes``（新增 provider 只需补目录）；
        目录未命中或无目录时保留内建前缀规则。**所有**分支都要过兼容判定
        （`_provider_accepts`）—— 否则订阅网关会抢走它套餐里没有的模型。
        """
        model_lower = model.lower()
        matched: list[LLMProvider] = []
        for name, preset in self._catalog.items():
            if name not in self._providers:
                continue
            prefixes = preset.get("model_prefixes") or []
            if any(
                str(prefix).lower() in model_lower
                for prefix in prefixes
                if str(prefix).strip()
            ) and self._provider_accepts(name, model):
                matched.append(self._providers[name])
        if matched:
            return matched

        def _pick(name: str) -> list[LLMProvider]:
            if name in self._providers and self._provider_accepts(name, model):
                return [self._providers[name]]
            return []

        if "claude" in model_lower:
            picked = _pick("anthropic")
            if picked:
                return picked
        if "deepseek" in model_lower:
            picked = _pick("deepseek")
            if picked:
                return picked
        # gpt / o1 / o3 / embedding → openai
        if (
            "gpt" in model_lower
            or "o1" in model_lower
            or "o3" in model_lower
            or "embedding" in model_lower
        ):
            picked = _pick("openai")
            if picked:
                return picked
        # 兜底: 优先用 openai（最通用，历史行为）；**不兼容时**退回全部兼容 provider。
        # 旧版最后一行是"无条件返回全部 provider" —— 那正是订阅网关抢走它套餐外模型的入口。
        if "openai" in self._providers:
            picked = _pick("openai")
            if picked:
                return picked
        return self._compatible_providers(model)

    def _provider_cost(self, name: str) -> float:
        """provider 参考成本（$/1M tokens）：目录优先，其次内建默认。"""
        preset = self._catalog.get(name) or {}
        cost = preset.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        return self.DEFAULT_COST.get(name, 5.0)

    def _provider_quality(self, name: str) -> float:
        """provider 参考质量分（0-1）：目录优先，其次内建默认。"""
        preset = self._catalog.get(name) or {}
        quality = preset.get("quality")
        if isinstance(quality, (int, float)):
            return float(quality)
        return self.DEFAULT_QUALITY.get(name, 0.5)

    def _weighted_select(self, providers: list[LLMProvider]) -> LLMProvider:
        """加权随机选择"""
        if len(providers) == 1:
            return providers[0]

        scores = []
        for p in providers:
            # 免费/自托管 provider 成本为 0，取下限避免除零放大权重
            cost = max(self._provider_cost(p.name), 0.01)
            latency = max(self._latencies.get(p.name, 500.0), 1.0)
            quality = self._provider_quality(p.name)

            score = (
                self._weights["cost"] * (1.0 / cost)
                + self._weights["latency"] * (1000.0 / latency)
                + self._weights["quality"] * quality
            )
            scores.append(score)

        # 加权随机
        total = sum(scores)
        if total <= 0:
            return random.choice(providers)

        r = random.uniform(0, total)
        cumulative = 0.0
        for p, s in zip(providers, scores, strict=False):
            cumulative += s
            if r <= cumulative:
                return p

        return providers[-1]
