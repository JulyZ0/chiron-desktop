"""熔断器接线回归：单测「熔断器本身」之外的那半边 —— 网关怎么用它。

`tests/test_circuit_breaker.py` 钉住了三态机本身；这里钉住 router 侧的三处接线，
它们各自都有一个"看起来正常、实际给错答案"的失败模式：

1. **重置只清一半** —— chat 与 embed 是两张独立熔断表。只清 chat 会留下
   "管理端点了恢复、聊天好了、embedding 仍被判熔断"，而运维看到的是同一个名字。
2. **重置把身份丢了** —— 旧实现直接换成新 `CircuitBreaker()`，新实例的
   provider/kind 为空，之后状态再也不会写进 `llm_circuit_breaker_state`。
3. **探活报的是探活前的状态** —— 旧实现先取 `state` 快照再探活，探活失败把熔断器
   打到 OPEN，返回的却仍是 "closed"，管理端据此认为"健康"。
"""

from __future__ import annotations

import pytest

from app.gateway.circuit_breaker import CircuitState
from app.gateway.provider import ChatResponse, EmbeddingResponse, LLMProvider
from app.gateway.router import GatewayRouter
from app.observability.metrics import LLM_CIRCUIT_STATE


class ProbeProvider(LLMProvider):
    """探活用的假 provider：按 `fail` 决定 ping 成功还是抛错。"""

    name = "fake"

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def chat(self, messages, model, **kwargs):
        if self.fail:
            raise RuntimeError("upstream down")
        return ChatResponse(content="pong", finish_reason="stop")

    async def chat_stream(self, messages, model, **kwargs):
        if False:
            yield  # 保持 async generator 形态
        raise AssertionError("health_check 不应走流式路径")

    async def embed(self, text, model):
        return EmbeddingResponse()

    async def close(self):
        pass


def _router(*, fail: bool = False) -> GatewayRouter:
    """构造一个带探活模型的 router。

    `PROBE_MODELS` 里没有 "fake"，所以显式经目录给 `probe_model` —— 否则
    `health_check` 会走 "skipped" 分支，测不到探活本身。
    """
    return GatewayRouter(
        providers={"fake": ProbeProvider(fail=fail)},
        provider_catalog=[{"id": "fake", "probe_model": "fake-probe"}],
    )


def _trip(breaker) -> None:
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()


@pytest.mark.asyncio
async def test_reset_breaker_clears_both_chat_and_embed() -> None:
    router = _router()
    _trip(router._breakers["fake"])
    _trip(router._embed_breakers["fake"])
    assert router._breakers["fake"].state is CircuitState.OPEN
    assert router._embed_breakers["fake"].state is CircuitState.OPEN

    assert await router.reset_breaker("fake") is True

    assert router._breakers["fake"].state is CircuitState.CLOSED
    assert router._embed_breakers["fake"].state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_reset_breaker_keeps_metric_identity() -> None:
    """重置后仍是**带身份**的同一个熔断器，否则指标会停在 open 值上。"""
    router = _router()
    _trip(router._breakers["fake"])

    await router.reset_breaker("fake")

    assert router._breakers["fake"].provider == "fake"
    assert router._breakers["fake"].kind == "chat"
    value = LLM_CIRCUIT_STATE.labels(provider="fake", kind="chat")._value.get()
    assert value == 0.0, "重置后指标应回到 closed(0)，而不是留着上一次的 open(2)"


@pytest.mark.asyncio
async def test_reset_breaker_unknown_provider() -> None:
    assert await _router().reset_breaker("nope") is False


@pytest.mark.asyncio
async def test_health_check_reports_state_after_probe() -> None:
    """探活失败把熔断器打到 OPEN 时，返回的 circuit 必须是 "open" 而不是探活前的值。"""
    router = _router(fail=True)
    breaker = router._breakers["fake"]
    # 只差一次失败就到阈值：这次探活正好触发 OPEN，是新旧实现唯一的区分点。
    for _ in range(breaker.failure_threshold - 1):
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED

    result = await router.health_check()

    assert result["fake"]["status"] == "error"
    assert breaker.state is CircuitState.OPEN
    assert result["fake"]["circuit"] == "open"


@pytest.mark.asyncio
async def test_health_check_probe_without_model_is_skipped() -> None:
    """目录没给探活模型时不该打上游（用错模型名会 404 并误开熔断）。"""
    router = GatewayRouter(providers={"fake": ProbeProvider(fail=True)})

    result = await router.health_check()

    assert result["fake"]["probe"] == "skipped"
    assert router._breakers["fake"].state is CircuitState.CLOSED


def test_breaker_publishes_state_metric() -> None:
    """`llm_circuit_breaker_state` 此前只有定义、零处写入。"""
    from app.gateway.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=1, provider="metric-probe", kind="embed")
    assert LLM_CIRCUIT_STATE.labels(provider="metric-probe", kind="embed")._value.get() == 0.0

    cb.record_failure()
    assert LLM_CIRCUIT_STATE.labels(provider="metric-probe", kind="embed")._value.get() == 2.0

    cb.reset()
    assert LLM_CIRCUIT_STATE.labels(provider="metric-probe", kind="embed")._value.get() == 0.0


def test_stats_exposes_embed_circuit_separately() -> None:
    """`stats()` 必须同时报 chat 与 embed 两张表的状态。

    两张表各自独立，而 `reset_breaker()` 会把它们一起清。只报 chat 的话，
    "embedding 熔断了、聊天正常"这个真实形态在读数上完全看不出来。
    """
    router = _router()
    _trip(router._embed_breakers["fake"])

    providers = router.stats()["providers"]

    assert providers["fake"]["circuit_state"] == "closed"
    assert providers["fake"]["embed_circuit_state"] == "open"


def test_register_provider_does_not_reset_existing_breaker() -> None:
    """重复注册同一个 provider 不能把已熔断的熔断器换掉（换掉 = 熔断被静默解除）。"""
    router = _router()
    _trip(router._breakers["fake"])

    router.register_provider("fake", ProbeProvider())

    assert router._breakers["fake"].state is CircuitState.OPEN
