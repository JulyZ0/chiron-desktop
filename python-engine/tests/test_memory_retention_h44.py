"""H44：no_retention 全链路（历史 + 记忆）不落盘的回归。"""

from __future__ import annotations

import pytest

from app.memory import retention_guard
from app.middleware.privacy_middleware import NO_RETENTION, privacy_mode_var


def test_default_allows_memory_writes() -> None:
    """没有请求上下文（脚本/后台任务/单测）⇒ 正常模式，允许写。"""
    assert retention_guard.memory_writes_allowed() is True


def test_no_retention_blocks_memory_writes() -> None:
    token = privacy_mode_var.set(NO_RETENTION)
    try:
        assert retention_guard.memory_writes_allowed() is False
    finally:
        privacy_mode_var.reset(token)


def test_other_privacy_modes_still_allow() -> None:
    token = privacy_mode_var.set("strict")
    try:
        assert retention_guard.memory_writes_allowed() is True
    finally:
        privacy_mode_var.reset(token)


@pytest.mark.parametrize(
    "path",
    [
        "app/memory/profile.py",
        "app/memory/summary_store.py",
        "app/memory/profile_card.py",
    ],
)
def test_memory_write_entrypoints_check_the_guard(path: str) -> None:
    """三个写入入口都必须调用闸门（新加写路径时也要接上）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert "memory_writes_allowed" in src, f"{path} 未接 no_retention 闸门"


def test_history_writer_and_memory_share_the_same_source() -> None:
    """历史写入与记忆写入必须查同一个判据（否则两条链路口径会漂）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    history = (root / "app/agent/history_writer.py").read_text(encoding="utf-8")
    guard = (root / "app/memory/retention_guard.py").read_text(encoding="utf-8")
    assert "is_no_retention" in history
    assert "is_no_retention" in guard, "闸门必须复用隐私中间件的判定，而不是自己再读一次头"
