"""被动中断必须保留现场，用户显式停止必须写终态。

背景：`run()` 的 `finally` 原本把**所有** `CancelledError` / `GeneratorExit` 一律写成
`cancelled`。而 `cancelled` 是终态 —— 一出 `checkpoint.ACTIVE_STATUSES`，
`checkpoint.load()` 与 reconciler 的 `status IN ('running','checkpointed','resuming')`
都再也看不到那条快照。于是「网关单轮超时」「SSE 断开」这类**没人下过令**的中断，
会把该会话的续跑现场一起销毁：用户重试只能从头重来。

本文件钉住三条判定：

1. **确认没有用户停止信号** ⇒ 补快照 + 写 `checkpointed`（仍在活跃集合内，可被续跑）；
2. **有用户停止信号** ⇒ 写 `cancelled`（旧行为不变，绝不自动复活用户主动停掉的 run）；
3. **信号读不到**（Redis 不可用）⇒ 不猜，退回 `cancelled`。宁可让用户重试，
   也不要把一个可能已被叫停的回合交给 reconciler 去自动复活 —— 那会继续烧 token。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent import checkpoint as ckpt
from app.agent import interrupt
from app.agent.runtime import AgentRuntime, AgentTask


async def _run_to_cancel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe: Any = "patched",
    stop_during_call: bool = False,
) -> dict[str, Any]:
    """让引擎在第一次模型调用处被取消，并记录收尾时的状态迁移。

    `probe` 是被 monkeypatch 的 `probe_interrupt` 返回值：
      * `False` ⇒ 确认没有用户停止信号（外因中断）；
      * `None`  ⇒ 读不到（Redis 不可用）；
      * `"patched"` 且 `stop_during_call=True` ⇒ **不**打桩，改为在模型调用期间置上
        真实的用户停止信号，让收尾判据自然读到 `True`。
    """
    marks: list[str] = []
    saves: list[dict[str, Any]] = []

    class Gateway:
        async def chat_stream(self, **_kwargs: Any):
            if stop_during_call:
                # 用户点停止落在「模型调用进行中」：轮次边界的检查已经过去了，
                # 于是它只能在收尾判据里被看到 —— 这正是要覆盖的第二条路径。
                interrupt.request_interrupt_local("s-intr")
            raise asyncio.CancelledError()
            yield  # pragma: no cover — 只为让它成为 async generator

    async def fake_mark(task: AgentTask, status: str) -> None:
        marks.append(status)

    async def fake_save(task: AgentTask, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        saves.append(kwargs)

    runtime = AgentRuntime(gateway=Gateway())
    monkeypatch.setattr(runtime, "_mark_run_status", fake_mark)
    monkeypatch.setattr(runtime, "_save_checkpoint", fake_save)

    if not stop_during_call:

        async def fake_probe(session_id: str, **_kwargs: Any) -> bool | None:
            return probe

        monkeypatch.setattr(interrupt, "probe_interrupt", fake_probe)

    async def fake_clear(session_id: str, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(interrupt, "clear_interrupt", fake_clear)

    task = AgentTask(
        id="t-intr",
        tenant_id="t",
        user_id="u",
        session_id="s-intr",
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=3,
    )

    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in runtime.run(task)]

    return {"marks": marks, "saves": saves}


@pytest.fixture(autouse=True)
def _clean_local_flags():
    interrupt._LOCAL.clear()
    yield
    interrupt._LOCAL.clear()


async def test_transport_cancel_keeps_run_resumable(monkeypatch: pytest.MonkeyPatch):
    """没有用户停止信号 ⇒ 写 `checkpointed` 而不是 `cancelled`。"""
    out = await _run_to_cancel(monkeypatch, probe=False)

    assert ckpt.STATUS_CHECKPOINTED in out["marks"], (
        "外因中断必须保留现场：写 cancelled 会把它移出 ACTIVE_STATUSES，"
        "load()/reconciler 再也看不到这条快照"
    )
    assert ckpt.STATUS_CANCELLED not in out["marks"]
    assert out["saves"], "标了可续就必须真的补一次快照，否则续跑没有现场可用"
    assert ckpt.STATUS_CHECKPOINTED in ckpt.ACTIVE_STATUSES


async def test_user_stop_is_terminal(monkeypatch: pytest.MonkeyPatch):
    """有用户停止信号 ⇒ 写 `cancelled`，且**不**补快照（不要给复活留材料）。

    注意用**真实**信号驱动（`stop_during_call`），不是打桩返回 `True`：用户点停止可能落在
    「模型调用进行中」,这时轮次边界的检查已经过去了，只剩收尾判据能看到信号。这条路径
    才是最容易把主动停止误判成外因中断的地方。
    """
    out = await _run_to_cancel(monkeypatch, stop_during_call=True)

    assert out["marks"] == [ckpt.STATUS_CANCELLED]
    assert out["saves"] == []


async def test_turn_boundary_stop_already_marks_terminal(monkeypatch: pytest.MonkeyPatch):
    """轮次边界就发现停止信号时，run 正常 `return`（不抛），`cancelled` 已写。

    这条**不经过** `finally` 的兜底判据 —— 它走 `runtime.run` 里的 `is_interrupted` 分支。
    在 `finally` 里再写一次必须是幂等/无害的（`mark()` 只迁活跃行）。
    """
    marks: list[str] = []

    class Gateway:
        async def chat_stream(self, **_kwargs: Any):
            raise AssertionError("取消就该在轮次边界停住，不该再调模型")
            yield  # pragma: no cover

    async def fake_mark(task: AgentTask, status: str) -> None:
        marks.append(status)

    async def fake_clear(session_id: str, **_kwargs: Any) -> None:
        return None

    runtime = AgentRuntime(gateway=Gateway())
    monkeypatch.setattr(runtime, "_mark_run_status", fake_mark)
    monkeypatch.setattr(interrupt, "clear_interrupt", fake_clear)

    task = AgentTask(
        id="t-boundary",
        tenant_id="t",
        user_id="u",
        session_id="s-boundary",
        content="hi",
        system_prompt="base",
        llm_config={"mode": "normal"},
        max_turns=3,
    )
    interrupt.request_interrupt_local("s-boundary")

    events = [event async for event in runtime.run(task)]

    assert "cancelled" in [e.type for e in events]
    assert marks == [ckpt.STATUS_CANCELLED]


async def test_unknown_signal_falls_back_to_terminal(monkeypatch: pytest.MonkeyPatch):
    """信号读不到（Redis 不可用）⇒ 保守写 `cancelled`，绝不猜成"可以续跑"。"""
    out = await _run_to_cancel(monkeypatch, probe=None)

    assert out["marks"] == [ckpt.STATUS_CANCELLED]
    assert out["saves"] == []


async def test_probe_interrupt_exposes_unknown():
    """`probe_interrupt` 必须把"读不到"暴露成 `None`，不能折成 False。

    折成 False 会让"用户刚停掉 + Redis 恰好故障"的 run 被当成外因中断自动复活；
    折成 True 又会把断线/超时错判成用户意图、销毁现场。两种折法都错。
    """

    class _Broken:
        async def get(self, _key: str) -> str | None:
            raise RuntimeError("redis down")

    interrupt._LOCAL.clear()
    assert await interrupt.probe_interrupt("s-unknown", redis=_Broken()) is None
    # 既有 `is_interrupted` 契约不变：读不到 = 没被取消（run 循环不该因 Redis 抽风停下）
    assert await interrupt.is_interrupted("s-unknown", redis=_Broken()) is False
    # 本地命中仍是确定的 True
    interrupt.request_interrupt_local("s-unknown")
    assert await interrupt.probe_interrupt("s-unknown", redis=_Broken()) is True
    interrupt._LOCAL.clear()
