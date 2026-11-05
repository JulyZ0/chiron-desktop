"""隔离档位**硬底线**：`none` / `job` 不能在 Windows 上被配置选中。

为什么需要这组测试
------------------
`SANDBOX_ISOLATION_ALLOW_DOWNGRADE` 原本是一个能放行**任何**降级的开关 ——
包括降到 `none`（完全没有隔离）。这与 README 的
「执行隔离是默认值；沙箱不可用时拒绝启动，不偷偷回退」直接冲突：
一个布尔开关不该有能力抹掉承诺。

现在的语义分两层，本文件把两层都钉住：

1. **配置改不动底线**：显式请求 `none`/`job` ⇒ Windows 上按 `FLOOR_LEVEL` 执行；
2. **缺机制也不放行**：实际档位落到 `FLOOR_LEVEL` 以下 ⇒ `below_floor=True`，
   `require_or_raise` 拒绝执行，且**不看** `allow_downgrade`。

第 2 条是这套设计的重点：开关放宽的是"稍弱的隔离"，不是"没有隔离"。
"""

from __future__ import annotations

import pytest

import app.isolation as iso
from app.isolation import (
    FLOOR_LEVEL,
    IsolationDecision,
    Level,
    require_or_raise,
    resolve_level,
)


@pytest.fixture
def windows_healthy(monkeypatch):
    """模拟一台机制齐备的 Windows 机器：Job 与 Low IL 均可用。"""
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    return iso


# ── 第一层：请求值低于底线 → 按底线执行，不报错 ──


@pytest.mark.parametrize("low", [Level.NONE, Level.JOB])
def test_low_request_is_raised_to_floor(windows_healthy, low):
    """显式请求 `none`/`job`，Windows 上实际生效值必须顶到 `FLOOR_LEVEL`。"""
    decision = resolve_level(low)

    assert decision.effective.rank >= FLOOR_LEVEL.rank, (
        f"请求 {low.value} 竟然真的生效为 {decision.effective.value} —— 底线被绕过了"
    )
    assert decision.reasons, "抬到底线必须在 reasons 里说明，否则是静默改写"
    assert not decision.below_floor


def test_floor_itself_is_accepted(windows_healthy):
    """请求正好等于底线 ⇒ 原样生效，且不该有 reasons。"""
    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective == FLOOR_LEVEL
    assert decision.reasons == []
    assert not decision.below_floor


def test_request_above_floor_still_honored(windows_healthy):
    """底线不能反过来削掉更高的请求（appcontainer 仍要尝试）。"""
    decision = resolve_level(Level.APPCONTAINER)

    # APPCONTAINER 需要 profile 授权，这里未 mock，允许降级到 low_il ——
    # 但要确认没有因为底线逻辑被错误地压到更低。
    assert decision.effective.rank >= FLOOR_LEVEL.rank


# ── 第二层：机制缺失 → below_floor，且开关放不过去 ──


def test_missing_job_object_marks_below_floor(windows_healthy, monkeypatch):
    """Job Object 不可用 ⇒ 退到 `none`，必须标记 below_floor。"""
    monkeypatch.setattr(windows_healthy, "_probe_job", lambda: False)

    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective is Level.NONE
    assert decision.below_floor, "退到 none 却没标记 below_floor —— fail-closed 会失效"


def test_missing_low_il_marks_below_floor(windows_healthy, monkeypatch):
    """Low IL 不可设置 ⇒ 退到 `job`，低于底线，同样标记。"""
    monkeypatch.setattr(windows_healthy, "_probe_low_il", lambda: False)

    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective is Level.JOB
    assert decision.below_floor


def test_below_floor_cannot_be_waived_by_downgrade_switch(windows_healthy, monkeypatch):
    """**核心断言**：allow_downgrade=true 也放不过 below_floor。

    这是本文件存在的理由。若这条变红，说明开关又变回了"能关掉隔离"。
    """
    monkeypatch.setattr(windows_healthy, "_probe_low_il", lambda: False)
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: True)

    decision = resolve_level(FLOOR_LEVEL)
    assert decision.below_floor

    with pytest.raises(RuntimeError) as exc:
        require_or_raise(decision)

    message = str(exc.value)
    assert "硬底线" in message, "拒绝信息必须点明是硬底线，而不是普通降级"
    assert "ALLOW_DOWNGRADE" in message, "要告诉用户那个开关为什么不管用"


# ── 普通降级仍可经开关放行（底线之上，不能一刀切） ──


def test_ordinary_degradation_still_needs_permission(windows_healthy, monkeypatch):
    """`appcontainer → low_il` 这类降级：默认拒绝。"""
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: False)

    decision = IsolationDecision(Level.APPCONTAINER, Level.LOW_IL, ["AppContainer 不可用"])

    with pytest.raises(RuntimeError) as exc:
        require_or_raise(decision)
    assert "ALLOW_DOWNGRADE" in str(exc.value)


def test_ordinary_degradation_allowed_when_permitted(windows_healthy, monkeypatch):
    """底线之上的降级，开关有效 —— 否则开发机无法使用。"""
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: True)

    decision = IsolationDecision(Level.APPCONTAINER, Level.LOW_IL, ["AppContainer 不可用"])

    require_or_raise(decision)  # 不抛


def test_non_windows_is_not_subject_to_floor(monkeypatch):
    """Linux 侧保持既有 local 实现，不受硬底线约束（用户明确要求）。"""
    monkeypatch.setattr(iso.win, "IS_WINDOWS", False)

    decision = resolve_level(Level.LOW_IL)

    assert decision.effective is Level.NONE
    assert not decision.below_floor, "非 Windows 不该触发 Windows 硬底线"


def test_status_reports_floor_and_selectable(monkeypatch):
    """前端据此渲染选项卡：低于底线的档位必须不可选。"""
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    monkeypatch.setattr(iso, "configured_level", lambda: FLOOR_LEVEL)

    payload = iso.status()

    assert payload["floor"] == FLOOR_LEVEL.value
    assert "below_floor" in payload
    selectable = payload["selectable"]
    assert Level.NONE.value not in selectable
    assert Level.JOB.value not in selectable
    assert FLOOR_LEVEL.value in selectable
