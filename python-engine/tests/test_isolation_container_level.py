"""`container` 隔离挡位的档位语义（A/H2）。

判据：
* `parse_level("container")` 可用，且强度排在最后（比 Low IL 更强）；
* 依赖缺失 ⇒ **effective=NONE + below_floor=True**（启动门禁/执行侧拒跑），
  不是静默降级到无命名空间的本地执行；
* Windows 上明确不可用（命名管道驱动未实现），原因可读；
* 非 Windows 上，只有 `none` 与 `container` 是可执行档位（Windows 原生机不适用）。
"""

from __future__ import annotations

import pytest

from app.isolation import (
    IsolationDecision,
    Level,
    _level_executable,
    _level_unavailable_reason,
    parse_level,
    resolve_level,
)


def test_parse_level_accepts_container():
    assert parse_level("container") is Level.CONTAINER
    assert Level.CONTAINER.rank > Level.LOW_IL.rank


def test_non_windows_container_available(monkeypatch):
    from app.isolation import kiri, win

    monkeypatch.setattr(win, "IS_WINDOWS", False)
    monkeypatch.setattr(kiri, "available", lambda: True)
    decision = resolve_level(Level.CONTAINER)
    assert decision.requested is Level.CONTAINER
    assert decision.effective is Level.CONTAINER
    assert decision.below_floor is False


def test_non_windows_container_missing_refuses(monkeypatch):
    from app.isolation import kiri, win

    monkeypatch.setattr(win, "IS_WINDOWS", False)
    monkeypatch.setattr(kiri, "available", lambda: False)
    monkeypatch.setattr(kiri, "unavailable_reason", lambda: "未检测到 Kiri")
    decision = resolve_level(Level.CONTAINER)
    assert decision.requested is Level.CONTAINER
    assert decision.effective is Level.NONE
    assert decision.below_floor is True, "依赖缺失必须触发 fail-closed，而不是本地执行"
    assert decision.degraded is True
    assert any("Kiri" in reason for reason in decision.reasons)


def test_windows_container_is_unavailable(monkeypatch):
    from app.isolation import win

    monkeypatch.setattr(win, "IS_WINDOWS", True)
    decision = resolve_level(Level.CONTAINER)
    assert decision.effective is Level.NONE
    assert decision.below_floor is True
    assert any("命名管道" in reason for reason in decision.reasons)


def test_level_executable_only_none_and_container_off_windows(monkeypatch):
    from app.isolation import kiri, win

    monkeypatch.setattr(win, "IS_WINDOWS", False)
    monkeypatch.setattr(kiri, "available", lambda: True)
    assert _level_executable(Level.NONE) is True
    assert _level_executable(Level.CONTAINER) is True
    assert _level_executable(Level.LOW_IL) is False


def test_container_unavailable_reason_is_actionable(monkeypatch):
    from app.isolation import kiri, win

    monkeypatch.setattr(win, "IS_WINDOWS", False)
    monkeypatch.setattr(kiri, "unavailable_reason", lambda: "未检测到 Kiri。可在设置页下载运行时包")
    assert "下载" in _level_unavailable_reason(Level.CONTAINER)


def test_decision_describe_keeps_both_sides():
    decision = IsolationDecision(Level.CONTAINER, Level.NONE, ["依赖缺失"], below_floor=True)
    text = decision.describe()
    assert "container" in text and "none" in text and "依赖缺失" in text


@pytest.mark.parametrize("level", [Level.JOB, Level.LOW_IL])
def test_windows_native_levels_are_not_executable_off_windows(monkeypatch, level):
    from app.isolation import win

    monkeypatch.setattr(win, "IS_WINDOWS", False)
    assert _level_executable(level) is False
