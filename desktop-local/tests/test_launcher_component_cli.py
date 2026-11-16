"""组件子命令的回归测试：``app.components`` 必须有生产入口。

回归背景：``components/`` 共 1791 行，此前只被 tests 引用，``launcher`` 从未
import 过它 —— 属于"有测试、无入口"的不可达代码。现在由 ``launcher`` 的
位置子命令接出来，这些用例守住那条路径不被改回去。
"""

from __future__ import annotations

import json

from app import launcher
from app.components import COMPONENT_NAMES


def _capture(monkeypatch, argv):
    """跑 ``launcher.main`` 并把 stdout 的单行 JSON 解出来。"""
    lines: list[str] = []
    monkeypatch.setattr(launcher, "_emit", lambda payload: lines.append(json.dumps(payload)))
    code = launcher.main(argv)
    assert len(lines) == 1, f"应当只输出一行 JSON，实际 {len(lines)} 行"
    return code, json.loads(lines[0])


def test_status_lists_pageindex_and_langextract(monkeypatch, tmp_path):
    """★ status 子命令列出的必须正是 pageindex / langextract 两个组件。"""
    code, payload = _capture(
        monkeypatch, ["--data-dir", str(tmp_path), "status"]
    )
    assert code == 0
    assert payload["event"] == "components"
    names = [item["name"] for item in payload["components"]]
    assert names == ["pageindex", "langextract"]
    assert tuple(names) == COMPONENT_NAMES
    for item in payload["components"]:
        assert item["installed"] is False
        assert item["running"] is False


def test_source_unconfigured_is_reported_not_swallowed(monkeypatch, tmp_path):
    """★ 没配 CHIRON_COMPONENT_BASE_URL 时 install 要明确报错，不能静默成功。

    ``launcher.main`` 顶层有兜底：异常会被转成 ``launcher-error`` 事件并返回 1，
    而不是抛给调用方（Electron 只能读到那行 JSON，抛栈它看不见）。
    """
    monkeypatch.delenv("CHIRON_COMPONENT_BASE_URL", raising=False)
    monkeypatch.delenv("CHIRON_COMPONENT_BASE_URL_LOCAL", raising=False)
    code, payload = _capture(
        monkeypatch, ["--data-dir", str(tmp_path), "install", "--name", "pageindex"]
    )
    assert code == 1
    assert payload["event"] == "launcher-error"
    assert "CHIRON_COMPONENT_BASE_URL" in payload["error"]


def test_install_without_name_explains_choices(monkeypatch, tmp_path):
    """缺 --name 时的报错要顺带告诉用户可选哪些组件。"""
    code, payload = _capture(monkeypatch, ["--data-dir", str(tmp_path), "install"])
    assert code == 1
    for name in COMPONENT_NAMES:
        assert name in payload["error"]


def test_unknown_component_is_rejected(monkeypatch, tmp_path):
    code, payload = _capture(
        monkeypatch, ["--data-dir", str(tmp_path), "install", "--name", "nope"]
    )
    assert code == 1
    assert "nope" in payload["error"]


def test_default_command_is_still_serve(monkeypatch):
    """★ 不传子命令时必须仍是原来的纯数据栈引导（Electron 契约不能断）。"""
    args = launcher._parse_args(["--data-dir", "x", "--no-pg"])
    assert args.command == "serve"
    assert args.no_pg is True
