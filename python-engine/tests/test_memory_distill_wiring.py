"""C6 记忆提炼：写入侧的 tenant 口径必须与召回侧一致。

回归的是本次修复的缺陷。`runtime.py` 曾这样写：

    stats = await self._memory.ingest_candidates(
        task.tenant_id or "default",   # ← 字符串
        ...

而 `AgentTask.tenant_id: int`、`user_memory_entries.tenant_id` 是 smallint。单租户
部署下 `tenant_id` 常常是 0（假值），于是这里真的会送一个 `"default"` 字符串进整数列，
asyncpg 抛 DataError；外层那层 `except` 只记一行 warning。表现就是**记忆提炼永远
不落库，却一声不响** —— 与"记忆系统与会话脱钩"的现场完全吻合。

剔除"某个值恰好非零所以没触发"这种偶然性：这里专门用 `tenant_id=0`。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.runtime import AgentRuntime, AgentTask
from app.config import settings
from app.gateway.provider import ChatResponse


def _task(**overrides: Any) -> AgentTask:
    payload: dict[str, Any] = dict(
        id="t1",
        tenant_id=0,
        user_id="u1",
        session_id="s1",
        content="帮我记住：我偏好中文回答",
        system_prompt="SYS",
        max_turns=1,
    )
    payload.update(overrides)
    return AgentTask(**payload)


def _runtime() -> tuple[AgentRuntime, MagicMock]:
    memory = MagicMock()
    session_ctx = MagicMock()
    session_ctx.profile_cached = False
    session_ctx.summaries_prefetched = 0
    memory.on_session_start = AsyncMock(return_value=session_ctx)
    recalled = MagicMock()
    recalled.has_content = False
    recalled.profile_block = ""
    recalled.summary_items = []
    memory.recall = AsyncMock(return_value=recalled)
    memory.on_turn_complete = AsyncMock()
    memory.on_session_end = AsyncMock()
    memory.ingest_candidates = AsyncMock(return_value={"added": 1})

    gateway = MagicMock()

    async def fake_stream(**kwargs: Any):
        yield ChatResponse(content="好的", finish_reason="stop")

    gateway.chat_stream = fake_stream
    return AgentRuntime(gateway=gateway, memory=memory), memory


def _patch_distill(monkeypatch, candidates: list[Any]) -> None:
    """替换提炼调用本身（它要真打网关），只留下"候选如何落库"这一段被测。"""

    async def fake_distill(**kwargs: Any):
        return candidates, 10, 5

    monkeypatch.setattr("app.memory.distill.distill_candidates", fake_distill)


@pytest.mark.asyncio
async def test_distill_passes_integer_tenant_not_a_string(monkeypatch):
    """tenant 必须是 `or 0`（与召回、记账同一口径），不能是 `"default"`。"""
    monkeypatch.setattr(settings, "memory_distill_enabled", True)
    _patch_distill(monkeypatch, [MagicMock(slot="fact", key="lang", value="中文", confidence=40)])

    runtime, memory = _runtime()
    _ = [event async for event in runtime.run(_task())]

    memory.ingest_candidates.assert_awaited()
    tenant = memory.ingest_candidates.await_args.args[0]
    assert not isinstance(tenant, str), (
        f"tenant 被降级成了字符串 {tenant!r}：这会被送进 smallint 列并抛 DataError"
    )
    assert tenant == 0


@pytest.mark.asyncio
async def test_distill_keeps_a_real_tenant_id(monkeypatch):
    """有真实租户时原样透传 —— 修复不能把正常值一起压成 0。"""
    monkeypatch.setattr(settings, "memory_distill_enabled", True)
    _patch_distill(monkeypatch, [MagicMock(slot="fact", key="lang", value="中文", confidence=40)])

    runtime, memory = _runtime()
    _ = [event async for event in runtime.run(_task(tenant_id=7))]

    assert memory.ingest_candidates.await_args.args[0] == 7


@pytest.mark.asyncio
async def test_distill_stays_off_by_default(monkeypatch):
    """默认关：不花一次模型调用，也不写任何东西。"""
    monkeypatch.setattr(settings, "memory_distill_enabled", False)
    _patch_distill(monkeypatch, [MagicMock(slot="fact", key="k", value="v", confidence=40)])

    runtime, memory = _runtime()
    _ = [event async for event in runtime.run(_task())]

    memory.ingest_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_candidates_means_no_write(monkeypatch):
    """提炼没产出候选时不该空写一轮（也就能确认"确实走了提炼分支"）。"""
    monkeypatch.setattr(settings, "memory_distill_enabled", True)
    _patch_distill(monkeypatch, [])

    runtime, memory = _runtime()
    _ = [event async for event in runtime.run(_task())]

    memory.ingest_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_failure_does_not_break_the_turn(monkeypatch):
    """落库失败只降级，不能让整个回合失败（提炼是增强项）。"""
    monkeypatch.setattr(settings, "memory_distill_enabled", True)
    _patch_distill(monkeypatch, [MagicMock(slot="fact", key="lang", value="中文", confidence=40)])

    runtime, memory = _runtime()
    memory.ingest_candidates = AsyncMock(side_effect=RuntimeError("DataError"))

    events = [event async for event in runtime.run(_task())]

    assert events, "回合应正常跑完"
    assert any(getattr(e, "type", "") == "done" for e in events), "应以 done 收尾"
