"""``app/agent/runtime_config.py`` —— 后台「Agent 运行配置」的运行时入口。

这一层有三处"会静默改变 agent 行为"的地方，因此都钉在测试里：

1. **坏值只丢该键，不整份拒绝**。后台保存的配置可能来自旧版本、或被手工改坏；
   ``apply`` 若整份拒绝，用户会看到"只改了一格却什么都没生效"，比只丢一格更难排查。
2. **读不到配置时保持现状，绝不回落成默认值**。网关不可达 / 未配 internal_token 都返回
   ``False`` —— 否则"网关一抖，配置归零"，运行中的会话会突然换一套策略。
3. **默认值逐字保持不变**。``narration=off`` 必须渲染空串，system 段才与引入本项之前
   逐字相同（前缀缓存照常命中，行为零变化）。这条最容易在"顺手美化文案"时被破坏。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent import runtime_config
from app.config import settings

#: 每个用例开始时把六个字段复位成 env 兜底值，避免上一个用例的写入漏到下一个。
_DEFAULTS = {
    "agent_plan_approval": "never",
    "agent_interaction_wait_seconds": 240.0,
    "agent_narration": "off",
    "agent_no_progress_notice": True,
    "agent_no_progress_threshold": 3,
    "agent_token_window_ratio": 8.0,
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for field, value in _DEFAULTS.items():
        monkeypatch.setattr(settings, field, value, raising=False)
    yield


class _FakeResponse:
    """够 ``hydrate`` 用的最小响应：上下文管理 + ``read()``。"""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _patch_gateway(monkeypatch, *, payload: object | None = None, error: Exception | None = None):
    """拦下 ``urllib.request.urlopen`` 并记录被调的 URL。"""
    calls: list[str] = []

    def _urlopen(req, timeout=None):  # noqa: ANN001 - 签名只求够用
        calls.append(getattr(req, "full_url", str(req)))
        if error is not None:
            raise error
        return _FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return calls


# ── apply：坏值只丢该键 ──


def test_apply_rejects_empty_config() -> None:
    assert runtime_config.apply(None) is False
    assert runtime_config.apply({}) is False
    assert settings.agent_plan_approval == "never"


def test_apply_takes_valid_values() -> None:
    assert runtime_config.apply(
        {
            "plan_approval": "always",
            "interaction_wait_seconds": 600,
            "narration": "brief",
            "no_progress_notice": False,
            "no_progress_threshold": 5,
            "token_window_ratio": 4,
        }
    )
    assert settings.agent_plan_approval == "always"
    assert settings.agent_interaction_wait_seconds == 600.0
    assert settings.agent_narration == "brief"
    assert settings.agent_no_progress_notice is False
    assert settings.agent_no_progress_threshold == 5
    assert settings.agent_token_window_ratio == 4.0


def test_apply_drops_only_the_bad_key() -> None:
    # 一格写错不该让其余五格一起失效 —— 那正是"整份拒绝"最难排查的地方。
    assert runtime_config.apply(
        {"plan_approval": "explode", "narration": "detailed", "no_progress_threshold": 7}
    )
    assert settings.agent_plan_approval == "never"  # 被丢，保持原值
    assert settings.agent_narration == "detailed"  # 照常生效
    assert settings.agent_no_progress_threshold == 7


def test_apply_ignores_unknown_keys() -> None:
    # 只收 `_FIELD_MAP` 声明的键：一次坏配置不该把任意字段写进 settings。
    assert runtime_config.apply({"agent_plan_approval": "always"}) is False
    assert settings.agent_plan_approval == "never"


def test_apply_explicit_null_keeps_current() -> None:
    # 与 SaveSettings 的"value 为 null 即删除"同义：不覆盖当前生效值。
    assert runtime_config.apply({"narration": None, "plan_approval": "always"})
    assert settings.agent_narration == "off"
    assert settings.agent_plan_approval == "always"


def test_apply_bounds_are_inclusive() -> None:
    # 0 = 关闭规则，所以阈值下界必须是 0；倍数从 0.5 起才算"真设了一个倍数"。
    assert runtime_config.apply({"no_progress_threshold": 0})
    assert settings.agent_no_progress_threshold == 0
    assert runtime_config.apply({"token_window_ratio": 0.5})
    assert settings.agent_token_window_ratio == 0.5
    assert runtime_config.apply({"token_window_ratio": 100})
    assert settings.agent_token_window_ratio == 100.0


def test_apply_rejects_out_of_bounds() -> None:
    assert runtime_config.apply({"no_progress_threshold": 101}) is False
    assert runtime_config.apply({"token_window_ratio": 0.4}) is False
    assert runtime_config.apply({"interaction_wait_seconds": 0}) is False
    assert settings.agent_no_progress_threshold == 3
    assert settings.agent_token_window_ratio == 8.0


def test_apply_coerces_loose_scalars() -> None:
    # 后台表单/旧版数据可能存成字符串；认得出的取值应当被采纳，而不是当作坏值丢掉。
    assert runtime_config.apply(
        {"no_progress_notice": "false", "token_window_ratio": "6", "plan_approval": " ALWAYS "}
    )
    assert settings.agent_no_progress_notice is False
    assert settings.agent_token_window_ratio == 6.0
    assert settings.agent_plan_approval == "always"


# ── hydrate：读不到就保持现状 ──


def test_hydrate_without_token_does_not_call_gateway(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "", raising=False)
    calls = _patch_gateway(monkeypatch, payload={"data": {"plan_approval": "always"}})
    assert runtime_config.hydrate() is False
    assert calls == []


def test_hydrate_keeps_current_on_gateway_error(monkeypatch) -> None:
    # 网关不可达是启动期的常态（引擎常比网关先起来）—— 不能因此把策略重置成默认值。
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    _patch_gateway(monkeypatch, error=OSError("connection refused"))
    assert runtime_config.hydrate() is False
    assert settings.agent_plan_approval == "never"


def test_hydrate_ignores_empty_payload(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    _patch_gateway(monkeypatch, payload={"data": {}})
    assert runtime_config.hydrate() is False


def test_hydrate_applies_gateway_config(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    monkeypatch.setattr(settings, "gateway_internal_url", "http://gw:8080", raising=False)
    calls = _patch_gateway(
        monkeypatch, payload={"data": {"plan_approval": "always", "narration": "brief"}}
    )
    assert runtime_config.hydrate() is True
    assert settings.agent_plan_approval == "always"
    assert settings.agent_narration == "brief"
    # 端点名是引擎与网关之间的契约（见 internal/api/agent_config.go）。
    assert calls == ["http://gw:8080/v1/internal/agent-config"]


def test_hydrate_accepts_bare_config_body(monkeypatch) -> None:
    # 网关将来若直接返回配置本身（不带 data 信封）也要能用 —— 两种形状都见过。
    monkeypatch.setattr(settings, "internal_token", "tok", raising=False)
    _patch_gateway(monkeypatch, payload={"narration": "detailed"})
    assert runtime_config.hydrate() is True
    assert settings.agent_narration == "detailed"


# ── 派生读取：消费点读的是这三个函数 ──


def test_plan_approval_required() -> None:
    assert runtime_config.plan_approval_required() is False
    settings.agent_plan_approval = "always"
    assert runtime_config.plan_approval_required() is True


def test_no_progress_threshold_never_negative() -> None:
    settings.agent_no_progress_threshold = -3
    assert runtime_config.no_progress_threshold() == 0


def test_token_window_ratio_falls_back_when_non_positive() -> None:
    # 0/负数会让 `scale_to_window` 失去意义 —— 退回默认倍数而不是关掉后备线。
    settings.agent_token_window_ratio = 0
    assert runtime_config.token_window_ratio() == 8.0


def test_state_reports_current_values() -> None:
    settings.agent_plan_approval = "always"
    settings.agent_token_window_ratio = 12.0
    snapshot = runtime_config.state()
    assert snapshot["plan_approval"] == "always"
    assert snapshot["token_window_ratio"] == 12.0
    assert set(snapshot) == set(runtime_config._FIELD_MAP)


# ── 叙述片段：off 必须逐字不变 ──


def test_narration_off_renders_nothing() -> None:
    # 空串是"零行为变化"的实现方式：system 段与引入本项之前逐字相同。
    assert runtime_config.render_narration_suffix() == ""


def test_narration_on_renders_instructions() -> None:
    settings.agent_narration = "brief"
    brief = runtime_config.render_narration_suffix()
    settings.agent_narration = "detailed"
    detailed = runtime_config.render_narration_suffix()
    assert brief and detailed
    assert brief != detailed
    # 标题是 prompt_sections 拼装时依赖的锚点（见 app/agent/prompt_sections.py）。
    assert brief.startswith("## Progress narration")
    assert detailed.startswith("## Progress narration")


# ── 监听任务本身：不能再因 `await hydrate()`（同步函数）当场死掉 ──


def test_listen_for_changes_survives_startup(monkeypatch) -> None:
    """回归网：监听任务必须能进入订阅循环，而不是启动即异常退出。

    此前 `await hydrate()` 等的是同步函数，`TypeError: 'bool' object can't be
    awaited` 直接杀掉任务 —— 引擎从此不订阅设置广播，后台六项配置全部不生效。
    """

    monkeypatch.setattr(runtime_config, "_hydrate_once", lambda: (True, False))

    async def _redis_down():
        raise RuntimeError("redis down (test)")

    monkeypatch.setattr("app.redis_client.get_redis", _redis_down)

    async def _run() -> None:
        task = asyncio.create_task(runtime_config.listen_for_changes())
        await asyncio.sleep(0.05)
        assert not task.done(), "监听任务不应在启动阶段异常退出"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
