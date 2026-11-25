"""H24（Python 侧）回归：未授权不得执行、两条闸门都要过。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.plugins.store import PluginStore


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginStore:
    monkeypatch.setenv("PLUGIN_DATA_DIR", str(tmp_path))
    return PluginStore()


def _write(store: PluginStore, user: str, payload: dict) -> None:
    path = store._path(user)  # noqa: SLF001 - 测试直接写配置文件
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _server(name: str, *, approved: bool, status: str = "active") -> dict:
    return {
        "name": name,
        "command": "node",
        "args": ["server.js"],
        "status": status,
        "approved": approved,
    }


def test_unapproved_active_server_is_not_executable(store: PluginStore) -> None:
    """`active` 但未授权 ⇒ 不得交给池子（否则引擎会为它起进程/连网）。"""
    _write(
        store,
        "u1",
        {"commands_enabled": True, "mcp_servers": [_server("demo", approved=False)]},
    )
    assert store.active_servers("u1") == []


def test_commands_switch_off_blocks_everything(store: PluginStore) -> None:
    """总开关关闭 ⇒ 即使插件已授权也不执行。"""
    _write(
        store,
        "u1",
        {"commands_enabled": False, "mcp_servers": [_server("demo", approved=True)]},
    )
    assert store.commands_enabled("u1") is False
    assert store.active_servers("u1") == []


def test_approved_and_enabled_is_executable(store: PluginStore) -> None:
    _write(
        store,
        "u1",
        {"commands_enabled": True, "mcp_servers": [_server("demo", approved=True)]},
    )
    got = store.active_servers("u1")
    assert [s.name for s in got] == ["demo"]
    assert got[0].approved is True


def test_disabled_status_is_not_executable(store: PluginStore) -> None:
    _write(
        store,
        "u1",
        {
            "commands_enabled": True,
            "mcp_servers": [_server("demo", approved=True, status="disabled")],
        },
    )
    assert store.active_servers("u1") == []


def test_missing_approved_field_is_treated_as_unapproved(store: PluginStore) -> None:
    """老配置没有 `approved` 字段 ⇒ 按未授权处理（默认关闭，不是默认放行）。"""
    _write(
        store,
        "u1",
        {
            "commands_enabled": True,
            "mcp_servers": [{"name": "demo", "command": "node", "status": "active"}],
        },
    )
    assert store.active_servers("u1") == []


def test_missing_commands_switch_is_treated_as_off(store: PluginStore) -> None:
    _write(store, "u1", {"mcp_servers": [_server("demo", approved=True)]})
    assert store.commands_enabled("u1") is False
    assert store.active_servers("u1") == []


def test_corrupt_file_fails_closed(store: PluginStore) -> None:
    path = store._path("u1")  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.commands_enabled("u1") is False
    assert store.active_servers("u1") == []


def test_mixed_servers_only_returns_authorized(store: PluginStore) -> None:
    _write(
        store,
        "u1",
        {
            "commands_enabled": True,
            "mcp_servers": [
                _server("ok-one", approved=True),
                _server("no-approval", approved=False),
                _server("off", approved=True, status="disabled"),
            ],
        },
    )
    assert [s.name for s in store.active_servers("u1")] == ["ok-one"]
