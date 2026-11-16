"""M23：换 key 重试的两半 —— 错误分类与编排（提交点之前、预算默认 1）。

为什么要单独钉这两件事：
1. **分类错了会静默出两种事故**：把 400（请求本身错）当可重试，就白花一次调用并掩盖真错误；
   把 429（该 key 限流）当不可重试，用户就会看到一次本可避免的失败或模型降级。
2. **编排错了会重复计费**：只在"还没向调用方产出任何东西"之前换 key —— 这里用
   `resolve`/`call` 两个可注入的协程把"提交点"变成可断言的事实。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.gateway import errors as errors_mod
from app.gateway import key_retry as kr
from app.gateway.key_ring import NoUsableKeyError


class _StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


class _TimeoutError(Exception):
    """类名含 `timeout` ⇒ 传输类（不 import 任何 SDK，见 `_TRANSPORT_HINTS`）。"""


@pytest.fixture(autouse=True)
def _default_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 1)


# ── 错误分类 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 408, 409, 429, 500, 502, 503, 504])
def test_retryable_statuses(status: int) -> None:
    assert kr.is_retryable_key_error(_StatusError(status)) is True


@pytest.mark.parametrize("status", [400, 404, 422, 451])
def test_non_retryable_statuses(status: int) -> None:
    """请求本身错 / 内容策略：换 key 结果一样，重试只是白花一次调用。"""
    assert kr.is_retryable_key_error(_StatusError(status)) is False


def test_transport_errors_are_retryable() -> None:
    assert kr.is_retryable_key_error(_TimeoutError("connect timed out")) is True


def test_unknown_error_is_not_retryable() -> None:
    """判不出来就不重试：宁可让调用方按既有语义降级，也不要盲目放大上游压力。"""
    assert kr.is_retryable_key_error(ValueError("boom")) is False


def test_status_is_read_from_response_object() -> None:
    class _WrappedError(Exception):
        def __init__(self) -> None:
            super().__init__("wrapped")
            self.response = type("R", (), {"status_code": 429})()

    assert kr.is_retryable_key_error(_WrappedError()) is True


# ── 预算开关 ──────────────────────────────────────────────────────────────


def test_key_scoped_only_for_credential_statuses() -> None:
    """冷却/换 key 只跟着**凭证**走：401/403/429 是这把 key 的事，其余不是。"""
    for status in sorted(kr.KEY_SCOPED_STATUS):
        assert kr.is_key_scoped_failure(_StatusError(status)) is True
    for status in (400, 404, 408, 409, 422, 500, 503):
        assert kr.is_key_scoped_failure(_StatusError(status)) is False
    assert kr.is_key_scoped_failure(_TimeoutError("t")) is False


def test_is_key_scoped_is_a_strict_subset_of_retryable() -> None:
    """凭证类一定也算可重试（否则"该冷却却不重试"这类口径漂移会被静默吞掉）。"""
    for status in sorted(kr.KEY_SCOPED_STATUS):
        assert kr.is_retryable_key_error(_StatusError(status)) is True


def test_budget_defaults_and_zero_means_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 1)
    assert kr.key_retry_max() == 1
    monkeypatch.setattr(settings, "key_retry_max", 0)
    assert kr.key_retry_max() == 0
    monkeypatch.setattr(settings, "key_retry_max", -3)
    assert kr.key_retry_max() == 0, "负值等价于关"


def test_budget_falls_back_when_config_is_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", "n/a")
    assert kr.key_retry_max() == kr.DEFAULT_KEY_RETRY_MAX


# ── 编排 ──────────────────────────────────────────────────────────────────


class _Harness:
    """记录每次取到的 key 与调用结果，供断言"换没换 key、报没报失败"。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.resolved = 0
        self.calls: list[str] = []
        self.reported: list[str] = []

    async def resolve(self) -> tuple[Any, Any]:
        self.resolved += 1
        key = f"k{self.resolved}"
        return (f"client-{key}", {"key": key})

    async def call(self, client: Any) -> str:
        self.calls.append(str(client))
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    async def report_failure(self, item: Any) -> None:
        self.reported.append(str((item or {}).get("key", "")))


async def test_retries_with_a_different_key_on_retryable_error() -> None:
    h = _Harness([_StatusError(429), "second-try-ok"])

    got = await kr.call_with_key_retry(
        provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
    )

    assert got == "second-try-ok"
    assert h.calls == ["client-k1", "client-k2"], "必须换一把 key（重新 resolve）"
    assert h.reported == ["k1"], "旧 key 无论是否重试都要上报冷却"


async def test_does_not_retry_on_non_retryable_error() -> None:
    h = _Harness([_StatusError(400)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1, "不可重试的错误不该再取一把 key"
    assert h.reported == [], "400 是请求本身的错，不该让这把 key 替它背冷却"


async def test_transport_and_5xx_do_not_cool_the_key() -> None:
    """链路类错误（超时/5xx）**不**冷却 key：冷却按 key 计，而一把 key 通常挂着多个模型。

    回归的是这个具体事故：一次瞬时抖动把唯一的 key 关进 30s 冷却 ⇒ 同轮下一次调用
    取不到 key ⇒ 曾经静默换上占位 key 打上游，用户看到的是"你的 API Key 无效"。
    """
    for outcome in (_TimeoutError("connect timed out"), _StatusError(503)):
        h = _Harness([outcome])
        with pytest.raises(type(outcome)):
            await kr.call_with_key_retry(
                provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
            )
        assert h.resolved == 1, f"{outcome} 不该触发换 key"
        assert h.reported == [], f"{outcome} 不该触发冷却"


async def test_original_error_survives_the_cooldown_it_caused() -> None:
    """报完失败后取不到第二把 key ⇒ 抛**首个真实错误**，不拿"没 key"盖掉它。"""

    class _OnlyOneKey(_Harness):
        async def resolve(self) -> tuple[Any, Any]:
            self.resolved += 1
            if self.resolved == 1:
                return ("client-k1", {"key": "k1"})
            raise NoUsableKeyError("openai", {"configured": 1, "cooling": 1, "retry_after": 30})

    h = _OnlyOneKey([_StatusError(401)])

    with pytest.raises(_StatusError) as got:
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert "401" in str(got.value), "上游说这把 key 无效 ⇒ 这条信息必须留给用户"
    assert h.reported == ["k1"]


async def test_no_usable_key_on_first_resolve_is_not_swallowed() -> None:
    """第一轮就没 key：直接报"没有可用密钥"，不编造上游判决。"""

    class _NoRingKey(_Harness):
        async def resolve(self) -> tuple[Any, Any]:
            self.resolved += 1
            raise NoUsableKeyError("openai", {"configured": 0})

    h = _NoRingKey(["unused"])

    with pytest.raises(NoUsableKeyError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )
    assert h.calls == []


async def test_budget_zero_disables_the_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_retry_max", 0)
    h = _Harness([_StatusError(429)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1


async def test_no_key_available_is_not_retried() -> None:
    """取不到 key（KeyRing 空）时重试没有意义 —— 再取一次还是空的。"""

    class _NoKey(_Harness):
        async def resolve(self) -> tuple[Any, Any]:
            self.resolved += 1
            return (None, None)

    h = _NoKey([_StatusError(429)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1


async def test_budget_exhausted_raises_the_second_error() -> None:
    h = _Harness([_StatusError(429), _StatusError(429)])

    with pytest.raises(_StatusError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 2, "默认预算 1 ⇒ 最多两把 key"
    assert h.reported == ["k1", "k2"]


async def test_first_try_success_does_not_report_or_retry() -> None:
    h = _Harness(["ok"])

    got = await kr.call_with_key_retry(
        provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
    )

    assert got == "ok"
    assert h.resolved == 1 and h.reported == []


# ── G-L5：配额/计费耗尽 ≠ 限流 ─────────────────────────────────────────────
#
# 两者同为 429，处置却相反：限流换一把 key 有救；配额耗尽是**账号级**的事实，
# 换多少把 key 都是同一堵墙。误判成限流的代价是双重误诊 —— 白烧一次往返，
# 还会把 key 关进冷却，用户最后看到"没有可用密钥"，与真实原因（该充值）无关。


class _QuotaError(Exception):
    """OpenAI 官方配额耗尽的真实形状：429 + code/message 明确指向额度。"""

    def __init__(self, status: int = 429) -> None:
        super().__init__("You exceeded your current quota, please check your plan and billing details.")
        self.status_code = status
        self.code = "insufficient_quota"


def test_quota_exhausted_is_recognized_from_official_shape() -> None:
    assert errors_mod.is_quota_exhausted(_QuotaError()) is True


@pytest.mark.parametrize(
    "marker",
    [
        "insufficient_quota",
        "exceeded your current quota",
        "billing_hard_limit_reached",
        "credit balance is too low",
        "insufficient_balance",
        "account_deactivated",
        "额度不足",
        "余额不足",
    ],
)
def test_quota_markers_are_recognized(marker: str) -> None:
    err = _StatusError(429)
    err.args = (f"provider said: {marker}",)
    assert errors_mod.is_quota_exhausted(err) is True


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached for gpt-4o in organization org-x on requests per min",
        "Rate limit exceeded, please retry after 20s",
        "Too many requests",
        "The engine is currently overloaded, please try again later",
    ],
)
def test_plain_rate_limit_is_not_quota(message: str) -> None:
    """限流文案里常带 `limit` / `quota` 的近义说法，不能被误判成额度耗尽 ——
    那个方向的误报更贵（用户看到一次本可避免的失败）。"""
    err = _StatusError(429)
    err.args = (message,)
    assert errors_mod.is_quota_exhausted(err) is False


def test_payment_required_is_quota_without_reading_text() -> None:
    """402 本身就是配额/计费类，不依赖文案。"""
    assert errors_mod.is_quota_exhausted(_StatusError(402)) is True


def test_quota_error_neither_cools_the_key_nor_retries() -> None:
    """核心契约：配额耗尽**既不换 key、也不冷却** —— 它是账号的事，不是这把凭证的事。"""
    exc = _QuotaError()
    assert kr.is_key_scoped_failure(exc) is False, "配额类不是凭证的错，不该冷却 key"
    assert kr.is_retryable_key_error(exc) is False, "换 key 也撞同一堵墙"


async def test_quota_does_not_burn_the_retry_budget() -> None:
    """整条链路上的后果：只调一次模型，不换 key，原样抛出原始错误。"""
    h = _Harness([_QuotaError(), "should-not-be-used"])

    with pytest.raises(_QuotaError):
        await kr.call_with_key_retry(
            provider="openai", resolve=h.resolve, call=h.call, report_failure=h.report_failure
        )

    assert h.resolved == 1, "不该再取第二把 key"
    assert h.reported == [], "配额类不该让 key 进冷却"
