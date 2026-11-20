"""模型 ↔ provider 兼容路由 + 确定性 4xx 不得冷却密钥（2026-11 子 Agent 全 failed 回归）。

现场（用户报告）：

1. 子 Agent 全部 failed，事件流里两条错误：
   * `claude-sonnet-4-20250514 isn't included in OpenCode Go. …`
   * `NoUsableKeyError: no_usable_key provider=opencode-go-anthropic configured=1
     blocked=0 cooling=1`
2. 卡片显示 `↑0 · ↓0` + "还没有摘要"。

根因链（本文件逐条钉住）：

* `_find_candidates` 匹配不到时退回"全部 provider"，订阅网关成本最低 ⇒ 抢走
  Anthropic 直连模型 → 400；
* Anthropic 流式路径**无条件**上报 key 失败 ⇒ 唯一那把 key 进 30s 冷却 ⇒ 下一次
  直接 `no_usable_key`，把"模型不在套餐"这个真实原因埋掉；
* 失败时 `summary` 是空串（提取式摘要拿不到输出）⇒ 卡片只能显示"还没有摘要"。
"""
from __future__ import annotations

import pytest

from app.gateway import key_ring as key_ring_mod
from app.gateway.errors import (
    is_deterministic_client_error,
    is_model_unavailable,
)
from app.gateway.key_retry import (
    call_with_key_retry,
    is_key_scoped_failure,
    is_retryable_key_error,
)
from app.gateway.key_ring import NoUsableKeyError, last_error_of, note_last_error
from app.gateway.model_compat import model_family, provider_supports_model
from app.gateway.provider import ChatResponse, EmbeddingResponse, LLMProvider
from app.gateway.router import GatewayRouter

CLAUDE = "claude-sonnet-4-20250514"

#: 上游真实文案（OpenCode Go 对套餐外模型的 400）
NOT_IN_PLAN_TEXT = (
    "Error code: 400 - {'error': {'message': \"claude-sonnet-4-20250514 isn't included "
    "in OpenCode Go. See the models it includes at https://opencode.ai/console/go.\", "
    "'type': 'invalid_request_error'}}"
)


class StatusError(Exception):
    """模拟各家 SDK 的状态码异常（字段名取最通用的 `status_code`）。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = message


class FakeProvider(LLMProvider):
    """记录调用次数的最小 provider；`fail_with` 非空时抛它。"""

    def __init__(self, name: str, fail_with: BaseException | None = None) -> None:
        self.name = name
        self.fail_with = fail_with
        self.calls = 0
        self.last_provider_hint = ""

    async def chat_stream(self, messages, model, **kwargs):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        yield ChatResponse(content="ok", finish_reason="stop")

    async def chat(self, messages, model, **kwargs):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return ChatResponse(content="ok", finish_reason="stop")

    async def embed(self, text, model):
        return EmbeddingResponse()

    async def close(self):
        pass


CATALOG = [
    {"id": "anthropic", "model_prefixes": ["claude"], "cost": 3.0, "quality": 0.95},
    {"id": "openai", "model_prefixes": ["gpt", "o1", "o3"], "cost": 5.0,
     "quality": 0.90},
    {"id": "opencode-go", "model_prefixes": [], "cost": 0.4, "quality": 0.85},
    {"id": "opencode-go-anthropic", "model_prefixes": [], "cost": 0.4,
     "quality": 0.85},
]


def _router(providers: dict[str, FakeProvider], catalog=CATALOG) -> GatewayRouter:
    return GatewayRouter(providers=providers, provider_catalog=list(catalog))


# ── 1. 错误分类 ─────────────────────────────────────────────────────────────


class TestErrorClassification:
    def test_not_in_plan_400_is_model_unavailable(self):
        exc = StatusError(400, NOT_IN_PLAN_TEXT)
        assert is_model_unavailable(exc) is True
        # 确定性错误：不重试、不换 key、不冷却
        assert is_deterministic_client_error(exc) is True
        assert is_retryable_key_error(exc) is False
        assert is_key_scoped_failure(exc) is False

    def test_model_not_found_404_is_model_unavailable(self):
        exc = StatusError(404, "model_not_found: no such model kimi-x")
        assert is_model_unavailable(exc) is True
        assert is_retryable_key_error(exc) is False
        assert is_key_scoped_failure(exc) is False

    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_credential_errors_still_cool_keys(self, status):
        exc = StatusError(status, "unauthorized")
        assert is_key_scoped_failure(exc) is True
        assert is_retryable_key_error(exc) is True

    def test_5xx_is_not_deterministic(self):
        exc = StatusError(503, "upstream busy")
        assert is_deterministic_client_error(exc) is False
        assert is_retryable_key_error(exc) is True


# ── 2. 确定性 400 不触发换 key / 不冷却（无重试风暴） ────────────────────────


class TestKeyRetryOnDeterministic400:
    @pytest.mark.asyncio
    async def test_400_does_not_cool_key_nor_retry(self, monkeypatch):
        monkeypatch.setattr(
            "app.gateway.key_retry.key_retry_max", lambda: 3
        )
        reported: list[object] = []
        calls = {"n": 0}
        exc = StatusError(400, NOT_IN_PLAN_TEXT)

        async def resolve():
            return object(), {"key": "sk-test"}

        async def call(_client):
            calls["n"] += 1
            raise exc

        async def report(item):
            reported.append(item)

        with pytest.raises(StatusError):
            await call_with_key_retry(
                provider="opencode-go-anthropic",
                resolve=resolve,
                call=call,
                report_failure=report,
            )

        assert calls["n"] == 1, "确定性 400 不得重试（否则就是重试风暴）"
        assert reported == [], "确定性 400 不得把密钥关进冷却"

    @pytest.mark.asyncio
    async def test_429_cools_and_retries_once(self, monkeypatch):
        monkeypatch.setattr("app.gateway.key_retry.key_retry_max", lambda: 1)
        reported: list[object] = []
        calls = {"n": 0}

        async def resolve():
            return object(), {"key": "sk-test"}

        async def call(_client):
            calls["n"] += 1
            raise StatusError(429, "rate limited")

        async def report(item):
            reported.append(item)

        with pytest.raises(StatusError):
            await call_with_key_retry(
                provider="anthropic",
                resolve=resolve,
                call=call,
                report_failure=report,
            )
        assert calls["n"] == 2
        assert len(reported) == 2


# ── 3. NoUsableKeyError 必须带最后一条上游原因 ───────────────────────────────


class TestNoUsableKeyCarriesReason:
    def teardown_method(self):
        key_ring_mod.clear_last_error()

    def test_reason_is_included_in_message(self):
        note_last_error("opencode-go-anthropic", StatusError(400, NOT_IN_PLAN_TEXT))
        err = NoUsableKeyError(
            "opencode-go-anthropic",
            {"configured": 1, "blocked": 0, "cooling": 1, "retry_after": 24},
        )
        assert "no_usable_key" in str(err)
        assert "cooling=1" in str(err)
        assert "last_error=" in str(err)
        # 真实原因必须可读，且 URL 已脱敏
        assert "isn't included in OpenCode Go" in str(err)
        assert "https://opencode.ai" not in str(err)

    def test_stale_reason_is_not_reported(self):
        note_last_error("x", "old failure")
        # 手动把时间戳推到 TTL 之外
        ts, text = key_ring_mod._LAST_ERRORS["x"]
        key_ring_mod._LAST_ERRORS["x"] = (ts - 10_000, text)
        assert last_error_of("x") == ""
        err = NoUsableKeyError("x", {"configured": 1, "cooling": 1})
        assert "last_error=" not in str(err)


# ── 4. 模型-供应商兼容判定 ──────────────────────────────────────────────────


class TestModelCompatibility:
    @pytest.mark.parametrize(
        "model",
        [CLAUDE, "claude-opus-4", "claude-3-5-haiku-20241022"],
    )
    def test_anthropic_models_are_rejected_by_opencode_go_by_default(self, model):
        # 缺省保守：不把 anthropic 模型路由到订阅网关
        assert provider_supports_model("opencode-go", {}, model) is False
        assert provider_supports_model("opencode-go-anthropic", {}, model) is False

    @pytest.mark.parametrize(
        "model", ["glm-5", "kimi-k2", "qwen3-coder", "deepseek-v4-flash"]
    )
    def test_plan_models_are_accepted(self, model):
        assert provider_supports_model("opencode-go", {}, model) is True

    def test_direct_providers_are_unrestricted(self):
        assert provider_supports_model("anthropic", {}, CLAUDE) is True
        assert provider_supports_model("openai", {}, "gpt-5") is True

    def test_allowlist_is_overridable_via_preset(self):
        preset = {"model_allowlist": ["claude-haiku-5.5"]}
        assert provider_supports_model("opencode-go", preset, "claude-haiku-5.5") is True
        assert provider_supports_model("opencode-go", preset, CLAUDE) is False

    def test_deny_prefix_wins(self):
        preset = {"model_deny_prefixes": ["kimi"]}
        assert provider_supports_model("opencode-go", preset, "kimi-k2") is False

    def test_model_family(self):
        assert model_family(CLAUDE) == "anthropic"
        assert model_family("glm-5") == "zhipu"


# ── 5. 路由决策 ────────────────────────────────────────────────────────────


class TestRoutingDecision:
    def test_claude_never_routed_to_opencode_go(self):
        router = _router(
            {"opencode-go": FakeProvider("opencode-go"),
             "opencode-go-anthropic": FakeProvider("opencode-go-anthropic")}
        )
        assert router._find_candidates(CLAUDE) == []

    def test_claude_routes_to_anthropic_when_configured(self):
        router = _router(
            {"anthropic": FakeProvider("anthropic"),
             "opencode-go": FakeProvider("opencode-go")}
        )
        assert [p.name for p in router._find_candidates(CLAUDE)] == ["anthropic"]

    def test_plan_model_routes_to_opencode_go(self):
        # 只有订阅网关（没有直连 provider 抢路由）时，套餐内模型照常路由过去
        router = _router({"opencode-go": FakeProvider("opencode-go")})
        assert [p.name for p in router._find_candidates("glm-5")] == ["opencode-go"]

    @pytest.mark.asyncio
    async def test_hint_to_incompatible_provider_is_ignored(self):
        """会话 provider=opencode-go，但模型是 Anthropic 直连模型 ⇒ 必须改投兼容 provider。"""
        anthropic = FakeProvider("anthropic")
        go = FakeProvider("opencode-go")
        router = _router({"anthropic": anthropic, "opencode-go": go})
        chunks = [
            c
            async for c in router.chat_stream(
                [], CLAUDE, provider_hint="opencode-go"
            )
        ]
        assert anthropic.calls == 1
        assert go.calls == 0
        assert chunks[-1].provider == "anthropic"

    @pytest.mark.asyncio
    async def test_no_compatible_provider_fails_fast_with_actionable_error(self):
        """只有订阅网关配了 key ⇒ fail-fast，错误里点名"需要 provider=anthropic"。"""
        go = FakeProvider("opencode-go")
        router = _router({"opencode-go": go})
        chunks = [c async for c in router.chat_stream([], CLAUDE, provider_hint="opencode-go")]
        last = chunks[-1]
        assert last.finish_reason == "error"
        assert "anthropic" in (last.message or "")
        assert last.provider == "none"
        assert go.calls == 0, "不兼容时绝不打上游（否则就是那个 400）"

    @pytest.mark.asyncio
    async def test_upstream_400_does_not_trip_breaker_and_is_actionable(self):
        """上游真的返回 400 时：不记熔断失败、错误文案可行动、原因被记下。"""
        go = FakeProvider("opencode-go", fail_with=StatusError(400, NOT_IN_PLAN_TEXT))
        router = _router({"opencode-go": go})
        # 用套餐内模型，绕过本地兼容判定，模拟"上游口径比本地白名单更窄"
        chunks = [c async for c in router.chat_stream([], "glm-5")]
        message = chunks[-1].message or ""
        assert chunks[-1].finish_reason == "error"
        assert "不支持" in message
        assert "套餐内可用模型族" in message
        assert "upstream:" in message
        assert router._breakers["opencode-go"]._failure_count == 0
        assert "isn't included in OpenCode Go" in last_error_of("opencode-go")


# ── 6. 子 Agent 继承父模型 / 失败摘要 ────────────────────────────────────────


class TestSubagentInheritance:
    def test_parent_llm_config_is_inherited_from_tool_context(self):
        from app.agent.subagent_runner import _parent_llm_config
        from app.tools.context import get_all, restore_context, set_tool_context

        snapshot = get_all()
        try:
            set_tool_context(llm_model=CLAUDE, llm_provider="anthropic")
            assert _parent_llm_config() == {"model": CLAUDE, "provider": "anthropic"}
        finally:
            restore_context(snapshot)

    def test_missing_context_yields_empty(self):
        from app.agent.subagent_runner import _parent_llm_config
        from app.tools.context import get_all, restore_context, set_tool_context

        snapshot = get_all()
        try:
            set_tool_context(llm_model="", llm_provider="")
            assert _parent_llm_config() == {}
        finally:
            restore_context(snapshot)


class TestFailureSummary:
    def test_failure_summary_includes_upstream_reason(self):
        from app.subagent.reporting import failure_summary

        text = failure_summary("failed", [NOT_IN_PLAN_TEXT])
        assert "失败原因" in text
        assert "isn't included in OpenCode Go" in text

    def test_failure_summary_redacts_url_and_truncates(self):
        from app.subagent.reporting import FAILURE_MAX_CHARS, failure_summary

        reason = "bad key sk-abcdefghijklmnopqrstuvwxyz012345 " + "x" * 2000
        text = failure_summary("failed", [reason])
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text
        assert len(text) <= FAILURE_MAX_CHARS
        assert text.endswith("…")

    def test_completed_run_has_no_failure_summary(self):
        from app.subagent.reporting import failure_summary

        assert failure_summary("completed", ["ignored"]) == ""


# ── 7. Anthropic 流式路径：确定性 400 不得把 key 关进冷却 ─────────────────────


class TestAnthropicStreamKeyCooling:
    """回归：`chat_stream` 的 except 曾**无条件** `_report_failure`。

    唯一一把 key 因此被 400 冷却 30s，下一次请求只剩 `no_usable_key … cooling=1`。
    """

    @staticmethod
    def _provider(exc: BaseException):
        from app.providers.anthropic import AnthropicProvider

        class _KeyRing:
            def __init__(self):
                self.reported: list[str] = []

            async def report_failure(self, provider, key):
                self.reported.append(key)

            async def get_key(self, provider):
                return {"key": "sk-test", "id": "d"}

            async def diagnose(self, provider):
                return {"configured": 1, "cooling": 1, "retry_after": 30}

        class _Stream:
            async def __aenter__(self):
                raise exc

            async def __aexit__(self, *a):
                return False

        class _Messages:
            def stream(self, **kwargs):
                return _Stream()

        class _Client:
            messages = _Messages()

        provider = object.__new__(AnthropicProvider)

        async def _resolve():
            return _Client(), {"key": "sk-test", "id": "d"}

        provider._key_ring = _KeyRing()
        provider._resolve_client = _resolve
        return provider

    @pytest.mark.asyncio
    async def test_400_does_not_cool_the_key(self):
        from app.gateway.provider import ChatMessage

        provider = self._provider(StatusError(400, NOT_IN_PLAN_TEXT))
        with pytest.raises(StatusError):
            [c async for c in provider.chat_stream([ChatMessage(role="user", content="hi")], CLAUDE)]
        assert provider._key_ring.reported == []
        # 原因被记下 —— 下一次 NoUsableKeyError 才能说出真实原因
        assert "isn't included in OpenCode Go" in last_error_of("anthropic")

    @pytest.mark.asyncio
    async def test_429_still_cools_the_key(self):
        from app.gateway.provider import ChatMessage

        provider = self._provider(StatusError(429, "rate limited"))
        with pytest.raises(StatusError):
            [c async for c in provider.chat_stream([ChatMessage(role="user", content="hi")], CLAUDE)]
        assert provider._key_ring.reported == ["sk-test"]


# ── 8. 子 Agent 失败时 summary 必须带原因（卡片不再"还没有摘要"） ─────────────


class TestRunnerFailureSummary:
    @pytest.mark.asyncio
    async def test_failed_run_writes_reason_into_summary_and_event_stream(self):
        from app.agent.subagent_runner import SubAgentRunner

        class _Store:
            def __init__(self):
                self.finish: dict[str, dict] = {}

            async def start_run(self, **kwargs):
                return ""

            async def add_step(self, *a, **k):
                return None

            async def flush_steps(self, run_id):
                return None

            async def finish_run(self, run_id, **kwargs):
                self.finish[run_id] = kwargs

            async def mark_lifecycle(self, *a, **k):
                return None

        class _Sink:
            def __init__(self):
                self.notices: list[str] = []
                self.done: dict = {}

            def emit_started(self, **k):
                return None

            def emit_progress(self, **k):
                self.notices.append(str(k.get("content") or ""))

            def emit_done(self, **k):
                self.done = k

            def __getattr__(self, _name):
                return lambda **k: None

        store, sink = _Store(), _Sink()
        runner = SubAgentRunner(
            store=store, gateway=object(), sink=sink, allow_write=False
        )
        result = await runner.run("干点活", mode="normal", max_turns=1)

        assert result.status == "failed"
        assert "失败原因" in result.summary
        # 落库的 summary 与返回体同一条（前端卡片读 DB/终态事件的 summary）
        saved = next(iter(store.finish.values()))
        assert "失败原因" in (saved.get("summary") or "")
        assert saved.get("status") == "failed"
        assert sink.done.get("summary")
        # 事件流里也能直接看到（不是"只有 DB 里才有"）
        assert sink.notices, "失败原因必须进事件流"
        assert "chat_stream" in sink.notices[0] or "失败" in sink.notices[0]


