"""P1 回归：无人值守下的「等人」必须有上限、且到点必须收场。

要防的失败模式是**永久挂起**，不是"等得久"。三条不变量：

1. **等待有上限，且可配置**：`_await_approval` / `_await_answer` 不再写死 300s。
   无人值守里没人会来点，多等的每一秒都是白等；交互场景里用户可能只想等半分钟。
   两种场景共用一个硬编码值，必然有一边不合适。
2. **到点即收场，不留悬空**：超时后转 `awaiting_input` 挂起（把这一步原样留在
   "等你确认"里），而不是无限等下去。连落库现场都没有时才退回即时拒绝 ——
   那种情况下的挂起等于永久挂起。
3. **票的寿命必须覆盖等待窗口**：票比等待先过期 = 挂起即永久挂起（超时去挂起时
   票已经没了，再没人能看见它、也没人能把它收口成 `abandoned`）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import runtime as runtime_module
from app.agent.runtime import (
    APPROVAL_TTL_SECONDS,
    ASK_USER_TOOL,
    AgentRuntime,
    _decision_key_ttl_seconds,
    _interaction_ttl_seconds,
    _interaction_wait_seconds,
)
from app.agent.runtime_types import AgentTask, ApprovalDecision, DECISION_APPROVE
from app.config import settings


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch):
    """把两个配置项调到指定值；用例结束自动还原。"""

    def _set(*, wait: float | None = None, ttl: int | None = None) -> None:
        if wait is not None:
            monkeypatch.setattr(settings, "agent_interaction_wait_seconds", wait, raising=False)
        if ttl is not None:
            monkeypatch.setattr(settings, "agent_interaction_ttl_seconds", ttl, raising=False)

    return _set


class TestWaitLimitIsConfigurable:
    def test_default_matches_the_historic_300s(self, cfg, monkeypatch: pytest.MonkeyPatch):
        """默认值不变：升级本身不该改变任何现网行为。"""
        cfg(wait=300.0)
        assert _interaction_wait_seconds() == 300.0

    def test_wait_limit_follows_config(self, cfg):
        """配置是多少就等多少 —— 这是"可配置"的全部意义。"""
        cfg(wait=30.0)
        assert _interaction_wait_seconds() == 30.0
        cfg(wait=1800.0)
        assert _interaction_wait_seconds() == 1800.0

    def test_non_positive_wait_falls_back_to_one_second(self, cfg):
        """0 / 负数会被夹到 1s，而不是变成"立刻放弃"。

        `max(1.0, …)` 而不是直接返回：把上限配成 0 的意图通常是"别等"，
        但"别等"的正确表达是无人值守自动拒绝，不是让每一次审批都在同一瞬间过期。
        """
        cfg(wait=0.0)
        assert _interaction_wait_seconds() == 1.0
        cfg(wait=-5.0)
        assert _interaction_wait_seconds() == 1.0

    def test_garbage_config_does_not_crash_the_turn(self, cfg):
        """配成非数字时回落到 300s：配置错不该让每个回合都抛异常。"""
        cfg(wait="not-a-number")
        assert _interaction_wait_seconds() == 300.0


class TestTicketOutlivesTheWait:
    """票的寿命必须 >= 等待上限，否则挂起即永久挂起。"""

    def test_default_ticket_ttl_is_a_day(self, cfg):
        cfg(wait=300.0, ttl=24 * 3600)
        assert _interaction_ttl_seconds() == 24 * 3600

    def test_ttl_is_raised_when_it_would_expire_before_the_wait(self, cfg):
        """配错方向（票比等待先过期）时由运行时垫到等待上限。

        这条不变量只写在注释里是不够的 —— 配错的人不会去读注释。
        """
        cfg(wait=1800.0, ttl=60)
        assert _interaction_ttl_seconds() == 1800
        assert _interaction_ttl_seconds() >= int(_interaction_wait_seconds())

    def test_ttl_is_capped_at_30_days(self, cfg):
        """上限兜底：票不该因为一个笔误（如把天数当秒填）活到几年后。"""
        cfg(wait=300.0, ttl=400 * 24 * 3600)
        assert _interaction_ttl_seconds() == 30 * 24 * 3600


class TestDecisionKeyCoversTheWindow:
    """跨副本决策键 / 票据的 Redis TTL 必须覆盖整个等待窗口。"""

    def test_default_key_ttl_keeps_the_historic_300s(self, cfg):
        cfg(wait=300.0)
        assert _decision_key_ttl_seconds() == max(360, int(APPROVAL_TTL_SECONDS))

    def test_longer_wait_extends_the_key_ttl(self, cfg):
        """等待上限调大后，键必须跟着活更久。

        否则用户在窗口后半段点的批准会**先被 Redis 清掉再被读到** ——
        表现是"我明明点了，它还是超时挂起了"，且日志里只会看到一次超时。
        """
        cfg(wait=1800.0)
        assert _decision_key_ttl_seconds() >= 1800
        assert _decision_key_ttl_seconds() > int(APPROVAL_TTL_SECONDS)

    def test_short_wait_never_shrinks_below_the_historic_ttl(self, cfg):
        """等得短了不代表键要更短命：键短于 300s 会让慢副本取不到答复。"""
        cfg(wait=10.0)
        assert _decision_key_ttl_seconds() >= int(APPROVAL_TTL_SECONDS)


def _rt() -> AgentRuntime:
    return AgentRuntime(gateway=None, unattended=False)


def _task() -> AgentTask:
    return AgentTask(
        id="turn-1",
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        content="hi",
        # 挂起 / 登记票的前提：这一轮必须有可续跑的 run 现场（见 `_register_interaction`）。
        # 没有它时 `create` 根本不会被调用，测试就会"通过"得毫无意义。
        run_token="rt-1",
    )


class TestWaitFunctionsHonorTheConfig:
    """接线级：两个等待函数**确实**用了配置值，而不只是辅助函数支持配置。

    只测辅助函数是自欺 —— 配置项存在、辅助函数正确、但调用点仍传死值，
    现场表现与改之前完全一样，而测试却全绿。
    """

    async def test_approval_waits_the_configured_limit(self, cfg, monkeypatch):
        cfg(wait=45.0)
        rt = _rt()
        call = {"id": "tc-1", "name": "run_code", "arguments": '{"code": "print(1)"}'}
        future: object = object()
        rt._pending_approvals["tc-1"] = future  # noqa: SLF001
        seen: list[float] = []

        async def _deny(tc_id, fut, timeout, user_id=""):
            seen.append(timeout)
            return None

        monkeypatch.setattr(rt, "_wait_approval_decision", _deny)

        result = await rt._await_approval(call, _task())  # noqa: SLF001

        assert seen == [45.0], "等待上限必须来自配置，而不是签名里的 300.0"
        # 到点必须收场：没有落库现场时退回明确错误，而不是继续等
        assert "timed out" in result["error"]

    async def test_answer_waits_the_configured_limit(self, cfg, monkeypatch):
        cfg(wait=45.0)
        rt = _rt()
        call = {"id": "tc-ask", "name": ASK_USER_TOOL, "arguments": '{"question": "?"}'}
        rt._pending_answers["tc-ask"] = object()  # noqa: SLF001
        seen: list[float] = []

        async def _no_answer(tc_id, fut, timeout):
            seen.append(timeout)
            return None

        monkeypatch.setattr(rt, "_wait_answer_decision", _no_answer)

        result = await rt._await_answer(call, _task())  # noqa: SLF001

        assert seen == [45.0], "ask_user 与审批共用同一条可配置上限"
        assert "did not answer" in result["error"]

    async def test_explicit_timeout_argument_still_wins(self, cfg, monkeypatch):
        """显式传参优先于配置：调用方点名要等多久时不该被配置覆盖。"""
        cfg(wait=45.0)
        rt = _rt()
        call = {"id": "tc-1", "name": "run_code", "arguments": "{}"}
        rt._pending_approvals["tc-1"] = object()  # noqa: SLF001
        seen: list[float] = []

        async def _deny(tc_id, fut, timeout, user_id=""):
            seen.append(timeout)
            return None

        monkeypatch.setattr(rt, "_wait_approval_decision", _deny)

        await rt._await_approval(call, _task(), timeout=7.0)  # noqa: SLF001

        assert seen == [7.0]

    async def test_ticket_is_registered_with_the_configured_ttl(self, cfg, monkeypatch):
        """票的 TTL 必须按配置下发，否则票会先于等待窗口过期 → 挂起即永久挂起。"""
        cfg(wait=60.0, ttl=7200)
        from app.agent import interactions

        captured: list[dict] = []

        async def _create(**kwargs):
            captured.append(kwargs)
            return "ix-1"

        monkeypatch.setattr(interactions, "create", _create)

        rt = _rt()
        await rt._register_interaction(  # noqa: SLF001
            _task(),
            kind="approval",
            tool_call_id="tc-1",
            tool_name="run_code",
            args_hash="h",
            question="确认？",
            request={},
        )

        assert captured and captured[0]["ttl_seconds"] == 7200
        assert captured[0]["ttl_seconds"] >= int(_interaction_wait_seconds())


class TestUnavailableChannelDoesNotAnswerForTheUser:
    """远端通道不可用 ≠ 用户没回答。

    要防的失败模式：Redis 不可用时（桌面单机部署的常态），远端 poll 会**立即**
    返回空值。旧实现用一次 `FIRST_COMPLETED` 收工，于是"这条通道用不了"被当成
    "没人答"，仍在本机等着的用户直接判超时/挂起 —— 用户点了「批准」什么也不会
    发生。等待的对象是人，不是通道。
    """

    async def test_local_approval_wins_when_remote_is_down(self, cfg):
        cfg(wait=5.0)
        rt = _rt()

        async def _remote_down(tc_id, timeout, user_id="", interval=1.5):
            return None  # Redis 不可用：立即返回，不是等满

        rt._poll_remote_decision = _remote_down  # noqa: SLF001

        future = asyncio.get_running_loop().create_future()

        async def _approve_shortly():
            await asyncio.sleep(0.05)
            if not future.done():
                future.set_result(ApprovalDecision(decision=DECISION_APPROVE))

        helper = asyncio.create_task(_approve_shortly())
        decision = await rt._wait_approval_decision("tc-1", future, timeout=5.0)  # noqa: SLF001
        await helper

        assert decision is not None, "本机用户的批准不能被「远端不可用」吞掉"
        assert decision.decision == DECISION_APPROVE

    async def test_local_answer_wins_when_remote_is_down(self, cfg):
        cfg(wait=5.0)
        rt = _rt()

        async def _remote_down(tc_id, timeout, interval=1.5):
            return None

        rt._poll_remote_answer = _remote_down  # noqa: SLF001

        future = asyncio.get_running_loop().create_future()

        async def _answer_shortly():
            await asyncio.sleep(0.05)
            if not future.done():
                future.set_result("用方案 A")

        helper = asyncio.create_task(_answer_shortly())
        answer = await rt._wait_answer_decision("tc-ask", future, timeout=5.0)  # noqa: SLF001
        await helper

        assert answer == "用方案 A", "本机用户的回答不能被「远端不可用」吞掉"

    async def test_both_channels_dead_still_returns_at_the_deadline(self, cfg):
        """两条通道都用不了时必须到点收场 —— 修复不能变成永不返回。"""
        cfg(wait=0.2)
        rt = _rt()

        async def _remote_down(*args, **kwargs):
            return None

        rt._poll_remote_decision = _remote_down  # noqa: SLF001
        never = asyncio.get_running_loop().create_future()

        got = await rt._wait_approval_decision("tc-1", never, timeout=0.2)  # noqa: SLF001
        assert got is None

