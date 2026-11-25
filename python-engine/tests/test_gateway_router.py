"""Tests for GatewayRouter — 预算扣减必须在流式路径也生效。

背景 bug: chat_stream（所有 Agent 推理的主路径）此前只做预算预检查
(TokenBudget.check)，从不按实际用量 deduct，导致 per-tenant 月度 token
预算（budget:{tenant}:{month} 的 used 字段）永远不增长、超限请求永不
被拒绝。非流式 chat() 路径正确扣减，两条路径行为必须一致。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.gateway.cache import SemanticCache
from app.gateway.provider import (
    ChatResponse,
    EmbeddingResponse,
    LLMProvider,
)
from app.gateway.router import GatewayRouter


class FakeProvider(LLMProvider):
    """返回预设 chunks 的假 provider"""

    name = "fake"

    def __init__(self, chunks: list[ChatResponse]):
        self._chunks = chunks

    async def chat_stream(self, messages, model, **kwargs):
        for c in self._chunks:
            yield c

    async def chat(self, messages, model, **kwargs):
        return self._chunks[-1]

    async def embed(self, text, model):
        return EmbeddingResponse()

    async def close(self):
        pass


def _make_budget(*, allow: bool = True):
    """预算桩：H36 之后 router 走 `budget_reserve`（Lua 预占），不再直接 check/deduct。

    `_redis.eval` 用真 Lua 才能测的东西在 `tests/test_budget_reserve_h36.py`；
    这里只关心"router 有没有按预占/结算的口径调用"，所以给一个最小的 eval 桩：
    reserve 返回 `{'reserved', n}`，release/commit 返回 1。
    """
    budget = MagicMock()
    budget.check = AsyncMock(return_value=allow)
    budget.deduct = AsyncMock()
    budget._key = MagicMock(return_value="budget:t1:2026-11")

    async def _eval(script, numkeys, *args):
        if "HINCRBY', KEYS[1], 'reserved'" in script:
            return ["reserved" if allow else "denied", 100] if allow else ["denied", 100]
        return 1

    budget._redis = MagicMock()
    budget._redis.eval = AsyncMock(side_effect=_eval)
    return budget


class TestChatStreamBudget:
    @pytest.mark.asyncio
    async def test_chat_stream_deducts_actual_tokens(self):
        """流式推理结束后必须按实际 token 用量扣减预算（与 chat() 一致）。

        若不扣减，流式调用（Agent 主路径）永远不会消耗 used 额度，
        月度预算限制形同虚设——这是本测试要防住的回归。
        """
        provider = FakeProvider([
            ChatResponse(content="你好"),
            ChatResponse(finish_reason="stop", input_tokens=120, output_tokens=80),
        ])
        budget = _make_budget()
        router = GatewayRouter(providers={"fake": provider}, budget=budget)

        chunks = [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        assert chunks[-1].finish_reason == "stop"
        # 成功路径：预占 → 按**实际**用量结算（不再是"直接 deduct"）
        eval_calls = budget._redis.eval.await_args_list
        assert len(eval_calls) == 2, f"应有一次预占 + 一次结算，实际 {len(eval_calls)}"
        settle = eval_calls[-1]
        assert settle.args[4] == 200, "结算必须落实际 token 数"
        budget.deduct.assert_not_called()

    @pytest.mark.asyncio
    async def test_chat_stream_no_deduct_without_tokens(self):
        """无 token 用量时不应触发扣减（deduct 内部对 <=0 也会忽略）。"""
        provider = FakeProvider([
            ChatResponse(content="hi", finish_reason="stop"),
        ])
        budget = _make_budget()
        router = GatewayRouter(providers={"fake": provider}, budget=budget)

        [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        # 无用量：结算金额为 0，但仍要清掉占位（否则额度会被永久占用）
        eval_calls = budget._redis.eval.await_args_list
        assert eval_calls, "预占仍应发生"
        assert eval_calls[-1].args[4] == 0, "无用量时结算金额为 0"
        budget.deduct.assert_not_called()

    @pytest.mark.asyncio
    async def test_chat_stream_provider_error_does_not_deduct(self):
        """provider 抛异常时（error 路径）不扣减，与 chat() 的异常语义一致。"""
        class FailingProvider(FakeProvider):
            async def chat_stream(self, messages, model, **kwargs):
                if False:
                    yield  # 保持 async generator 形态
                raise RuntimeError("connection reset")

        budget = _make_budget()
        router = GatewayRouter(providers={"fake": FailingProvider([])}, budget=budget)

        chunks = [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

        assert chunks[-1].finish_reason == "error"
        budget.deduct.assert_not_called()
        # 失败路径必须**退回占位**：一次上游故障不该永久吃掉额度
        eval_calls = budget._redis.eval.await_args_list
        assert eval_calls, "预占应已发生"
        assert eval_calls[-1].args[4] == 0, "失败时以 0 结算（等价于退回占位）"


class _DenyLimiter:
    async def allow(self, tenant_id: int) -> bool:  # noqa: ARG002
        return False


class _BrokenLimiter:
    async def allow(self, tenant_id: int) -> bool:  # noqa: ARG002
        raise RuntimeError("redis down")


class TestModelRateLimit:
    """R7：模型调用限流只拦 provider 调用；缓存命中/限流故障都不该被误伤。"""

    @pytest.mark.asyncio
    async def test_chat_returns_rate_limited_without_provider(self):
        router = GatewayRouter({}, rate_limiter=_DenyLimiter())
        resp = await router.chat([{"role": "user", "content": "hi"}], "m", tenant_id=1)
        assert resp.finish_reason == "error"
        assert resp.error_code == "rate_limited"
        assert "rate limited" in resp.message

    @pytest.mark.asyncio
    async def test_chat_stream_yields_rate_limited(self):
        router = GatewayRouter({}, rate_limiter=_DenyLimiter())
        chunks = [
            c
            async for c in router.chat_stream(
                [{"role": "user", "content": "hi"}], "m", tenant_id=1
            )
        ]
        assert len(chunks) == 1
        assert chunks[0].error_code == "rate_limited"

    @pytest.mark.asyncio
    async def test_limiter_failure_fails_open(self):
        router = GatewayRouter({}, rate_limiter=_BrokenLimiter())
        resp = await router.chat([{"role": "user", "content": "hi"}], "m", tenant_id=1)
        # 放行后走正常的"没有可用 provider"分支，而不是被限流拦下。
        assert resp.finish_reason == "error"
        assert resp.error_code != "rate_limited"

    @pytest.mark.asyncio
    async def test_disabled_windows_do_not_touch_redis(self):
        from app.gateway.ratelimit import LocalTenantRateLimiter, TenantRateLimiter

        # 0/0 = 关闭：不连 Redis、不计数，必须恒放行。
        assert await TenantRateLimiter(None, 0, 0).allow(1) is True  # type: ignore[arg-type]
        local = LocalTenantRateLimiter(0, 0)
        for _ in range(100):
            assert await local.allow(1) is True


class _AllowLimiter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def allow(self, key: object) -> bool:
        self.calls.append(key)
        return True


class _RecordingDenyLimiter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def allow(self, key: object) -> bool:
        self.calls.append(key)
        return False


class TestUserModelRateLimit:
    @pytest.mark.asyncio
    async def test_user_limit_uses_runtime_user_context(self):
        from app.tools.context import set_tool_context

        tenant = _AllowLimiter()
        user = _RecordingDenyLimiter()
        router = GatewayRouter({}, rate_limiter=tenant, user_rate_limiter=user)
        set_tool_context(user_id="u-42")
        try:
            resp = await router.chat([{"role": "user", "content": "hi"}], "m", tenant_id=1)
        finally:
            set_tool_context(user_id="")

        assert resp.error_code == "rate_limited"
        assert "user" in resp.message
        assert user.calls == ["u-42"], "用户身份应来自运行时上下文，而不是调用参数"

    @pytest.mark.asyncio
    async def test_user_limit_skipped_without_context(self):
        from app.tools.context import set_tool_context

        user = _RecordingDenyLimiter()
        router = GatewayRouter({}, user_rate_limiter=user)
        set_tool_context(user_id="")
        resp = await router.chat([{"role": "user", "content": "hi"}], "m", tenant_id=1)

        assert resp.error_code != "rate_limited"
        assert user.calls == [], "没有用户上下文时不该用空 key 计数"


class _QuotaDeny:
    async def exceeded(self, tenant_id: int, user_id: str) -> str:
        return "user" if user_id else "tenant"

    async def add(self, tenant_id: int, user_id: str, tokens: int) -> None:
        return None


class TestTokenQuotaGate:
    @pytest.mark.asyncio
    async def test_tpm_blocks_before_provider(self):
        from app.tools.context import set_tool_context

        router = GatewayRouter({}, token_quota=_QuotaDeny())
        set_tool_context(user_id="u-1")
        try:
            resp = await router.chat([{"role": "user", "content": "hi"}], "m", tenant_id=1)
        finally:
            set_tool_context(user_id="")
        assert resp.error_code == "rate_limited"
        assert "user" in resp.message


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.now = 1000.0

    async def get(self, key: str) -> str | None:
        expires_at = self.ttls.get(key)
        if expires_at is not None and self.now >= expires_at:
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            return None
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = self.now + ex
        return True

    async def delete(self, key: str) -> int:
        existed = self.store.pop(key, None) is not None
        self.ttls.pop(key, None)
        return int(existed)


class _CacheProvider(FakeProvider):
    def __init__(self, name: str = "fake") -> None:
        super().__init__([])
        self.name = name
        self.calls = 0

    async def chat_stream(self, messages, model, **kwargs):
        self.calls += 1
        yield ChatResponse(content=f"{self.name}-answer-{self.calls}", finish_reason="stop")

    async def chat(self, messages, model, **kwargs):
        self.calls += 1
        return ChatResponse(content=f"{self.name}-answer-{self.calls}", finish_reason="stop")


def _cache(redis: _FakeRedis, **kwargs: Any) -> SemanticCache:
    async def embed(text: str) -> list[float]:
        return [0.5] * 8

    return SemanticCache(redis=redis, embed_fn=embed, **kwargs)


@contextmanager
def _runtime_scope(*, user_id: str = "user-a", tenant_id: int = 7, **extra: Any):
    from app.tools.context import get_all, restore_context, set_tool_context

    snapshot = get_all()
    set_tool_context(user_id=user_id, tenant_id=tenant_id, **extra)
    try:
        yield
    finally:
        restore_context(snapshot)


async def _invoke(router: GatewayRouter, *, stream: bool, **kwargs: Any) -> ChatResponse:
    messages = [{"role": "user", "content": "Summarize this shared production request."}]
    if not stream:
        return await router.chat(messages, "fake-model", **kwargs)
    chunks = [chunk async for chunk in router.chat_stream(messages, "fake-model", **kwargs)]
    assert len(chunks) == 1
    return chunks[0]


class TestProductionCacheDomain:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_private_default_and_private_context_never_cache(self, stream: bool) -> None:
        redis = _FakeRedis()
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(redis))

        with _runtime_scope():
            first = await _invoke(router, stream=stream)
            second = await _invoke(router, stream=stream)
        with _runtime_scope(session_visibility="private"):
            third = await _invoke(router, stream=stream, private=False)
            fourth = await _invoke(router, stream=stream, private=False)
        with _runtime_scope(session_visibility="shared"):
            fifth = await _invoke(router, stream=stream, private=True)
            sixth = await _invoke(router, stream=stream, private=True)

        answers = {first.content, second.content, third.content, fourth.content, fifth.content, sixth.content}
        assert len(answers) == 6
        assert provider.calls == 6
        assert redis.store == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_shared_scope_hits_with_runtime_identity(self, stream: bool) -> None:
        redis = _FakeRedis()
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(redis))

        with _runtime_scope():
            first = await _invoke(router, stream=stream, private=False)
            second = await _invoke(router, stream=stream, private=False)

        assert first.content == second.content == "fake-answer-1"
        assert provider.calls == 1
        assert len(redis.store) == 2
        assert set(redis.ttls.values()) == {redis.now + 3600}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_shared_visibility_context_enables_cache(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(_FakeRedis()))

        with _runtime_scope(session_visibility="shared"):
            first = await _invoke(router, stream=stream)
            second = await _invoke(router, stream=stream)

        assert first.content == second.content == "fake-answer-1"
        assert provider.calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize(
        ("user_id", "tenant_id"),
        [("", 7), ("user-a", 0)],
    )
    async def test_incomplete_runtime_identity_fails_closed(
        self, stream: bool, user_id: str, tenant_id: int
    ) -> None:
        redis = _FakeRedis()
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(redis))

        with _runtime_scope(user_id=user_id, tenant_id=tenant_id):
            first = await _invoke(router, stream=stream, private=False)
            second = await _invoke(router, stream=stream, private=False)

        assert first.content == "fake-answer-1"
        assert second.content == "fake-answer-2"
        assert provider.calls == 2
        assert redis.store == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_cross_user_requests_do_not_hit(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(_FakeRedis()))

        with _runtime_scope(user_id="user-a"):
            answer_a = await _invoke(router, stream=stream, private=False)
        with _runtime_scope(user_id="user-b"):
            answer_b = await _invoke(router, stream=stream, private=False)
        with _runtime_scope(user_id="user-a"):
            answer_a_again = await _invoke(router, stream=stream, private=False)

        assert answer_a.content == answer_a_again.content == "fake-answer-1"
        assert answer_b.content == "fake-answer-2"
        assert provider.calls == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_final_provider_lineage_change_does_not_hit(self, stream: bool) -> None:
        provider_a = _CacheProvider("fake-a")
        provider_b = _CacheProvider("fake-b")
        router = GatewayRouter(
            {"fake-a": provider_a, "fake-b": provider_b}, cache=_cache(_FakeRedis())
        )

        with _runtime_scope():
            answer_a = await _invoke(
                router, stream=stream, private=False, provider_hint="fake-a"
            )
            answer_b = await _invoke(
                router, stream=stream, private=False, provider_hint="fake-b"
            )
            answer_b_again = await _invoke(
                router, stream=stream, private=False, provider_hint="fake-b"
            )

        assert answer_a.content == "fake-a-answer-1"
        assert answer_b.content == answer_b_again.content == "fake-b-answer-1"
        assert provider_a.calls == 1
        assert provider_b.calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_catalog_revision_change_does_not_hit(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter(
            {"fake": provider},
            cache=_cache(_FakeRedis()),
            provider_catalog=[{"id": "fake", "revision": "catalog-1"}],
        )

        with _runtime_scope():
            first = await _invoke(router, stream=stream, private=False)
            router._catalog["fake"]["revision"] = "catalog-2"
            second = await _invoke(router, stream=stream, private=False)
            third = await _invoke(router, stream=stream, private=False)

        assert first.content == "fake-answer-1"
        assert second.content == third.content == "fake-answer-2"
        assert provider.calls == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_output_policy_change_does_not_hit(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(_FakeRedis()))

        with _runtime_scope():
            first = await _invoke(router, stream=stream, private=False, max_tokens=32)
            second = await _invoke(router, stream=stream, private=False, max_tokens=64)
            third = await _invoke(router, stream=stream, private=False, max_tokens=64)

        assert first.content == "fake-answer-1"
        assert second.content == third.content == "fake-answer-2"
        assert provider.calls == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_dropped_response_format_uses_effective_policy(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(_FakeRedis()))

        with _runtime_scope():
            first = await _invoke(
                router,
                stream=stream,
                private=False,
                response_format={"type": "json_object"},
            )
            second = await _invoke(router, stream=stream, private=False)

        assert first.content == second.content == "fake-answer-1"
        assert provider.calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_caller_policy_digest_change_does_not_hit(self, stream: bool) -> None:
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(_FakeRedis()))

        with _runtime_scope():
            first = await _invoke(
                router, stream=stream, private=False, policy_digest="policy-1"
            )
            second = await _invoke(
                router, stream=stream, private=False, policy_digest="policy-2"
            )
            third = await _invoke(
                router, stream=stream, private=False, policy_digest="policy-2"
            )

        assert first.content == "fake-answer-1"
        assert second.content == third.content == "fake-answer-2"
        assert provider.calls == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_router_cache_expires_after_configured_ttl(self, stream: bool) -> None:
        redis = _FakeRedis()
        provider = _CacheProvider()
        router = GatewayRouter(
            {"fake": provider}, cache=_cache(redis, l2_ttl=5, l3_ttl=5)
        )

        with _runtime_scope():
            first = await _invoke(router, stream=stream, private=False)
            assert set(redis.ttls.values()) == {redis.now + 5}

            redis.now += 6
            fresh_router = GatewayRouter(
                {"fake": provider}, cache=_cache(redis, l2_ttl=5, l3_ttl=5)
            )
            second = await _invoke(fresh_router, stream=stream, private=False)

        assert first.content == "fake-answer-1"
        assert second.content == "fake-answer-2"
        assert provider.calls == 2

    @pytest.mark.asyncio
    async def test_router_delete_uses_the_same_domain(self) -> None:
        redis = _FakeRedis()
        provider = _CacheProvider()
        router = GatewayRouter({"fake": provider}, cache=_cache(redis))
        messages = [{"role": "user", "content": "Delete this shared cached answer."}]

        with _runtime_scope():
            first = await router.chat(messages, "fake-model", private=False)
            assert await router.delete_cache(messages, "fake-model", private=False) == 3
            assert redis.store == {}
            second = await router.chat(messages, "fake-model", private=False)

        assert first.content == "fake-answer-1"
        assert second.content == "fake-answer-2"
        assert provider.calls == 2


def test_cache_ttl_settings_are_configurable() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, cache_l2_ttl=120, cache_l3_ttl=30)

    assert settings.cache_l2_ttl == 120
    assert settings.cache_l3_ttl == 30
