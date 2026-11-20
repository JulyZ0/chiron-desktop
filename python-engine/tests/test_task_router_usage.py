"""auto 链路（TaskRouter）的 token 用量回传（P1-9）。

背景：`mode=auto` 是统一链路 `/v1/chat/submit` 的默认模式。意图理解要调一次
LLM，命中的 `agent:*` 能力还要各调一次 —— 但这两段的 usage 此前没有任何人接，
于是引擎回包里的 `usage` 恒为 `None`，网关只能记"有一次调用、0 token"，计费页
里这条链路的回合看起来像没花钱。

测试覆盖:
- 意图理解把逐帧 token 累加进 UsageAccumulator
- `_llm_executor` 在输出里带上 usage（模型/厂商/三个 token 维度）
- `_execute_single_task` 把 usage 摘下（不污染用户可见输出）并登记
- `route_task` 的返回值带 usage block；一次调用都没有时不给假 usage
- 解析失败（JSON 不合法）时用量仍要入账（调用已发生）
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.capabilities import (
    Capability,
    CapabilityParam,
    CapabilityResult,
    CapabilityType,
    WorkstationType,
    _llm_executor,
)
from app.core.task_router import (
    SubTask,
    TaskRouter,
    UsageAccumulator,
)


@pytest.fixture(autouse=True)
def _isolated_side_effects(monkeypatch):
    """span / ContextBus 落 Redis Stream，与"用量有没有回传"无关 —— 隔离掉。"""

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.trace.record_span", _noop)
    monkeypatch.setattr("app.core.context_bus.publish_state_change", _noop)


class _Chunk:
    """一帧流式响应（带与网关一致的可选字段）。"""

    def __init__(
        self,
        content: str = "",
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        model: str = "",
        provider: str = "",
    ) -> None:
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.model = model
        self.provider = provider


def _fake_gateway(chunks: list[_Chunk], seen: dict[str, Any] | None = None):
    class _FakeGateway:
        async def chat_stream(self, messages, model, **kwargs):  # noqa: ARG002
            if seen is not None:
                seen["model"] = model
            for chunk in chunks:
                yield chunk

    return _FakeGateway()


class _FakeRegistry:
    def __init__(self, cap: Capability) -> None:
        self._cap = cap

    async def get_by_id(
        self, capability_id: str, tenant_id: int = 0
    ) -> Capability | None:
        return self._cap if capability_id == self._cap.capability_id else None


# ── 累加器语义 ─────────────────────────────────────────────────────


class TestUsageAccumulator:
    def test_no_calls_yields_no_block(self):
        """没有调用就不给 block：编一个全 0 的 usage 等于冒充"已知无用量"。"""
        assert UsageAccumulator().to_block() is None

    def test_blocks_by_dominant_model(self):
        acc = UsageAccumulator()
        acc.add(model="gpt-4o-mini", provider="openai", input_tokens=100, output_tokens=10)
        acc.add(model="deepseek-chat", provider="deepseek", input_tokens=5000, output_tokens=400)

        block = acc.to_block()
        assert block is not None
        # 网关按单一模型查价：主模型取 token 占比最大的那个
        assert block["model"] == "deepseek-chat"
        assert block["provider"] == "deepseek"
        # token 是**全部模型之和**：只报主模型的量会让其余调用凭空消失（少记账）
        assert block["input_tokens"] == 5100
        assert block["output_tokens"] == 410
        assert block["calls"] == 2

    def test_missing_model_falls_back_then_unknown(self):
        acc = UsageAccumulator(fallback_model="qwen-plus")
        acc.add(input_tokens=10)
        assert acc.to_block()["model"] == "qwen-plus"

        other = UsageAccumulator()
        other.add(input_tokens=10)
        # 模型名是价格查询键，丢了会让 cost 恒 NULL —— 宁可记 unknown
        assert other.to_block()["model"] == "unknown"


# ── 意图理解的 token 累加 ──────────────────────────────────────────


class TestIntentUsage:
    async def test_intent_accumulates_stream_tokens(self, monkeypatch):
        import app.main as app_main

        chunks = [
            _Chunk('{"action": "general", ', input_tokens=800, model="deepseek-chat", provider="deepseek"),
            _Chunk('"keywords": ["你好"], "complexity": "simple"}', output_tokens=40),
        ]
        async def _get_gateway():
            return _fake_gateway(chunks)

        monkeypatch.setattr(app_main, "get_gateway", _get_gateway)

        acc = UsageAccumulator()
        await TaskRouter()._llm_understand_intent(
            "你好", {}, model="deepseek-chat", usage=acc
        )

        block = acc.to_block()
        assert block is not None
        assert block["input_tokens"] == 800
        assert block["output_tokens"] == 40
        assert block["model"] == "deepseek-chat"
        assert block["provider"] == "deepseek"

    async def test_usage_recorded_even_when_json_parse_fails(self, monkeypatch):
        """调用已发生（真的花了钱），解析失败不能把这次用量丢掉。"""
        import app.main as app_main

        async def _get_gateway():
            return _fake_gateway([_Chunk("not json", input_tokens=123, output_tokens=7, model="gpt-4o-mini")])

        monkeypatch.setattr(app_main, "get_gateway", _get_gateway)

        acc = UsageAccumulator()
        with pytest.raises(ValueError):
            await TaskRouter()._llm_understand_intent("你好", {}, usage=acc)

        block = acc.to_block()
        assert block is not None
        assert block["input_tokens"] == 123 and block["output_tokens"] == 7


# ── LLM 能力的 usage 透传 ──────────────────────────────────────────


class TestLLMCapabilityUsage:
    async def test_executor_reports_usage(self, monkeypatch):
        import app.main as app_main

        async def _get_gateway():
            return _fake_gateway(
                [
                    _Chunk("你好", input_tokens=300, output_tokens=20, model="gpt-4o-mini", provider="openai"),
                    _Chunk("", cached_tokens=64),
                ]
            )

        monkeypatch.setattr(app_main, "get_gateway", _get_gateway)

        out = await _llm_executor("system")(task="你好", model="gpt-4o-mini")
        assert out["result"] == "你好"
        assert out["usage"]["input_tokens"] == 300
        assert out["usage"]["output_tokens"] == 20
        assert out["usage"]["cached_tokens"] == 64
        assert out["usage"]["model"] == "gpt-4o-mini"

    async def test_execute_single_task_consumes_usage(self):
        """usage 从能力输出里摘掉后登记：不能出现在用户可见的聚合输出里。"""

        async def _executor(**kwargs: Any) -> dict[str, Any]:
            return {
                "result": "答案",
                "usage": {
                    "model": "deepseek-chat",
                    "provider": "deepseek",
                    "input_tokens": 900,
                    "output_tokens": 90,
                    "cached_tokens": 0,
                },
            }

        cap = Capability(
            capability_id="agent:probe",
            name="probe",
            description="probe",
            workstation_type=WorkstationType.AGENT,
            capability_type=CapabilityType.SERVICE,
            input_schema=[CapabilityParam("task", "string", "任务", required=True)],
            output_schema=CapabilityResult(fields=["result"]),
            _executor=_executor,
        )
        router = TaskRouter()
        router.registry = _FakeRegistry(cap)  # type: ignore[assignment]

        acc = UsageAccumulator()
        result = await router._execute_single_task(
            SubTask(
                subtask_id="sub_0",
                description="你好",
                capability_id="agent:probe",
                parameters={"task": "你好"},
            ),
            tenant_id=1,
            trace_id="",
            completed_tasks={},
            usage=acc,
        )

        assert result.status == "completed"
        # 输出里不再有 usage：否则它会混进聚合结果（agent 的 result 会被拼进回答）
        assert result.output == {"result": "答案"}
        block = acc.to_block()
        assert block is not None
        assert block["input_tokens"] == 900 and block["output_tokens"] == 90


# ── route_task 的整链回包 ─────────────────────────────────────────


def _stub_pipeline(router: TaskRouter, executed: list[Any]) -> None:
    async def _intent(user_input, context, **kwargs):
        acc = kwargs.get("usage")
        if acc is not None:
            acc.add(model="gpt-4o-mini", provider="openai", input_tokens=100, output_tokens=10)
        return {"type": "general", "keywords": ["你好"], "complexity": "simple"}

    async def _decompose(intent, tenant_id, user_input):
        return [SubTask(subtask_id="sub_0", description="你好", capability_id="agent:probe")]

    async def _match(tasks, tenant_id):
        return tasks

    async def _aggregate(results, intent, user_input):
        return {"reply": "ok"}

    router._understand_intent = _intent  # type: ignore[method-assign]
    router._decompose_task = _decompose  # type: ignore[method-assign]
    router._match_capabilities = _match  # type: ignore[method-assign]
    router._aggregate_results = _aggregate  # type: ignore[method-assign]
    if executed is not None:
        async def _execute(tasks, tenant_id, trace_id="", llm_model="", provider_hint="", usage=None):
            if usage is not None:
                usage.add(model="deepseek-chat", provider="deepseek", input_tokens=700, output_tokens=70)
            return []
        router._execute_tasks = _execute  # type: ignore[method-assign]


class TestRouteTaskUsageBlock:
    async def test_returns_usage_block(self):
        router = TaskRouter()
        _stub_pipeline(router, executed=[])

        result = await router.route_task("你好", tenant_id=1)

        block = result.get("usage")
        assert block is not None, "发生 LLM 调用的回合必须带 usage，否则网关只能记 0 token"
        assert block["input_tokens"] == 700 + 100
        assert block["output_tokens"] == 70 + 10
        # 主模型按 token 占比取 deepseek-chat（700+70 > 100+10）
        assert block["model"] == "deepseek-chat"
        assert block["calls"] == 2

    async def test_no_usage_when_nothing_called(self):
        """纯工具链路：usage 必须是 None（网关标 usage_missing），不能编全 0。"""
        router = TaskRouter()

        async def _intent(user_input, context, **kwargs):
            return {"type": "general", "keywords": ["你好"], "fallback": True}

        async def _decompose(intent, tenant_id, user_input):
            return [SubTask(subtask_id="sub_0", description="读取", capability_id="skill:read_file")]

        async def _match(tasks, tenant_id):
            return tasks

        async def _execute(tasks, tenant_id, trace_id="", llm_model="", provider_hint="", usage=None):
            return []

        async def _aggregate(results, intent, user_input):
            return {"reply": "ok"}

        router._understand_intent = _intent  # type: ignore[method-assign]
        router._decompose_task = _decompose  # type: ignore[method-assign]
        router._match_capabilities = _match  # type: ignore[method-assign]
        router._execute_tasks = _execute  # type: ignore[method-assign]
        router._aggregate_results = _aggregate  # type: ignore[method-assign]

        result = await router.route_task("读文件", tenant_id=1)

        assert result.get("usage") is None
