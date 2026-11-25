"""G20：子 Agent 要能**挂起等人工**（审批 / 提问不会"超时即该步失败"）。

**修前会怎样**（本文件前几条断言在修前失败）：子 Agent 跑在合成会话 ``sub:<父>:<run>``
里、**没有 ``agent_runs`` 行**（``app/agent/interactions.py`` 的注释写明这一点）。而
``AgentRuntime._register_interaction`` 的前提是"这一轮必须有一份能被续跑的 run 现场"：它
只认 ``task.run_token``。子 Agent 的 ``run_token`` 为空 ⇒ 票**根本不登记** ⇒ 审批/提问退回
"无人值守 ⇒ 即时拒绝"，用户看到的是子 Agent 莫名失败，而不是"它在等我"。

**为什么这么改**：给子会话建一行 ``agent_runs``（并绑定 run fence），于是：

* ``_register_interaction`` 能登记 → 前端看到待办 → 子 Agent **停在等人的地方**；
* 挂起期间 runtime 写下的 checkpoint 有归属行，续跑那一段能接上（``load_awaiting`` →
  ``claim_awaiting``），答复到达后由新一段 ``consume_grant`` 取走并继续执行。

**有界策略**（本实现的取舍，见 ``app/subagent/suspension.py``）：

1. 挂起期间**释放写槽**（writer slot）：人在思考时不该继续占着工作区的写权；
   恢复前重新取槽，取不到就以明确失败收尾（不静默降级）。
2. 挂起期间**停 wall 表**并**显式记录**等待时长：等人不是子 Agent 的花费，但它必须有界 ——
   等待本身由 ``SUBAGENT_SUSPEND_WAIT_SECONDS``（默认 900s，硬上限 1h）封顶。
3. 超时**明确收尾**：把该会话的待办转成 ``expired``、``agent_runs`` 行标 ``expired``，
   子 Agent 以一条 ``error`` 事件结束成 **failed**（不是静默卡死，也不是假 completed）。
4. 每一段的 ``agent_runs`` 行都会收口（completed / failed / cancelled / expired），
   不留悬挂的 ``running`` 行。

四条不变式（对应任务书 ①②③④）：

1. 子 Agent 发审批 ⇒ **有** pending 待办行，且**不会**立刻失败；
2. 回答后子 Agent 继续并完成（新的一段 + 取票）；
3. 不回答且超时 ⇒ 待办 ``expired``，子 Agent 收尾为**明确失败**；
4. 挂起期间不占写槽（本实现选的有界策略，断言在最后一个用例里）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent import interactions
from app.agent import runtime as runtime_module
from app.agent.subagent_runner import SubAgentRunner
from app.subagent import suspension


class _NullStore:
    """runner 的最小 store 替身（只要有这些方法，不落库）。"""

    async def start_run(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def add_step(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def flush_steps(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def finish_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeInteractions:
    """``pending_interactions`` 的内存替身（只实现子 Agent 挂起用到的契约）。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.consumed: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> str:
        self.create_calls.append(kwargs)
        row = {
            "id": f"ix-{len(self.rows) + 1}",
            "status": "pending",
            "session_id": kwargs.get("session_id", ""),
            "kind": kwargs.get("kind", ""),
            "tool_call_id": kwargs.get("tool_call_id", ""),
            "tool_name": kwargs.get("tool_name", ""),
            "args_hash": kwargs.get("args_hash", ""),
            "run_id": kwargs.get("run_token", ""),
        }
        self.rows.append(row)
        return row["id"]

    async def settle(self, **kwargs: Any) -> bool:
        for row in self.rows:
            if row["id"] == kwargs.get("interaction_id"):
                row["status"] = "consumed"
        return True

    async def consume_grant(self, **kwargs: Any) -> dict[str, Any] | None:
        self.consumed.append(kwargs)
        for row in self.rows:
            if (
                row["session_id"] == kwargs.get("session_id")
                and row["status"] == "answered"
                and row["args_hash"] == kwargs.get("args_hash")
            ):
                row["status"] = "consumed"
                return {"decision": {"decision": "approve"}, "reason": "ok", "id": row["id"]}
        return None

    async def list_open(self, *, tenant_id: int, user_id: str, session_id: str, limit: int = 20):
        return [
            dict(row)
            for row in self.rows
            if row["session_id"] == session_id and row["status"] in ("pending", "answered")
        ][:limit]

    async def expire_session(self, *, tenant_id: int, user_id: str, session_id: str) -> int:
        hit = 0
        for row in self.rows:
            if row["session_id"] == session_id and row["status"] in ("pending", "answered"):
                row["status"] = "expired"
                hit += 1
        return hit

    def answer(self, tool_call_id: str = "tc-1") -> None:
        for row in self.rows:
            if row["tool_call_id"] == tool_call_id:
                row["status"] = "answered"


@pytest.fixture
def ix(monkeypatch: pytest.MonkeyPatch) -> _FakeInteractions:
    fake = _FakeInteractions()
    for name in ("create", "settle", "consume_grant", "list_open", "expire_session"):
        monkeypatch.setattr(interactions, name, getattr(fake, name))
    return fake


class _Spies:
    """记录挂起链上的关键调用（**顺序**对断言很重要）。"""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.answer: bool | None = False
        self.wait_seconds: list[float] = []


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> _Spies:
    """只替换**落库**与**等待**两处（``expire_session`` 走真实现 → 假表里看得见）。"""
    spy = _Spies()

    async def _ensure_run_row(**kwargs: Any) -> bool:
        spy.rows.append(kwargs)
        return True

    async def _finish_run_row(**kwargs: Any) -> bool:
        spy.finished.append(kwargs)
        return True

    async def _wait_for_answer(**kwargs: Any) -> bool:
        spy.order.append("wait")
        spy.wait_seconds.append(float(kwargs.get("wait_seconds") or 0.0))
        if spy.answer is None:
            await asyncio.sleep(3600)  # 永远等下去（调用方随后取消）
        return bool(spy.answer)

    monkeypatch.setattr(suspension, "ensure_run_row", _ensure_run_row)
    monkeypatch.setattr(suspension, "finish_run_row", _finish_run_row)
    monkeypatch.setattr(suspension, "wait_for_answer", _wait_for_answer)
    return spy


def _runner(monkeypatch: pytest.MonkeyPatch, spies: _Spies, segments: list[dict[str, Any]]) -> Any:
    """用假 `AgentRuntime.run` 驱动 runner：每一段可以"登记审批并挂起"或正常结束。"""

    async def _run(self: Any, task: Any):  # noqa: ANN001
        spies.runs.append({"session_id": task.session_id, "run_token": task.run_token})
        index = min(len(spies.runs) - 1, len(segments) - 1)
        spec = segments[index] if segments else {}
        if spec.get("register"):
            # 这两步与真实 runtime 完全同构：先登记（前提是 task.run_token 非空），
            # 再把这一轮记成"停在等人上"。修前 register 会静默返回 ""，于是这里不会挂起。
            await self._register_interaction(
                task,
                kind="approval",
                tool_call_id="tc-1",
                tool_name="write_file",
                args_hash="hash-1",
                question="需要确认这次写入",
                request={"question": "需要确认这次写入"},
            )
            self._suspend_for_input(task, "tc-1", tool_name="write_file", kind="approval")
        if spec.get("consume"):
            # 续跑那一段：真实 runtime 在 `_begin_approval` 里先按参数指纹取票
            # （`consume_grant`），取到就带着答复执行，不再重新问。
            await self._take_interaction_grant(
                task, kind="approval", tool_name="write_file", args_hash="hash-1"
            )
        for evt in spec.get("events", []):
            yield evt

    monkeypatch.setattr(runtime_module.AgentRuntime, "run", _run)
    return SubAgentRunner(
        store=_NullStore(),
        gateway=object(),
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id=1,
        user_id="u1",
    )


# ── 1. 有 pending 行，且**不会**立刻失败 ──────────────────────────────────


async def test_子Agent发审批时有pending待办且停在等人上(
    ix: _FakeInteractions, spies: _Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies.answer = None  # 一直等（用例最后取消）
    runner = _runner(monkeypatch, spies, [{"register": True}])

    task = asyncio.create_task(runner.run("写个文件", run_id="r1"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if spies.order:
            break

    assert spies.order[:1] == ["wait"], f"子 Agent 必须停在「等人」上：{spies.order}"
    assert not task.done(), "挂起不是失败：这一段还没结束"

    child_session = "sub:s1:r1"
    pending = [
        row for row in ix.rows if row["session_id"] == child_session and row["status"] == "pending"
    ]
    assert pending, "子会话上必须真的有一行 pending_interactions（否则用户看不到、也答不了）"
    assert pending[0]["kind"] == "approval"
    assert pending[0]["run_id"], "票上要带 run_token 归属（续跑那一段靠它接手）"

    assert spies.rows and spies.rows[0]["session_id"] == child_session
    assert spies.rows[0]["run_token"], "子 Agent 必须有 run_token，否则 _register_interaction 直接跳过"
    assert spies.rows[0]["generation"] >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 取消也要收口：行不能留在 running
    for _ in range(20):
        await asyncio.sleep(0)
    assert spies.finished and spies.finished[-1]["status"] == "cancelled"


# ── 2. 回答后子 Agent 继续并完成 ──────────────────────────────────────────


async def test_回答后子Agent继续跑并完成(
    ix: _FakeInteractions, spies: _Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies.answer = True
    runner = _runner(
        monkeypatch,
        spies,
        [
            {"register": True},  # 第一段：发审批并挂起
            {  # 第二段：取票 → 接着跑完
                "consume": True,
                "events": [runtime_module.AgentEvent(type="text", content="写完了")],
            },
        ],
    )

    result = await runner.run("写个文件", run_id="r2")

    assert result.status == "completed", result.error
    assert len(spies.runs) == 2, "答复到达后必须开**新的一段**（新 run，才能取票续跑）"
    assert spies.runs[0]["run_token"] != spies.runs[1]["run_token"], "续跑段必须换 run_token"
    assert [row["generation"] for row in spies.rows] == [1, 2], "接手挂起现场会把 generation 推进"
    assert ix.consumed and ix.consumed[0]["session_id"] == "sub:s1:r2", "续跑段要按参数指纹取票"
    assert spies.finished[-1]["status"] == "completed", "行要收口成 completed，不留 running"


# ── 3. 不回答且超时 ⇒ expired + 明确失败 ─────────────────────────────────


async def test_不回答超时后待办转expired且子Agent明确失败(
    ix: _FakeInteractions, spies: _Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies.answer = False
    runner = _runner(monkeypatch, spies, [{"register": True}])

    result = await runner.run("写个文件", run_id="r3")

    assert result.status == "failed", "挂起超时必须**明确失败**，不能静默卡死也不能假装完成"
    assert "expired" in (result.error or ""), result.error
    assert [row["status"] for row in ix.rows] == ["expired"], "待办要转成 expired（用户/巡检都看得见）"
    assert spies.finished[-1]["status"] == "expired", "agent_runs 行同样标 expired，不留悬挂"
    assert spies.wait_seconds and 0 < spies.wait_seconds[0] <= suspension.MAX_SUSPEND_WAIT_SECONDS


async def test_挂起轮数有上限不会无限等下去(
    ix: _FakeInteractions, spies: _Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次都"答复到了"但子 Agent 又挂起（模型反复要确认）⇒ 也有上限。"""
    spies.answer = True
    runner = _runner(monkeypatch, spies, [{"register": True}])  # 每一段都挂起

    result = await runner.run("写个文件", run_id="r4")

    assert result.status == "failed"
    assert len(spies.runs) == suspension.MAX_SUSPEND_ROUNDS + 1
    assert "上限" in (result.error or "") or "expired" in (result.error or ""), result.error
    assert [row["status"] for row in ix.rows][-1] == "expired"


# ── 4. 挂起期间不占写槽（有界策略）─────────────────────────────────────────


async def test_挂起期间释放写槽并在续跑前重新取槽(
    ix: _FakeInteractions, spies: _Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies.answer = True
    runner = _runner(
        monkeypatch,
        spies,
        [{"register": True}, {"events": [runtime_module.AgentEvent(type="text", content="ok")]}],
    )

    async def _acquire(spec, *, run_id, child_depth):  # noqa: ANN001
        spies.order.append("acquire")

        def _release() -> None:
            spies.order.append("release")

        return _release

    monkeypatch.setattr(runner, "_acquire_write_slot", _acquire)

    await runner.run("写个文件", run_id="r5")

    assert spies.order.count("acquire") == 2, "续跑前必须重新取槽（可以写，但要有仲裁）"
    first_release = spies.order.index("release")
    first_wait = spies.order.index("wait")
    assert first_release < first_wait, f"挂起必须**先**释放写槽：{spies.order}"


# ── 5. 真实现：`wait_for_answer` 有答复即返回、超时有界 ────────────────────


async def test_wait_for_answer_有答复即返回(ix: _FakeInteractions) -> None:
    await ix.create(
        tenant_id=1, user_id="u1", session_id="sub:s1:r9", kind="approval", tool_call_id="tc-9"
    )
    ix.answer("tc-9")

    assert await suspension.wait_for_answer(
        tenant_id=1, user_id="u1", session_id="sub:s1:r9", wait_seconds=0.2, poll_seconds=0.01
    )


async def test_wait_for_answer_超时返回假而不是一直等(ix: _FakeInteractions) -> None:
    await ix.create(
        tenant_id=1, user_id="u1", session_id="sub:s1:r10", kind="approval", tool_call_id="tc-10"
    )
    loop = asyncio.get_running_loop()
    started = loop.time()

    assert not await suspension.wait_for_answer(
        tenant_id=1, user_id="u1", session_id="sub:s1:r10", wait_seconds=0.05, poll_seconds=0.01
    )
    assert loop.time() - started < 2.0, "等待必须有界：不能因为没人回答就把子 Agent 挂死"


def test_等待上限被硬上限截断() -> None:
    """``SUBAGENT_SUSPEND_WAIT_SECONDS`` 配得再大也不会超过硬上限（配置错不该变成"永久等"）。"""
    assert suspension.suspend_wait_seconds() <= suspension.MAX_SUSPEND_WAIT_SECONDS
    assert suspension.clamp_wait_seconds(10**9) == suspension.MAX_SUSPEND_WAIT_SECONDS
    assert suspension.clamp_wait_seconds(-5) > 0
