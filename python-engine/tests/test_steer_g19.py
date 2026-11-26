"""G19：父 → 子 steer（父 Agent 给运行中的子 Agent 追加指令）。

**修前会怎样**：父 Agent 想把一句追加约束送到**正在跑**的子 Agent 手里，唯一的通道是
`/v1/agent/steer`（按**会话**寻址）。而子 Agent 跑在合成会话 `sub:<父>:<run>` 里、
**不在 `run_registry` 里**（那里登记的是顶层回合的归属），于是准入校验直接回
``no active agent for this session`` —— 一句"先跑单测再改"永远送不进去。

本文件的判据：

1. `steer(run_id, message)` 能在子 Agent 的**最近步边界**写入一条消息，且**不打断**正在
   执行的工具调用（只入队，不 cancel、不注入到工具调用中间）；
2. 消息带**发送者**（父 Agent id），子 Agent **下一轮**从既有 steer 通道取到；
3. 目标不活跃 / 不存在 / 已结束 ⇒ **明确错误码**（绝不静默成功）；
4. 同一 run 并发 steer **不丢**：有界队列，溢出**明确报错**。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent import steer as steer_mod
from app.subagent import registry as subagent_registry


class _Redis:
    """够 steer 用即可：计数、list、rename（与 tests/test_steer.py 同形状）。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.counters: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if stop == -1 else items[start : stop + 1]

    async def rename(self, src: str, dst: str) -> None:
        if src not in self.lists:
            raise RuntimeError("no such key")
        self.lists[dst] = self.lists.pop(src)

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        return items[start:] if stop == -1 else items[start : stop + 1]

    async def delete(self, key: str) -> None:
        self.lists.pop(key, None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """把模块级状态清干净：队列、run→会话映射、已结束集合、每 run 计数。

    `_resolve_redis(None)` 会去连 Redis；本文件一律显式传 `redis=`，并把缺省解析换成
    "没有 Redis" —— 用例不依赖外部服务，也就不受它们抖动影响。
    """

    async def _no_redis(redis: Any = None) -> Any:
        return redis

    monkeypatch.setattr(steer_mod, "_resolve_redis", _no_redis)
    steer_mod._LOCAL.clear()
    steer_mod._GENERATION.clear()
    steer_mod._RUN_TARGETS.clear()
    steer_mod._RUN_BY_SESSION.clear()
    steer_mod._FINISHED.clear()
    steer_mod._PENDING.clear()
    yield
    steer_mod._LOCAL.clear()
    steer_mod._GENERATION.clear()
    steer_mod._RUN_TARGETS.clear()
    steer_mod._RUN_BY_SESSION.clear()
    steer_mod._FINISHED.clear()
    steer_mod._PENDING.clear()


CHILD_SESSION = "sub:parent-session:rs_1"


def _register(run_id: str = "rs_1", *, parent: str = "parent-session") -> None:
    steer_mod.register_child_run(
        run_id, child_session_id=f"sub:{parent}:{run_id}", parent_agent_id=parent
    )


# ── 1. 父 → 子：能送到、带发送者、下一轮生效 ────────────────────────────────


@pytest.mark.asyncio
async def test_父能向运行中的子写一条带发送者的指令() -> None:
    _register()
    redis = _Redis()

    result = await steer_mod.steer("rs_1", "先跑单测，再改代码", sender="parent-session",
                                   redis=redis)
    assert result.ok is True
    assert result.code == steer_mod.CODE_OK
    assert result.child_session == CHILD_SESSION

    # 子 Agent 下一轮开头从**既有** steer 通道取（runtime 的 drain_steer），
    # 取到的文本要带发送者名，来源标成 parent:<id>（历史里分得清是谁说的）。
    drained = await steer_mod.drain_steer(CHILD_SESSION, redis=None)
    assert len(drained) == 1
    text, source = drained[0]
    assert text.endswith("先跑单测，再改代码")
    assert "父 Agent" in text and "parent-session" in text
    assert source == "parent:parent-session"


@pytest.mark.asyncio
async def test_steer不打断正在执行的工具调用() -> None:
    """只入队；不许取消 run、不许碰正在执行的任务（工具调用会与 tool_calls 错位）。"""
    _register()
    working = asyncio.create_task(asyncio.sleep(30))

    def _boom(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("steer 不得取消正在跑的子 Agent")

    real_cancel = subagent_registry.cancel
    subagent_registry.cancel = _boom  # type: ignore[assignment]
    try:
        result = await steer_mod.steer("rs_1", "换个方向", sender="parent-session",
                                       redis=_Redis())
        assert result.ok is True
        assert not working.done(), "在跑的任务必须还在跑"
    finally:
        subagent_registry.cancel = real_cancel  # type: ignore[assignment]
        working.cancel()
        with pytest.raises(asyncio.CancelledError):
            await working


# ── 2. 目标不存在 / 已结束 ⇒ 明确错误码 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_未知run返回明确的run_not_found() -> None:
    result = await steer_mod.steer("rs_不存在", "喂", sender="p", redis=_Redis())
    assert result.ok is False
    assert result.code == steer_mod.CODE_RUN_NOT_FOUND
    assert result.reason


@pytest.mark.asyncio
async def test_已结束的run返回run_finished而不是静默成功() -> None:
    _register()
    steer_mod.forget_child_run("rs_1")

    result = await steer_mod.steer("rs_1", "还在吗", sender="p", redis=_Redis())
    assert result.ok is False
    assert result.code == steer_mod.CODE_RUN_FINISHED


@pytest.mark.asyncio
async def test_空指令与超长指令被拒() -> None:
    _register()
    empty = await steer_mod.steer("rs_1", "   ", sender="p", redis=_Redis())
    assert (empty.ok, empty.code) == (False, steer_mod.CODE_INVALID_MESSAGE)

    too_long = await steer_mod.steer(
        "rs_1", "x" * (steer_mod.MAX_CHARS + 1), sender="p", redis=_Redis()
    )
    assert (too_long.ok, too_long.code) == (False, steer_mod.CODE_INVALID_MESSAGE)
    assert await steer_mod.drain_steer(CHILD_SESSION, redis=None) == []


# ── 3. 并发安全：有界队列，溢出明确报错，已收下的不丢 ───────────────────────


@pytest.mark.asyncio
async def test_并发steer不丢且顺序稳定() -> None:
    _register()
    redis = _Redis()
    messages = [f"第{i}条指令" for i in range(steer_mod.MAX_CHILD_STEER_PER_RUN)]

    results = await asyncio.gather(
        *(
            steer_mod.steer("rs_1", message, sender="p", redis=redis)
            for message in messages
        )
    )
    assert all(result.ok for result in results), [r.code for r in results]

    drained = await steer_mod.drain_steer(CHILD_SESSION, redis=None)
    assert [text.split("\n")[-1] for text, _source in drained] == messages


@pytest.mark.asyncio
async def test_队列满时明确报错而不是静默丢弃() -> None:
    _register()
    redis = _Redis()
    cap = steer_mod.MAX_CHILD_STEER_PER_RUN

    for index in range(cap):
        result = await steer_mod.steer("rs_1", f"指令{index}", sender="p", redis=redis)
        assert result.ok is True, result.code

    overflow = await steer_mod.steer("rs_1", "再来一条", sender="p", redis=redis)
    assert overflow.ok is False
    assert overflow.code == steer_mod.CODE_QUEUE_FULL
    assert str(cap) in overflow.reason

    # 已经收下的一条都没丢
    drained = await steer_mod.drain_steer(CHILD_SESSION, redis=None)
    assert len(drained) == cap


@pytest.mark.asyncio
async def test_子取走之后可以继续steer() -> None:
    """有界队列算的是**待注入**条数：子 Agent 取走一轮之后额度要还回来。"""
    _register()
    redis = _Redis()
    cap = steer_mod.MAX_CHILD_STEER_PER_RUN

    for index in range(cap):
        assert (await steer_mod.steer("rs_1", f"a{index}", sender="p", redis=redis)).ok is True
    assert len(await steer_mod.drain_steer(CHILD_SESSION, redis=None)) == cap

    again = await steer_mod.steer("rs_1", "第二轮指令", sender="p", redis=redis)
    assert again.ok is True, again.code


# ── 4. 注册 / 注销：映射由 runner 维护，注销后不留悬空目标 ──────────────────


def test_注册与注销的映射是幂等且可查的() -> None:
    _register("rs_2", parent="p2")
    assert steer_mod.child_session_of("rs_2") == "sub:p2:rs_2"
    assert steer_mod.parent_of("rs_2") == "p2"

    steer_mod.forget_child_run("rs_2")
    steer_mod.forget_child_run("rs_2")  # 幂等
    assert steer_mod.child_session_of("rs_2") == ""


@pytest.mark.asyncio
async def test_注销后清掉残留的待注入条目() -> None:
    """子 Agent 收尾后残留的条目不得被下一次同会话的 run 取走（跨 run 误伤）。"""
    _register()
    await steer_mod.steer("rs_1", "这条来不及了", sender="p", redis=_Redis())
    steer_mod.forget_child_run("rs_1")
    assert await steer_mod.drain_steer(CHILD_SESSION, redis=None) == []
