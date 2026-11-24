"""H10 回归：审批票据绑定**规范化参数**，同等级改参数也必须重新授权。

PRD 9.2 的 H10 验收有三句，本文件逐句给一个独立探针（与 `test_approval_edit.py`
互为交叉验证 —— 那边钉的是 C5 编辑流程的细节，这里只钉 H10 的不变量）：

1. **票据绑定工具名、调用者、资源范围与规范化参数摘要**；
2. **任何参数变化都重新授权**（等级不变也算）；
3. **禁止仅凭风险等级不变复用批准**（另一张同等级的票不能顶替，用过的票不能再用）。

为什么值得单独一个文件：这三条是"批准的就是执行的"的**唯一**凭据。它们坏掉的方式都很安静
—— 用户点过一次批准，之后的同等级调用就悄悄跟着执行，界面上看不出任何异常。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agent.runtime import (
    DECISION_EDIT,
    AgentRuntime,
    ApprovalDecision,
    ApprovalTicket,
    _approval_caller_id,
    _approval_resource_scope,
    _approval_ticket_for,
)
from app.agent.tool_policy import args_hash


class _TicketStore:
    """票据存储替身：只实现 `set` / `get` / `delete`。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def set(self, key: Any, value: Any, ex: int | None = None) -> bool:
        self.store[str(key)] = value
        return True

    async def get(self, key: Any) -> Any:
        return self.store.get(str(key))

    async def delete(self, *keys: Any) -> int:
        return sum(1 for key in keys if self.store.pop(str(key), None) is not None)


@pytest.fixture
def ticket_store(monkeypatch: pytest.MonkeyPatch) -> _TicketStore:
    import app.redis_client as redis_client

    store = _TicketStore()

    async def _get_redis() -> _TicketStore:
        return store

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)
    return store


@pytest.fixture(autouse=True)
def broker(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    authorize = AsyncMock(
        return_value={
            "allowed": True,
            "level": "delete",
            "requires_user_approval": True,
            "requires_second_check": True,
        }
    )
    monkeypatch.setattr("app.tools.broker.authorize", authorize)
    return authorize


@pytest.fixture(autouse=True)
def audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.agent import approval_audit

    monkeypatch.setattr(approval_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _task(
    *,
    user_id: str = "user-1",
    tenant_id: int = 7,
    session_id: str = "session-1",
    project: str = "project-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="turn_1",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        workbench_context={
            "project": project,
            "root_path": "workspace",
            "project_locator": {"id": project, "provider": "local", "root": "workspace"},
        },
    )


def _call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _runtime() -> AgentRuntime:
    """最小 runtime（与 `_await_approval` 用到的字段对齐，避免绕过 `__init__` 后缺属性）。"""
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._pending_approvals = {}  # noqa: SLF001
    runtime._reset_interaction_state()  # noqa: SLF001
    runtime._current_mode = "ask"  # noqa: SLF001
    return runtime


def _capture_execution(monkeypatch: pytest.MonkeyPatch, executed: list[dict[str, Any]]) -> None:
    async def _fake(
        self: AgentRuntime,
        tool_call: dict[Any, Any],
        task: Any,
        *,
        approved: bool = False,
    ) -> tuple[dict[Any, Any], str | None]:
        executed.append(json.loads(tool_call["arguments"]))
        return {"output": "ok"}, None

    monkeypatch.setattr(AgentRuntime, "_execute_tool", _fake)


async def _approved_call(
    runtime: AgentRuntime,
    call: dict[str, Any],
    task: SimpleNamespace,
    decision: ApprovalDecision,
) -> dict[str, Any]:
    """模拟"审批已发起（票据已落）+ 决策已到"，再走真实的 `_await_approval`。"""
    tc_id = str(call["id"])
    runtime._pending_approvals[tc_id] = asyncio.get_running_loop().create_future()
    await runtime._store_approval_ticket(
        _approval_ticket_for(call, task, json.loads(call["arguments"]))
    )
    runtime._pending_approvals[tc_id].set_result(decision)
    return await runtime._await_approval(call, task, timeout=1)


# ── 1. 票据绑定四个维度 ────────────────────────────────────────────────


def test_ticket_binds_tool_caller_scope_and_normalized_args_digest():
    base = _call("delete_file", {"path": "a.txt"})
    task = _task()
    ticket = _approval_ticket_for(base, task, {"path": "a.txt"})

    assert ticket.tool_name == "delete_file"
    assert ticket.caller_id == _approval_caller_id(task)
    assert ticket.resource_scope == _approval_resource_scope(task, "delete_file", {"path": "a.txt"})
    assert ticket.args_hash == args_hash("delete_file", {"path": "a.txt"})

    # 调用者换人 → 票据不再属于他
    other_caller = _approval_ticket_for(base, _task(user_id="user-2"), {"path": "a.txt"})
    assert other_caller.caller_id != ticket.caller_id
    assert other_caller.resource_scope == ticket.resource_scope

    # 会话/项目换界 → 资源范围必须随之改变
    other_scope = _approval_ticket_for(base, _task(session_id="session-2"), {"path": "a.txt"})
    assert other_scope.resource_scope != ticket.resource_scope
    assert other_scope.caller_id == ticket.caller_id

    # 规范化：路径写法不同（`./a.txt` vs `a.txt`）是同一个参数摘要，不是两张票
    normalized = _approval_ticket_for(base, task, {"path": "./a.txt"})
    assert normalized.args_hash == ticket.args_hash


@pytest.mark.parametrize(
    "field",
    ["tool_call_id", "tool_name", "args_hash", "turn_id", "caller_id", "resource_scope"],
)
def test_ticket_losing_any_binding_field_is_unusable(field: str):
    call = _call("delete_file", {"path": "a.txt"})
    payload = json.loads(_approval_ticket_for(call, _task(), {"path": "a.txt"}).to_json())
    payload.pop(field)
    assert ApprovalTicket.from_json(json.dumps(payload)) is None, (
        f"缺 {field} 的票据必须按损坏处理（fail-closed），不能因为工具名/参数相同就被复用"
    )


async def test_ticket_from_another_caller_cannot_authorize_execution(ticket_store: _TicketStore):
    runtime = _runtime()
    call = _call("delete_file", {"path": "a.txt"})
    await runtime._store_approval_ticket(_approval_ticket_for(call, _task(), {"path": "a.txt"}))

    failure = await runtime._second_check_approval(call, _task(user_id="user-2"))

    assert "caller mismatch" in failure


# ── 2. 任何参数变化都重新授权（等级不变也算）────────────────────────────


async def test_same_level_edit_requires_fresh_broker_authorization(
    monkeypatch: pytest.MonkeyPatch,
    ticket_store: _TicketStore,
    broker: AsyncMock,
):
    """delete a.txt → delete b.txt：等级仍是 delete，但参数变了 ⇒ 必须重新问 Broker。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call("delete_file", {"path": "a.txt"})

    result = await _approved_call(
        runtime, call, task, ApprovalDecision(decision=DECISION_EDIT, arguments={"path": "b.txt"})
    )

    assert result.get("error") is None
    assert executed == [{"path": "b.txt"}], "执行的必须是**编辑后**的参数"
    broker.assert_awaited_once()
    assert broker.await_args.args == ("delete_file", {"path": "b.txt"})


@pytest.mark.parametrize(
    "broker_result",
    [{"allowed": False, "reason": "target outside approved scope"}, RuntimeError("broker down")],
)
async def test_same_level_edit_without_fresh_authorization_is_not_executed(
    monkeypatch: pytest.MonkeyPatch,
    ticket_store: _TicketStore,
    broker: AsyncMock,
    broker_result: object,
):
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call("delete_file", {"path": "a.txt"})
    if isinstance(broker_result, Exception):
        broker.side_effect = broker_result
    else:
        broker.return_value = broker_result

    result = await _approved_call(
        runtime, call, task, ApprovalDecision(decision=DECISION_EDIT, arguments={"path": "b.txt"})
    )

    assert executed == [], "拿不到新参数的授权 ⇒ 不执行（不能拿旧的批准顶上）"
    assert "fresh Broker authorization" in str(result.get("error", ""))


# ── 3. 禁止仅凭"风险等级不变"复用批准 ──────────────────────────────────


async def test_another_call_at_the_same_level_cannot_reuse_the_ticket(
    ticket_store: _TicketStore,
):
    """call_2 与已批准的 call_1 同级别、同参数：没有它自己的票就过不了二次校验。"""
    runtime = _runtime()
    task = _task()
    first = _call("delete_file", {"path": "a.txt"}, call_id="call_1")
    second = _call("delete_file", {"path": "a.txt"}, call_id="call_2")
    await runtime._store_approval_ticket(_approval_ticket_for(first, task, {"path": "a.txt"}))

    failure = await runtime._second_check_approval(second, task)

    assert "no approval ticket" in failure


async def test_ticket_is_single_use(ticket_store: _TicketStore):
    runtime = _runtime()
    task = _task()
    call = _call("delete_file", {"path": "a.txt"})
    await runtime._store_approval_ticket(_approval_ticket_for(call, task, {"path": "a.txt"}))

    assert await runtime._second_check_approval(call, task) == ""
    assert "no approval ticket" in await runtime._second_check_approval(call, task)


async def test_same_level_different_target_is_not_covered_by_the_ticket(
    ticket_store: _TicketStore,
):
    runtime = _runtime()
    task = _task()
    approved = _call("delete_file", {"path": "a.txt"})
    await runtime._store_approval_ticket(_approval_ticket_for(approved, task, {"path": "a.txt"}))

    drifted = _call("delete_file", {"path": "b.txt"})
    failure = await runtime._second_check_approval(drifted, task)

    assert "args mismatch" in failure
