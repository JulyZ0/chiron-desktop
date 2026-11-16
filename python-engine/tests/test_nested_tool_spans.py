"""G-T4：`run_code` 里嵌套的工具调用要挂到**发起它的那次调用**下面。

改动前这些子调用只发生在子进程里 —— 主循环既不为它记 span，也不把它挂到任何
节点下，于是执行图上"一次 run_code 里跑了 20 个工具"看起来像什么都没做。

本文件锁住四件事：

1. 父身份经工具上下文传递（`registry.execute` 的 `(name, params)` 签名没位置放它），
   子节点的 `parent_span_id` 与父 `tool_call_id` 都能算出来；
2. 多层嵌套（`run_code` 里再 `run_code`）的父指针指向**内层**，不串味 —— 靠的是
   内层捕获快照时正好处在外层子调用的作用域里；
3. 没有 trace 语境时**整条记账关闭**（不凭空造 trace，否则只会产出孤儿节点）；
4. 记账失败**绝不**影响工具结果（观测面出问题不能让业务失败）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.runtime import AgentRuntime
from app.tools import run_code as rc_mod
from app.tools.context import (
    get_all,
    get_tool_call_id,
    get_tool_span_id,
    restore_context,
    set_tool_context,
    tool_call_scope,
)
from app.tools.registry import registry

_PARENT_CALL = "call_parent"
_PARENT_SPAN = "tr1:turn:2:tool:call_parent"


@pytest.fixture(autouse=True)
def _isolate_context():
    """每个用例后还原工具上下文 —— `_safe_tool_call` 会 restore 整份快照。"""
    before = get_all()
    try:
        yield
    finally:
        restore_context(before)


@pytest.fixture
def echo_tool():
    """注册一个可成功、可失败的回显工具；用例结束即注销（registry 是进程内单例）。"""

    async def handler(value: str = "", fail: bool = False) -> dict[str, Any]:
        if fail:
            raise RuntimeError("boom")
        return {"echoed": value}

    name = "_gt4_echo"
    registry.register(
        name=name,
        description="G-T4 test echo tool",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "fail": {"type": "boolean"},
            },
        },
        handler=handler,
    )
    try:
        yield name
    finally:
        registry.unregister(name)


def _parent_snapshot(**extra: Any) -> dict[str, Any]:
    """模拟 `_run_in_subprocess` 在主 loop 里捕获到的那份上下文。"""
    snapshot: dict[str, Any] = {
        "trace_id": "tr1",
        "span_id": "tr1:turn:2",
        "tenant_id": "7",
        "tool_call_id": _PARENT_CALL,
        "tool_span_id": _PARENT_SPAN,
    }
    snapshot.update(extra)
    return snapshot


def _capture_spans(monkeypatch) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []

    async def _fake_record_span(**kwargs: Any) -> None:
        spans.append(kwargs)

    monkeypatch.setattr("app.trace.record_span", _fake_record_span, raising=False)
    return spans


class TestToolCallScope:
    """身份压栈是精确恢复：退出一层就回到上一层，而不是回到"空"。"""

    def test_nested_scopes_restore_exactly(self):
        set_tool_context(trace_id="tr1")
        assert get_tool_call_id() == ""

        with tool_call_scope("a", span_id="tr1:turn:1:tool:a"):
            assert get_tool_call_id() == "a"
            assert get_tool_span_id() == "tr1:turn:1:tool:a"

            with tool_call_scope("b", span_id="tr1:turn:1:tool:a:call:b"):
                assert get_tool_call_id() == "b"
                assert get_tool_span_id() == "tr1:turn:1:tool:a:call:b"

            # 内层退出只撤销自己那一次 set —— 外层仍然完整。
            assert get_tool_call_id() == "a"
            assert get_tool_span_id() == "tr1:turn:1:tool:a"

        assert get_tool_call_id() == ""
        assert get_tool_span_id() == ""

    def test_scope_keeps_unrelated_keys(self):
        """作用域只动自己那两个键，其余上下文（session/tenant）原样带下去。"""
        set_tool_context(session_id="s1", tenant_id="7")
        with tool_call_scope("a"):
            assert get_all()["session_id"] == "s1"
            assert get_all()["tenant_id"] == "7"


class TestNestedSpanRecording:
    @pytest.mark.asyncio
    async def test_child_span_hangs_under_parent_call(self, echo_tool, monkeypatch):
        spans = _capture_spans(monkeypatch)

        result = await rc_mod._safe_tool_call(echo_tool, {"value": "hi"}, _parent_snapshot(), "sub_1")

        assert result == {"echoed": "hi"}
        assert len(spans) == 1
        span = spans[0]
        assert span["trace_id"] == "tr1"
        # 子节点挂在父工具节点下，父子可以互相定位。
        assert span["parent_span_id"] == _PARENT_SPAN
        assert span["span_id"] == f"{_PARENT_SPAN}:call:sub_1"
        assert span["kind"] == "tool"
        assert span["tenant_id"] == 7
        metadata = span["metadata"]
        assert metadata["tool_name"] == echo_tool
        # 这一格是 G-T4 的要点：子调用自己就能回答"我是谁发起的"。
        assert metadata["parent_tool_call_id"] == _PARENT_CALL
        assert metadata["nested"] is True
        assert metadata["via"] == "run_code"
        assert metadata["success"] is True

    @pytest.mark.asyncio
    async def test_tool_error_marks_span_unsuccessful(self, echo_tool, monkeypatch):
        spans = _capture_spans(monkeypatch)

        result = await rc_mod._safe_tool_call(echo_tool, {"fail": True}, _parent_snapshot(), "sub_2")

        # registry 把 handler 异常转成结构化 error，调用方照旧拿到结果。
        assert "boom" in str(result.get("error"))
        assert spans[0]["metadata"]["success"] is False

    @pytest.mark.asyncio
    async def test_unknown_tool_is_not_recorded_as_success(self, monkeypatch):
        """工具不存在时 registry 返回 error —— 记账也要如实反映失败。"""
        spans = _capture_spans(monkeypatch)

        result = await rc_mod._safe_tool_call("_gt4_no_such_tool", {}, _parent_snapshot(), "sub_3")

        assert result.get("error")
        assert spans[0]["metadata"]["success"] is False

    @pytest.mark.asyncio
    async def test_deeper_nesting_points_at_inner_call(self, echo_tool, monkeypatch):
        """`run_code` 里再 `run_code`：子调用的父指针指向**内层**那次调用。

        内层 `run_code` 在子进程里捕获快照时，正处在外层子调用的作用域中 ——
        所以它的 `tool_call_id` 是外层那次子调用，而不是最初的父调用。
        """
        spans = _capture_spans(monkeypatch)
        outer_child_span = f"{_PARENT_SPAN}:call:run_code_a"

        # 内层 run_code 的语境 = 外层子调用作用域里的完整上下文，
        # 于是它的 `tool_call_id` 是外层那次子调用，而不是最初的父调用。
        set_tool_context(trace_id="tr1", span_id="tr1:turn:2", tenant_id="7")
        with tool_call_scope("run_code_a", span_id=outer_child_span):
            # 内层 run_code 走到 `_run_in_subprocess` 时捕获的就是这一份。
            inner_snapshot = get_all()

        await rc_mod._safe_tool_call(echo_tool, {"value": "deep"}, inner_snapshot, "deep_1")

        span = spans[0]
        assert span["metadata"]["parent_tool_call_id"] == "run_code_a"
        assert span["parent_span_id"] == outer_child_span
        assert span["span_id"] == f"{outer_child_span}:call:deep_1"


class TestRecordingGuards:
    @pytest.mark.asyncio
    async def test_no_trace_context_records_nothing(self, echo_tool, monkeypatch):
        """主进程没有 trace 语境（如直连 API 调用）时，不造孤儿节点。"""
        spans = _capture_spans(monkeypatch)

        result = await rc_mod._safe_tool_call(echo_tool, {"value": "x"}, {}, "sub_4")

        assert result == {"echoed": "x"}
        assert spans == []

    @pytest.mark.asyncio
    async def test_missing_call_id_records_nothing(self, echo_tool, monkeypatch):
        """子进程没给 `call_id` 时算不出节点 id —— 宁可不记，也不写一个重复的 id。"""
        spans = _capture_spans(monkeypatch)

        await rc_mod._safe_tool_call(echo_tool, {"value": "x"}, _parent_snapshot(), "")

        assert spans == []

    @pytest.mark.asyncio
    async def test_record_failure_does_not_break_the_call(self, echo_tool, monkeypatch):
        """Redis 挂了也照常返回工具结果 —— 观测面不能拖垮业务。"""

        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("redis down")

        monkeypatch.setattr("app.trace.record_span", _boom, raising=False)

        result = await rc_mod._safe_tool_call(echo_tool, {"value": "ok"}, _parent_snapshot(), "sub_5")

        assert result == {"echoed": "ok"}


class TestDispatchPushesIdentity:
    @pytest.mark.asyncio
    async def test_dispatch_tool_exposes_call_identity_to_handler(self):
        """`_dispatch_tool` 压入的身份，工具内（以及其下的嵌套调用）都能读到。"""
        seen: dict[str, str] = {}

        async def handler() -> dict[str, Any]:
            seen["call_id"] = get_tool_call_id()
            seen["span_id"] = get_tool_span_id()
            return {"ok": True}

        name = "_gt4_identity"
        registry.register(
            name=name,
            description="G-T4 identity probe",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        try:
            set_tool_context(trace_id="tr1", span_id="tr1:turn:1")
            runtime = AgentRuntime(gateway=None)

            # task 只被非本地工具分支用到；本地注册表命中即返回。
            await runtime._dispatch_tool(
                {"id": "c1", "name": name, "arguments": "{}"},
                None,  # type: ignore[arg-type]
            )
        finally:
            registry.unregister(name)

        # 节点 id 与主循环 `record_span` 用的那个逐字相同。
        assert seen == {"call_id": "c1", "span_id": "tr1:turn:1:tool:c1"}

    @pytest.mark.asyncio
    async def test_identity_is_popped_after_dispatch(self):
        """派发结束即出栈：工具返回后，后续代码不应还以为自己是那次调用。"""

        async def handler() -> dict[str, Any]:
            return {}

        name = "_gt4_pop"
        registry.register(
            name=name,
            description="G-T4 pop probe",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        try:
            set_tool_context(trace_id="tr1", span_id="tr1:turn:1")
            runtime = AgentRuntime(gateway=None)
            await runtime._dispatch_tool(
                {"id": "c9", "name": name, "arguments": "{}"},
                None,  # type: ignore[arg-type]
            )
        finally:
            registry.unregister(name)

        assert get_tool_call_id() == ""
