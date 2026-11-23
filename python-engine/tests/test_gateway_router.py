"""Tests for GatewayRouter — 预算扣减必须在流式路径也生效。

背景 bug: chat_stream（所有 Agent 推理的主路径）此前只做预算预检查
(TokenBudget.check)，从不按实际用量 deduct，导致 per-tenant 月度 token
预算（budget:{tenant}:{month} 的 used 字段）永远不增长、超限请求永不
被拒绝。非流式 chat() 路径正确扣减，两条路径行为必须一致。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _make_budget():
    budget = MagicMock()
    budget.check = AsyncMock(return_value=True)
    budget.deduct = AsyncMock()
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
        budget.deduct.assert_awaited_once_with("t1", 200)

    @pytest.mark.asyncio
    async def test_chat_stream_no_deduct_without_tokens(self):
        """无 token 用量时不应触发扣减（deduct 内部对 <=0 也会忽略）。"""
        provider = FakeProvider([
            ChatResponse(content="hi", finish_reason="stop"),
        ])
        budget = _make_budget()
        router = GatewayRouter(providers={"fake": provider}, budget=budget)

        [c async for c in router.chat_stream([], "fake-model", tenant_id="t1")]

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
