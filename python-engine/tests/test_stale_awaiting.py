"""复核 `abandon_stale_awaiting`：挂起 run 的**唯一**永久悬空出口必须被堵上。

为什么单独盯这条：`awaiting_input` 刻意不在 `ACTIVE_STATUSES` 里，所以
`load()` / `mark()` / `claim()` / `sweep_once` 的谓词在**定义上**都碰不到它。
此前 `expire_stale` 只把 `pending_interactions` 转 `expired`，不碰 `agent_runs`，
于是票过期之后那一行永远停在"等你回答"——不续跑、不失败、也没有任何巡检看得见。
用户回来时界面还显示"等你回答"，而那张票早已取不出来。

这里不去连真 PG（单测跑不起），而是**钉住 SQL 的谓词**：这些条件本身就是语义，
写错一个就等于退回到"永远悬空"。真实执行路径由集成环境覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import checkpoint as ckpt
from app.agent import resume


class _RecordingPool:
    """只记 SQL 与参数的池子；`rowcount` 决定"收回了几行"。"""

    def __init__(self, *, rowcount: int = 1, boom: bool = False) -> None:
        self.rowcount = rowcount
        self.boom = boom
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if self.boom:
            raise RuntimeError("pg is down")
        return f"UPDATE {self.rowcount}"


@pytest.mark.asyncio
async def test_expired_ticket_run_is_abandoned(monkeypatch: Any) -> None:
    pool = _RecordingPool(rowcount=2)
    monkeypatch.setattr(ckpt, "_pool", lambda: pool)

    reclaimed = await ckpt.abandon_stale_awaiting(limit=7)

    assert reclaimed == 2
    assert len(pool.executed) == 1
    sql, args = pool.executed[0]
    # 只动挂在"等人"上的行：别的状态有各自的路，掺进来会误杀正在跑的 run
    assert "r.status = 'awaiting_input'" in sql
    # 判据是**票**，不是快照窗口：票的 TTL 才是"答复还可能到达"的截止时间
    assert "pending_interactions" in sql
    assert "p.expires_at > now()" in sql
    # `answered` 未过期也算"有人会来"——票已答未 settle 的那一刻，答复正在路上
    assert "'pending', 'answered'" in sql
    # 结果是可见放弃，不是悄悄删行
    assert "SET status = 'abandoned'" in sql
    assert args == (7,)


@pytest.mark.asyncio
async def test_no_pool_is_noop(monkeypatch: Any) -> None:
    monkeypatch.setattr(ckpt, "_pool", lambda: None)

    assert await ckpt.abandon_stale_awaiting() == 0


@pytest.mark.asyncio
async def test_pool_error_degrades(monkeypatch: Any) -> None:
    # 回收失败不该连累 run 巡检：sweep_once 里这个调用被 try 包着，返回 0 即可
    monkeypatch.setattr(ckpt, "_pool", lambda: _RecordingPool(boom=True))

    assert await ckpt.abandon_stale_awaiting() == 0


@pytest.mark.asyncio
async def test_sweep_records_abandoned_count(monkeypatch: Any) -> None:
    """接线：`sweep_once` 必须真的调用它，并把条数放进 stats。"""

    async def _fake_abandon(*, limit: int = 50) -> int:
        return 3

    async def _fake_expire(*, limit: int = 50) -> int:
        return 1

    monkeypatch.setattr(ckpt, "abandon_stale_awaiting", _fake_abandon)
    monkeypatch.setattr(ckpt, "_pool", lambda: None)
    # `expire_stale` 走的是 interactions 模块的池子，这里直接短路
    from app.agent import interactions as pending_interactions

    monkeypatch.setattr(pending_interactions, "expire_stale", _fake_expire)

    stats = await resume.sweep_once()

    assert stats["awaiting_abandoned"] == 3
    assert stats["pending_expired"] == 1


@pytest.mark.asyncio
async def test_abandon_failure_does_not_stop_sweep(monkeypatch: Any) -> None:
    async def _boom(*, limit: int = 50) -> int:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(ckpt, "abandon_stale_awaiting", _boom)
    monkeypatch.setattr(ckpt, "_pool", lambda: None)
    from app.agent import interactions as pending_interactions

    async def _fake_expire(*, limit: int = 50) -> int:
        return 0

    monkeypatch.setattr(pending_interactions, "expire_stale", _fake_expire)

    # 不抛：巡检失败一次只该记日志，不该让整轮节拍停摆
    stats = await resume.sweep_once()
    assert stats["awaiting_abandoned"] == 0
