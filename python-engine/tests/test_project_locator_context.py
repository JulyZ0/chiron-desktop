"""S0：project_locator 工具上下文的形状校验与传播。"""

from __future__ import annotations

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.agent.runtime import _project_locator_of
from app.tools.context import get_project_locator, set_tool_context


def test_project_locator_of_filters_shape():
    raw = {
        "id": " ws-1 ",
        "provider": "server",
        "device_id": "dev-1",
        "root": r" D:\Work\App ",
        "root_key": "d:/work/app",
        "ignored": 42,
    }
    got = _project_locator_of({"project_locator": raw})
    assert got == {
        "id": "ws-1",
        "provider": "server",
        "device_id": "dev-1",
        "root": r"D:\Work\App",
        "root_key": "d:/work/app",
    }


def test_project_locator_of_rejects_wrong_shapes():
    assert _project_locator_of(None) == {}
    assert _project_locator_of({}) == {}
    assert _project_locator_of({"project_locator": "ws-1"}) == {}
    # 非字符串字段被丢弃；全丢光就是空 dict（不是 {"id": ""}）
    assert _project_locator_of({"project_locator": {"id": 123, "root": None}}) == {}


def test_tool_context_get_project_locator_roundtrip():
    set_tool_context(project_locator={"id": "ws-1", "provider": "server"})
    assert get_project_locator() == {"id": "ws-1", "provider": "server"}

    # 合并语义：后续设置别的键不能把 locator 冲掉
    set_tool_context(session_id="s-1")
    assert get_project_locator() == {"id": "ws-1", "provider": "server"}


def test_get_project_locator_default_and_wrong_type():
    set_tool_context(project_locator="not-a-dict")
    assert get_project_locator() == {}
