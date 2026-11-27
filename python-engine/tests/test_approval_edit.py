"""C5：审批 `edit` 语义。

对齐四条验收：
① `edit` 后执行的是**编辑后的参数**；
② `edit` 成**更高危**动作时**不直接执行**（防"用 edit 绕过分级"）；
③ 审计里**同时**可见原始与编辑后参数；
④ 旧版前端（只发 `approved: bool`）行为不变。

另覆盖协议解析（`parse_approval_payload`）与跨副本决策载荷的**新旧格式兼容**
（`ApprovalDecision.from_raw`）—— 它们是"决策能如实送达"的前提。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agent.runtime import (
    DECISION_APPROVE,
    DECISION_EDIT,
    DECISION_REJECT,
    AgentRuntime,
    ApprovalDecision,
    ApprovalTicket,
    _approval_ticket_for,
    parse_approval_payload,
)
from app.agent.tool_policy import args_hash, tool_level

# ── 协议解析（`/v1/agent/approval` 的两个端点共用） ──────────────────────


def test_legacy_approved_true_maps_to_approve():
    """④ 旧形态：只发 `approved: bool`。"""
    decision = parse_approval_payload(approved=True)
    assert isinstance(decision, ApprovalDecision)
    assert decision.decision == DECISION_APPROVE
    assert decision.approved is True


def test_legacy_approved_false_maps_to_reject():
    decision = parse_approval_payload(approved=False)
    assert isinstance(decision, ApprovalDecision)
    assert decision.decision == DECISION_REJECT
    assert decision.approved is False


def test_missing_both_is_an_error_not_a_default_approval():
    """两者都缺 → 报错。**默认批准**会让"漏发字段"变成放行。"""
    result = parse_approval_payload(approved=None)
    assert isinstance(result, str)


def test_new_decision_field():
    assert parse_approval_payload(decision="approve").decision == DECISION_APPROVE  # type: ignore[union-attr]
    assert parse_approval_payload(decision="REJECT").decision == DECISION_REJECT  # type: ignore[union-attr]


def test_unknown_decision_is_rejected():
    result = parse_approval_payload(decision="maybe")
    assert isinstance(result, str) and "unknown decision" in result


def test_edit_requires_json_object_arguments():
    ok = parse_approval_payload(decision="edit", arguments='{"cmd": "ls -la"}')
    assert isinstance(ok, ApprovalDecision)
    assert ok.arguments == {"cmd": "ls -la"}

    for bad in ("", "not json", "[1,2]", '"text"'):
        result = parse_approval_payload(decision="edit", arguments=bad)
        assert isinstance(result, str), f"{bad!r} 应被拒绝"


def test_edit_accepts_already_parsed_dict():
    """端点可能直接传 dict（main.py 的 body 就是 JSON 解出来的）。"""
    ok = parse_approval_payload(decision="edit", arguments={"cmd": "ls"})
    assert isinstance(ok, ApprovalDecision) and ok.arguments == {"cmd": "ls"}


# ── 跨副本决策载荷：新旧格式兼容 ───────────────────────────────────────


def test_decision_payload_roundtrip():
    original = ApprovalDecision(decision=DECISION_EDIT, arguments={"a": 1}, reason="改窄一点")
    parsed = ApprovalDecision.from_raw(original.to_json())
    assert parsed is not None
    assert parsed.decision == DECISION_EDIT
    assert parsed.arguments == {"a": 1}
    assert parsed.reason == "改窄一点"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", DECISION_APPROVE), (b"0", DECISION_REJECT), ("true", DECISION_APPROVE), ("false", DECISION_REJECT)],
)
def test_legacy_redis_values_still_parse(raw: Any, expected: str):
    """升级期间 Redis 里仍有旧的 `"1"`/`"0"` 决策键 —— 必须能读出来，否则审批被吞掉。"""
    parsed = ApprovalDecision.from_raw(raw)
    assert parsed is not None and parsed.decision == expected


def test_edit_counts_as_approved():
    assert ApprovalDecision(decision=DECISION_EDIT, arguments={}).approved is True


# ── `_await_approval` 的执行语义 ─────────────────────────────────────────


class _FakeRedis:
    """只实现票据读写用到的三个命令。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    async def set(self, key: Any, value: Any, ex: int | None = None) -> bool:
        self.store[str(key)] = value
        return True

    async def get(self, key: Any) -> Any:
        return self.store.get(str(key))

    async def delete(self, *keys: Any) -> int:
        return sum(1 for k in keys if self.store.pop(str(k), None) is not None)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    import app.redis_client as redis_client

    redis = _FakeRedis()

    async def _get_redis() -> _FakeRedis:
        return redis

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)
    return redis


@pytest.fixture(autouse=True)
def broker_authorize(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """H10：edit 的参数变化必须重新经过 Broker；默认替身允许，供语义测试断言调用。"""
    authorize = AsyncMock(
        return_value={
            "allowed": True,
            "level": "write",
            "requires_user_approval": True,
            "requires_second_check": False,
        }
    )
    monkeypatch.setattr("app.tools.broker.authorize", authorize)
    return authorize


@pytest.fixture(autouse=True)
def _audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """审计落到 tmp，避免污染仓库 logs/；同时便于断言其内容。"""
    from app.agent import approval_audit

    monkeypatch.setattr(approval_audit, "AUDIT_DIR", tmp_path)
    return tmp_path


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id="turn_1",
        tenant_id=7,
        user_id="user-1",
        session_id="session-1",
        workbench_context={
            "project": "project-1",
            "root_path": "workspace",
            "project_locator": {"id": "project-1", "provider": "local", "root": "workspace"},
        },
    )


def _call(arguments: dict[str, Any], name: str = "shell_exec") -> dict[str, Any]:
    return {"id": "call_1", "name": name, "arguments": json.dumps(arguments)}


def _runtime() -> AgentRuntime:
    """最小 runtime：`_await_approval` 只用到 `_pending_approvals` 与若干方法。

    绕过 `__init__` 就不再有 M7 的介入簿记（`_grants` / `_interactions` / `_awaiting_input`），
    而 `_await_approval` 现在会读它们 —— 用 `_reset_interaction_state()` 补上，将来加字段
    也不必回来改这里。
    """
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._pending_approvals = {}  # noqa: SLF001
    runtime._reset_interaction_state()  # noqa: SLF001
    runtime._current_mode = "ask"  # noqa: SLF001
    return runtime


async def _prepare(
    runtime: AgentRuntime,
    call: dict[str, Any],
    decision: ApprovalDecision,
    redis: _FakeRedis,
    task: SimpleNamespace | None = None,
) -> None:
    """模拟"审批已发起"（票据在 Redis、future 待唤醒），再投递决策。"""
    import asyncio

    task = task or _task()
    tc_id = call["id"]
    runtime._pending_approvals[tc_id] = asyncio.get_running_loop().create_future()
    await runtime._store_approval_ticket(
        _approval_ticket_for(call, task, json.loads(call["arguments"]))
    )
    runtime._pending_approvals[tc_id].set_result(decision)


def _capture_execution(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    """替换 `_execute_tool` 并记录它收到的 tool_call。

    注意：类属性经实例访问会绑定 `self`，所以替身**必须**带 `self` 参数
    （否则 TypeError 会被上层吞成"工具执行失败"，表现为"看起来执行了但结果全是错"）。
    """

    async def _fake(
        self: AgentRuntime,
        tool_call: dict[Any, Any],
        task: Any,
        *,
        approved: bool = False,
    ) -> tuple[dict[Any, Any], str | None]:
        calls.append(json.loads(tool_call["arguments"]))
        # 与真实契约一致：`(结果, 待确认原因)`；这里模拟"PreToolUse hook 没有表态"。
        return {"output": "ok"}, None

    monkeypatch.setattr(AgentRuntime, "_execute_tool", _fake)


def _audit_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "approval_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]


async def test_edit_executes_the_edited_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """① 批准的是**编辑后的那一次调用**（整份替换，不是补丁）。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime, call, ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}), fake_redis
    )

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert result.get("error") is None
    assert executed == [{"cmd": "ls -la"}], "执行的必须是编辑后的参数"


async def test_plain_approve_still_executes_original_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """④ 旧语义不变：只批准时执行原始参数。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(runtime, call, ApprovalDecision(decision=DECISION_APPROVE), fake_redis)

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert result.get("error") is None
    assert executed == [{"cmd": "ls"}]


async def test_reject_does_not_execute(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(runtime, call, ApprovalDecision(decision=DECISION_REJECT), fake_redis)

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert "denied" in result.get("error", "")
    assert executed == []


async def test_edit_escalation_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """② 编辑把动作升级（write → delete）⇒ **不直接执行**。"""
    # 前置事实：这一对参数确实构成"级别升高"（否则本用例测试的就不是它了）
    assert tool_level("shell_exec", {"cmd": "ls"}) == "write"
    assert tool_level("shell_exec", {"cmd": "rm -rf /"}) == "delete"

    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "rm -rf /"}),
        fake_redis,
    )

    result = await runtime._await_approval(call, _task(), timeout=1)
    assert executed == [], "升级后的动作绝不能被执行"
    error = result.get("error", "")
    assert "escalate" in error and "write" in error and "delete" in error


async def test_audit_records_both_argument_sets(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """③ 审计里同时可见原始与编辑后参数（否则无法回答"用户批准了什么"）。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}, reason="加个 -la"),
        fake_redis,
    )
    await runtime._await_approval(call, _task(), timeout=1)

    entries = _audit_entries(_audit_dir)
    assert entries, "审批决策必须留审计"
    last = entries[-1]
    assert last["decision"] == DECISION_EDIT
    assert "ls" in last["original_arguments"]
    assert "ls -la" in last["edited_arguments"]
    assert last["level_before"] == "write"
    assert last["level_after"] == "write"
    assert last["reason"] == "加个 -la"


async def test_edit_escalation_is_audited(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """被拦下的那次也要留痕（否则"为什么没执行"无从查证）。"""
    _capture_execution(monkeypatch, [])

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "rm -rf /"}),
        fake_redis,
    )
    await runtime._await_approval(call, _task(), timeout=1)

    last = _audit_entries(_audit_dir)[-1]
    assert last["level_before"] == "write"
    assert last["level_after"] == "delete"
    assert "escalate" in last["reason"]


async def test_edit_ticket_is_rewritten_to_edited_arguments(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis, _audit_dir: Path
) -> None:
    """不变量：票据在 edit 后指向**被批准并执行**的那份参数。"""
    _capture_execution(monkeypatch, [])

    runtime = _runtime()
    call = _call({"cmd": "ls"})
    await _prepare(
        runtime, call, ApprovalDecision(decision=DECISION_EDIT, arguments={"cmd": "ls -la"}), fake_redis
    )
    await runtime._await_approval(call, _task(), timeout=1)

    from app.redis_keys import rkey

    raw = fake_redis.store.get(rkey("approval_req:call_1"))
    assert raw is not None, "edit 后票据应被重写"
    ticket = ApprovalTicket.from_json(raw)
    assert ticket is not None
    assert ticket.args_hash == args_hash("shell_exec", {"cmd": "ls -la"})
    assert ticket.caller_id == _approval_ticket_for(call, _task(), {"cmd": "ls"}).caller_id
    assert ticket.resource_scope == _approval_ticket_for(call, _task(), {"cmd": "ls -la"}).resource_scope


# ── H10：票据绑定与同等级 edit 重新授权 ────────────────────────────────


def test_ticket_serializes_all_binding_fields() -> None:
    call = _call({"path": "a.txt"}, name="delete_file")
    ticket = _approval_ticket_for(call, _task(), {"path": "a.txt"})
    parsed = ApprovalTicket.from_json(ticket.to_json())
    assert parsed == ticket
    assert parsed.caller_id
    assert parsed.resource_scope


@pytest.mark.parametrize("missing", ["caller_id", "resource_scope"])
def test_unbound_legacy_ticket_is_rejected(missing: str) -> None:
    """旧格式缺调用者/资源范围时不能因工具名与参数相同而复用。"""
    call = _call({"path": "a.txt"}, name="delete_file")
    payload = json.loads(_approval_ticket_for(call, _task(), {"path": "a.txt"}).to_json())
    payload.pop(missing)
    assert ApprovalTicket.from_json(json.dumps(payload)) is None


async def test_same_level_edit_reauthorizes_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    broker_authorize: AsyncMock,
    _audit_dir: Path,
) -> None:
    """delete_file a→b 等级不变，也必须先把编辑后的完整参数交给 Broker。"""
    broker_authorize.return_value = {
        "allowed": True,
        "level": "delete",
        "requires_user_approval": True,
        "requires_second_check": True,
    }
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call({"path": "a.txt"}, name="delete_file")
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"path": "b.txt"}),
        fake_redis,
        task,
    )

    result = await runtime._await_approval(call, task, timeout=1)

    assert result.get("error") is None
    assert executed == [{"path": "b.txt"}]
    broker_authorize.assert_awaited_once()
    authorized = broker_authorize.await_args
    assert authorized.args == ("delete_file", {"path": "b.txt"})
    assert authorized.kwargs["tenant_id"] == 7
    assert authorized.kwargs["user_id"] == "user-1"
    assert authorized.kwargs["session_id"] == "session-1"
    assert authorized.kwargs["tool_call_id"] == "call_1"

    from app.redis_keys import rkey

    ticket = ApprovalTicket.from_json(fake_redis.store[rkey("approval_req:call_1")])
    assert ticket is not None
    assert ticket.args_hash == args_hash("delete_file", {"path": "b.txt"})
    assert json.loads(ticket.resource_scope)["resources"] == {"path": "b.txt"}


async def test_edit_cannot_execute_when_new_ticket_binding_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    broker_authorize: AsyncMock,
    _audit_dir: Path,
) -> None:
    broker_authorize.return_value = {
        "allowed": True,
        "level": "delete",
        "requires_user_approval": True,
        "requires_second_check": True,
    }
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call({"path": "a.txt"}, name="delete_file")
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"path": "b.txt"}),
        fake_redis,
        task,
    )
    monkeypatch.setattr(runtime, "_store_approval_ticket", AsyncMock(return_value=False))

    result = await runtime._await_approval(call, task, timeout=1)

    assert executed == []
    assert "edited approval ticket rewrite failed" in result.get("error", "")


@pytest.mark.parametrize(
    "outcome",
    [
        {"allowed": False, "reason": "target outside approved scope"},
        RuntimeError("broker unavailable"),
    ],
)
async def test_same_level_edit_fails_closed_without_fresh_authorization(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    broker_authorize: AsyncMock,
    outcome: object,
    _audit_dir: Path,
) -> None:
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call({"path": "a.txt"}, name="delete_file")
    if isinstance(outcome, Exception):
        broker_authorize.side_effect = outcome
    else:
        broker_authorize.return_value = outcome
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"path": "b.txt"}),
        fake_redis,
        task,
    )

    result = await runtime._await_approval(call, task, timeout=1)

    assert executed == []
    assert "fresh Broker authorization" in result.get("error", "")
    broker_authorize.assert_awaited_once()


async def test_canonically_unchanged_edit_does_not_reauthorize(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
    broker_authorize: AsyncMock,
    _audit_dir: Path,
) -> None:
    """键序/路径写法变化但规范化摘要相同，不是一次新的参数授权。"""
    executed: list[dict[str, Any]] = []
    _capture_execution(monkeypatch, executed)
    runtime = _runtime()
    task = _task()
    call = _call({"path": "./a.txt", "content": "x"}, name="edit_file")
    await _prepare(
        runtime,
        call,
        ApprovalDecision(decision=DECISION_EDIT, arguments={"content": "x", "path": "a.txt"}),
        fake_redis,
        task,
    )

    result = await runtime._await_approval(call, task, timeout=1)

    assert result.get("error") is None
    assert executed == [{"content": "x", "path": "a.txt"}]
    broker_authorize.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("user_id", "user-2", "caller mismatch"),
        ("tenant_id", 8, "caller mismatch"),
        ("session_id", "session-2", "resource scope mismatch"),
        ("workbench_context", {"project": "project-2", "root_path": "other"}, "resource scope mismatch"),
    ],
)
async def test_second_check_rejects_caller_or_resource_scope_mismatch(
    fake_redis: _FakeRedis,
    field: str,
    value: object,
    message: str,
) -> None:
    runtime = _runtime()
    task = _task()
    call = _call({"path": "a.txt"}, name="delete_file")
    await runtime._store_approval_ticket(_approval_ticket_for(call, task, {"path": "a.txt"}))
    changed = SimpleNamespace(**vars(task))
    setattr(changed, field, value)

    failure = await runtime._second_check_approval(call, changed)

    assert message in failure


async def test_second_check_rejects_tool_and_same_level_argument_mismatch(fake_redis: _FakeRedis) -> None:
    from app.redis_keys import rkey

    runtime = _runtime()
    task = _task()
    original = _call({"path": "a.txt"}, name="delete_file")
    ticket = _approval_ticket_for(original, task, {"path": "a.txt"})
    payload = json.loads(ticket.to_json())
    payload["tool_call_id"] = "another-call"
    fake_redis.store[rkey("approval_req:call_1")] = json.dumps(payload)
    assert "call id mismatch" in await runtime._second_check_approval(original, task)

    await runtime._store_approval_ticket(ticket)
    wrong_tool = _call({"path": "a.txt"}, name="forget")
    assert "tool mismatch" in await runtime._second_check_approval(wrong_tool, task)

    # 重新放票，模拟另一次同 id 审批；同等级改目标不能被原票复用。
    await runtime._store_approval_ticket(_approval_ticket_for(original, task, {"path": "a.txt"}))
    changed_args = _call({"path": "b.txt"}, name="delete_file")
    assert "args mismatch" in await runtime._second_check_approval(changed_args, task)


async def test_second_check_consumes_ticket_and_canonical_equivalence_passes(fake_redis: _FakeRedis) -> None:
    runtime = _runtime()
    task = _task()
    original = _call({"path": "./a.txt", "content": "x"}, name="edit_file")
    await runtime._store_approval_ticket(
        _approval_ticket_for(original, task, {"path": "./a.txt", "content": "x"})
    )
    equivalent = _call({"content": "x", "path": "a.txt"}, name="edit_file")

    assert await runtime._second_check_approval(equivalent, task) == ""
    assert "no approval ticket" in await runtime._second_check_approval(equivalent, task)
