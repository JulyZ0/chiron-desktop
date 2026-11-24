"""OpenCode 的 `x-opencode-session` 头必须被注入（且只对 OpenCode 注入）。

回归背景（真实故障）：OpenCode Go 强制要求该头，缺失时直接

    400 MissingSessionID: Request is missing x-opencode-session and cannot be routed
    efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it

而 Chiron 此前完全不发这个头 —— 用户把 provider 配好、模型也选对了，请求照样失败。
该头只影响对方的路由与 prompt 缓存，所以用「同一会话的稳定 ID」注入最合适。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.api.unified_executor import UnifiedChatHandler
from app.core.capabilities import (
    Capability,
    CapabilityParam,
    CapabilityResult,
    CapabilityType,
    WorkstationType,
    _llm_executor,
)
from app.core.task_router import SubTask, TaskRouter, quick_execute
from app.providers.named import NamedOpenAIProvider
from app.providers.session_context import (
    get_llm_session_id,
    reset_llm_session_id,
    set_llm_session_id,
)


def _provider(name: str) -> NamedOpenAIProvider:
    return NamedOpenAIProvider(name, api_key="sk-fake-for-test")


def test_opencode_go_injects_session_header():
    set_llm_session_id("sess-abc-123")
    kwargs = _provider("opencode-go")._build_kwargs([], "glm-5", 1024, 0.5, None)
    assert kwargs["extra_headers"] == {"x-opencode-session": "sess-abc-123"}


def test_opencode_zen_variants_also_inject():
    """四个 OpenCode 变体（Zen/Go × openai/anthropic）名字都以 opencode 开头。"""
    set_llm_session_id("sess-x")
    for name in ("opencode", "opencode-anthropic", "opencode-go", "opencode-go-anthropic"):
        kwargs = _provider(name)._build_kwargs([], "m", 100, 0.0, None)
        assert kwargs.get("extra_headers") == {"x-opencode-session": "sess-x"}, name


def test_other_providers_get_no_session_header():
    """其他 provider 不应被塞这个头（避免把内部标识泄露给无关第三方）。"""
    set_llm_session_id("sess-abc")
    kwargs = _provider("deepseek")._build_kwargs([], "deepseek-chat", 1024, 0.5, None)
    assert "extra_headers" not in kwargs


def test_no_session_id_means_no_header():
    """会话未知时不注入（行为与旧版本一致，由对方自行决定是否拒绝）。"""
    set_llm_session_id("")
    kwargs = _provider("opencode-go")._build_kwargs([], "glm-5", 100, 0.0, None)
    assert "extra_headers" not in kwargs


@pytest.fixture(autouse=True)
def _isolate_session_context():
    token = set_llm_session_id("")
    try:
        yield
    finally:
        reset_llm_session_id(token)


class _HeaderCapturingGateway:
    """在 gateway 边界执行真实 provider header 构造，模拟完整调用链。"""

    def __init__(self, *, provider: str = "opencode-go") -> None:
        self.provider = provider
        self.headers: list[dict[str, str] | None] = []
        self.session_ids: list[str] = []
        self.calls = 0

    async def chat_stream(self, messages: Any, model: str, **kwargs: Any):
        self.calls += 1
        self.session_ids.append(get_llm_session_id())
        kwargs_out = _provider(self.provider)._build_kwargs(
            messages, model, 100, 0.0, None
        )
        self.headers.append(kwargs_out.get("extra_headers"))
        if self.calls == 1:
            content = json.dumps(
                {
                    "action": "general",
                    "keywords": ["你好"],
                    "entities": {},
                    "complexity": "simple",
                    "description": "general chat",
                }
            )
        else:
            content = "ok"
        yield SimpleNamespace(
            content=content,
            model=model,
            provider=self.provider,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            finish_reason="stop",
        )


def _single_agent_router(monkeypatch: pytest.MonkeyPatch) -> TaskRouter:
    """只保留一个真实 LLM 能力，避免依赖全局能力注册表。"""

    cap = Capability(
        capability_id="agent:general_chat",
        name="General Chat",
        description="test",
        workstation_type=WorkstationType.AGENT,
        capability_type=CapabilityType.SERVICE,
        input_schema=[CapabilityParam("task", "string", "任务", required=True)],
        output_schema=CapabilityResult(fields=["reply"]),
        tags=["chat"],
        _executor=_llm_executor("test system prompt"),
    )

    class _Registry:
        async def get_by_id(self, capability_id: str, tenant_id: int = 0):
            return cap if capability_id == cap.capability_id else None

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.trace.record_span", _noop)
    router = TaskRouter()
    router.registry = _Registry()  # type: ignore[assignment]

    async def _decompose(intent, tenant_id, user_input):
        return [
            SubTask(
                subtask_id="sub_0",
                description=user_input,
                capability_id=cap.capability_id,
                parameters={"task": user_input},
            )
        ]

    async def _match(tasks, tenant_id):
        return tasks

    router._decompose_task = _decompose  # type: ignore[method-assign]
    router._match_capabilities = _match  # type: ignore[method-assign]
    return router


async def test_quick_submit_injects_same_session_header_across_turns(monkeypatch):
    """首页 quick-execute → submit_task → 两段 LLM → provider 均带同一会话头。"""
    gateway = _HeaderCapturingGateway()

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)
    handler = UnifiedChatHandler()
    handler.router = _single_agent_router(monkeypatch)
    llm_config = {"model": "glm-5", "provider": "opencode-go"}

    first = await handler.submit_task(
        "第一轮", tenant_id=1, session_id="quick-session", llm_config=llm_config
    )
    second = await handler.submit_task(
        "第二轮", tenant_id=1, session_id="quick-session", llm_config=llm_config
    )

    assert first["success"] is True, first
    assert second["success"] is True, second
    # 每轮意图理解 + agent:* 各一次，共四次 provider 请求。
    assert gateway.headers == [{"x-opencode-session": "quick-session"}] * 4
    assert gateway.session_ids == ["quick-session"] * 4
    assert get_llm_session_id() == ""


async def test_quick_execute_without_session_uses_task_id_and_resets(monkeypatch):
    """无 session 时用本次 task_id 兜底；成功返回后恢复外层 ContextVar。"""
    gateway = _HeaderCapturingGateway()
    router = _single_agent_router(monkeypatch)

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)
    monkeypatch.setattr("app.core.task_router.TaskRouter", lambda: router)
    outer = set_llm_session_id("outer-session")
    try:
        result = await quick_execute("你好", tenant_id=1, trace_id="trace-quick")
        assert result["status"] == "completed", result
        assert gateway.headers == [{"x-opencode-session": result["task_id"]}] * 2
        assert get_llm_session_id() == "outer-session"
    finally:
        reset_llm_session_id(outer)


async def test_router_resets_session_context_on_error(monkeypatch):
    """编排异常也不能把路由会话 ID 遗留在调用方上下文。"""
    gateway = _HeaderCapturingGateway()
    router = _single_agent_router(monkeypatch)

    async def _get_gateway():
        return gateway

    async def _decompose(*args, **kwargs):
        raise RuntimeError("decompose failed")

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)
    router._decompose_task = _decompose  # type: ignore[method-assign]
    outer = set_llm_session_id("outer-session")
    try:
        result = await router.route_task(
            "你好", tenant_id=1, context={"session_id": "error-session"}
        )
        assert result["status"] == "error"
        assert gateway.headers == [{"x-opencode-session": "error-session"}]
        assert get_llm_session_id() == "outer-session"
    finally:
        reset_llm_session_id(outer)


async def test_concurrent_routes_do_not_share_session_headers(monkeypatch):
    """两个并发路由分别持有自己的会话 ID，不因 ContextVar 复用而串线。"""

    class _BlockingGateway(_HeaderCapturingGateway):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_stream(self, messages: Any, model: str, **kwargs: Any):
            async for chunk in super().chat_stream(messages, model, **kwargs):
                if self.calls == 1:
                    # header 已捕获后再阻塞，确保两个请求在 provider 边界真实重叠。
                    self.started.set()
                    await self.release.wait()
                yield chunk

    gateways = {"session-a": _BlockingGateway(), "session-b": _BlockingGateway()}
    routers = {sid: _single_agent_router(monkeypatch) for sid in gateways}

    async def _get_gateway():
        # 每个路由任务在自己的 ContextVar 中运行；这里按当前会话 ID 取对应 gateway。
        return gateways[get_llm_session_id()]

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)
    tasks = [
        asyncio.create_task(
            routers[sid].route_task("你好", tenant_id=1, context={"session_id": sid})
        )
        for sid in gateways
    ]
    await asyncio.gather(*(gateway.started.wait() for gateway in gateways.values()))
    assert gateways["session-a"].headers == [{"x-opencode-session": "session-a"}]
    assert gateways["session-b"].headers == [{"x-opencode-session": "session-b"}]
    for gateway in gateways.values():
        gateway.release.set()

    results = await asyncio.gather(*tasks)
    assert all(result["status"] == "completed" for result in results)
    for sid, gateway in gateways.items():
        assert gateway.headers == [{"x-opencode-session": sid}] * 2
    assert get_llm_session_id() == ""
