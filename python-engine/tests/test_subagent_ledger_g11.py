"""G11：子 Agent 的写入必须并入**父**账本（否则父会话的「整轮 /undo」看不见它们）。

**修前会怎样**（本文件的第一批断言在修前全部失败）：``undo_stack`` 与
``side_effect_ledger`` 的键都是 ``get_session_id()``，而子 Agent 跑在
``sub:<父>:<run>`` 这个**合成会话**里（见 ``app/agent/subagent_runner.py`` 构造
``AgentTask`` 的那一行）。于是子 Agent 写过的文件全部落在 ``undo:sub:<父>:<run>``
这个**没有任何人会去弹**的栈上：父会话敲 ``/undo`` 只回滚父自己的写入，子 Agent 的改动
留在工作区 —— 而用户以为撤了（比"没有 /undo"更危险，见 ``docs/agent-safety-and-reliability.md``）。

**为什么这么改**：不新造机制，只在 ``push`` / ``record`` 里按 **lineage** 把同一条快照
**镜像**到祖先会话的键上（``sub:A:B`` → ``A``；嵌套委派 → 每一级祖先），并用
``(path, at, tool, 内容)`` 指纹去重 —— 同一次写入不能在同一个栈里出现两条
（否则"撤销一次"只是把内容换成同一份，而它后面的那一步被白白吃掉）。

四条不变式（对应任务书 ①②③④）：

1. 子写一次 ⇒ 父 ``/undo`` 能恢复该文件；
2. 父子各写一次同一文件 ⇒ 撤销两次回到最初内容，且**先回滚子**（后进先出）；
3. 写入失败 / 被拒的调用**不进**账本（不占用户的一次撤销）；
4. 没有父会话（顶层 run）⇒ 行为与修前**逐字一致**（单键、单条）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.agent import side_effect_ledger, undo_stack
from app.agent.side_effect_ledger import ROLLBACK_AUTO, SideEffect
from app.agent.undo_stack import Snapshot


class _FakeRedis:
    """只实现两个模块用到的 list 命令。

    ``lrem`` 是 G11 新增的：写入失败/被拒后要把那条**已经压进去**的快照从栈里摘掉
    （否则父会话的下一次 ``/undo`` 会先"恢复"一件根本没发生的事）。
    """

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self.lists.get(key, [])[: end + 1]

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def lpop(self, key: str) -> str | None:
        items = self.lists.get(key, [])
        return items.pop(0) if items else None

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def lrem(self, key: str, count: int, value: str) -> int:
        items = self.lists.get(key, [])
        removed = 0
        kept: list[str] = []
        for item in items:
            if item == value and (count == 0 or removed < count):
                removed += 1
                continue
            kept.append(item)
        self.lists[key] = kept
        return removed


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _get_redis() -> _FakeRedis:
        return fake

    monkeypatch.setattr("app.redis_client.get_redis", _get_redis)
    return fake


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("app.tools.sandbox.workspace_dir", lambda: tmp_path)
    return tmp_path


def _stack(fake: _FakeRedis, session_id: str) -> list[str]:
    return list(fake.lists.get(undo_stack._key(session_id), []))  # noqa: SLF001 - 断言要看键


def _ledger(fake: _FakeRedis, session_id: str) -> list[str]:
    return list(fake.lists.get(side_effect_ledger._key(session_id), []))  # noqa: SLF001


# ── 0. lineage 解析 ────────────────────────────────────────────────────────


def test_lineage_解析子会话的祖先链() -> None:
    """``sub:<父>:<run>`` ⇒ 父；嵌套委派 ⇒ 每一级祖先（由近到远）。"""
    assert undo_stack.lineage_sessions("s1") == []
    assert undo_stack.lineage_sessions("sub:s1:run1") == ["s1"]
    # 父自己的 id 里也带 `sub:` 时不能按第一个冒号切（那会切出 "sub" 这个假会话）
    assert undo_stack.lineage_sessions("sub:sub:s1:r1:r2") == ["sub:s1:r1", "s1"]
    assert undo_stack.lineage_sessions("") == []
    # 只有前缀、没有父：不是 lineage，别凭空造一个键
    assert undo_stack.lineage_sessions("sub:") == []


# ── 1. 子写一次 ⇒ 父 /undo 能恢复 ─────────────────────────────────────────


async def test_子写一次后父会话的undo能恢复该文件(fake_redis: _FakeRedis, workspace: Path) -> None:
    target = workspace / "a.txt"
    target.write_text("before", encoding="utf-8")

    note = await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")
    assert note == ""
    target.write_text("after-by-child", encoding="utf-8")

    # 父会话的 /undo（它只认自己的 session id）
    message, snapshot = await undo_stack.pop_and_restore("s1")

    assert "restored" in message, message
    assert target.read_text(encoding="utf-8") == "before"
    assert snapshot is not None
    assert _stack(fake_redis, "s1") == [], "父栈被撤销后应当为空"


async def test_子会话自己那一份快照仍然保留(fake_redis: _FakeRedis, workspace: Path) -> None:
    """镜像**不是搬迁**：子会话自己的栈也留着（两个键各自独立，互不改写）。"""
    target = workspace / "b.txt"
    target.write_text("v1", encoding="utf-8")

    await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")

    assert len(_stack(fake_redis, "s1")) == 1
    assert len(_stack(fake_redis, "sub:s1:run1")) == 1


# ── 2. 父子各写一次 ⇒ 撤销两次回到最初，且先回滚子（LIFO）──────────────────


async def test_父子各写一次后撤销两次回到最初内容且顺序正确(
    fake_redis: _FakeRedis, workspace: Path
) -> None:
    target = workspace / "c.txt"
    original = "v0"
    target.write_text(original, encoding="utf-8")

    # 父先写（快照 = v0）
    await undo_stack.snapshot_before_write("s1", target, "write_file")
    target.write_text("v1-parent", encoding="utf-8")

    # 子再写（快照 = v1-parent）—— 它落在父之后，撤销时必须**先**回滚它
    await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")
    target.write_text("v2-child", encoding="utf-8")

    first, _ = await undo_stack.pop_and_restore("s1")
    assert "restored" in first, first
    assert target.read_text(encoding="utf-8") == "v1-parent", "第一步必须撤销子 Agent 的那次写入"

    second, _ = await undo_stack.pop_and_restore("s1")
    assert "restored" in second, second
    assert target.read_text(encoding="utf-8") == original, "第二次撤销回到最初内容"

    assert _stack(fake_redis, "s1") == []


async def test_子写新建的文件被父撤销时删除(fake_redis: _FakeRedis, workspace: Path) -> None:
    """子 Agent 新建的文件（快照 existed=False）⇒ 父 /undo 应当删掉它。"""
    target = workspace / "created-by-child.txt"

    await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")
    target.write_text("child made this", encoding="utf-8")

    message, _ = await undo_stack.pop_and_restore("s1")

    assert "removed" in message, message
    assert not target.exists()


# ── 3. 去重：同一条快照不能在一个栈里出现两次 ─────────────────────────────


async def test_同一条快照重复压栈只留一条(fake_redis: _FakeRedis, workspace: Path) -> None:
    """收尾重排队（``pop_and_restore`` 失败会 push 回去）等路径可能重复压同一条。

    不去重的后果：撤销一次只是"恢复成同一份内容"，而它后面那步已经被悄悄吃掉了 ——
    用户按了两次 /undo 却只前进了一步。
    """
    snapshot = Snapshot(
        path=str(workspace / "d.txt"),
        existed=True,
        content="x",
        tool="write_file",
        at=1234.5,
    )

    assert await undo_stack.push("sub:s1:run1", snapshot) is True
    await undo_stack.push("sub:s1:run1", snapshot)

    assert len(_stack(fake_redis, "sub:s1:run1")) == 1
    assert len(_stack(fake_redis, "s1")) == 1, "镜像也只会有一条"


async def test_同一文件的不同时间快照都要保留(fake_redis: _FakeRedis, workspace: Path) -> None:
    """去重按**指纹**（路径 + 时间 + 工具 + 内容），不是按路径 ——
    父子对同一文件的两次写入是两件不同的事，必须都能撤销。"""
    target = workspace / "e.txt"
    target.write_text("v0", encoding="utf-8")
    await undo_stack.snapshot_before_write("s1", target, "write_file")
    target.write_text("v1", encoding="utf-8")
    await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")

    assert len(_stack(fake_redis, "s1")) == 2


# ── 4. 顶层会话：行为与修前一致 ────────────────────────────────────────────


async def test_没有父会话时只写自己的键(fake_redis: _FakeRedis, workspace: Path) -> None:
    target = workspace / "f.txt"
    target.write_text("v0", encoding="utf-8")

    await undo_stack.snapshot_before_write("s1", target, "write_file")

    assert sorted(fake_redis.lists) == [undo_stack._key("s1")], (
        "顶层会话不该凭空多出任何镜像键"
    )
    assert len(_stack(fake_redis, "s1")) == 1


# ── 5. 副作用账本：父会话也能看见子 Agent 的账 ────────────────────────────


async def test_子会话的副作用记进父账本且不重复(fake_redis: _FakeRedis) -> None:
    effect = SideEffect(
        kind="file_write",
        tool="write_file",
        target="a.txt",
        rollback=ROLLBACK_AUTO,
        at=42.0,
    )

    await side_effect_ledger.record("sub:s1:run1", effect)
    await side_effect_ledger.record("sub:s1:run1", effect)  # 同一条重复上报

    parent = await side_effect_ledger.recent("s1")
    assert [entry.target for entry in parent] == ["a.txt"], "父会话的账本里要看得见子 Agent 的副作用"
    assert len(_ledger(fake_redis, "sub:s1:run1")) == 1, "自己的那份也去重"


# ── 6. 失败 / 被拒的写入不进账本 ──────────────────────────────────────────


async def test_丢弃失败写的快照会同时清掉父子的栈(fake_redis: _FakeRedis, workspace: Path) -> None:
    target = workspace / "g.txt"
    target.write_text("v0", encoding="utf-8")

    await undo_stack.snapshot_before_write("sub:s1:run1", target, "write_file")
    removed = await undo_stack.discard("sub:s1:run1", str(target))

    assert removed == 2, "子会话与父会话两个栈各摘一条（只摘一半等于没摘）"
    assert await undo_stack.peek("sub:s1:run1") == []
    assert await undo_stack.peek("s1") == [], "父会话不该为一次没发生的写入留一步撤销"


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


def _runner_with_events(monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> Any:
    """用假事件流驱动 SubAgentRunner（与 test_subagent_approval_forward.py 同一手法）。"""
    from app.agent import runtime as runtime_mod
    from app.agent.subagent_runner import SubAgentRunner

    async def _run(self: Any, task: Any):  # noqa: ANN001
        for evt in events:
            yield evt

    monkeypatch.setattr(runtime_mod.AgentRuntime, "run", _run)
    return SubAgentRunner(
        store=_NullStore(),
        gateway=object(),
        parent_session_id="s1",
        parent_run_id="rs_parent",
        tenant_id=1,
        user_id="u1",
    )


def _evt(**kwargs: Any) -> Any:
    from app.agent import runtime as runtime_mod

    return runtime_mod.AgentEvent(**kwargs)


async def test_子Agent失败或受阻的写调用不进账本(
    fake_redis: _FakeRedis, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子 Agent 的 ``write_file`` 失败（或被 guard 拒绝）⇒ 那条**事前**快照必须被摘掉。

    慢一步说清为什么：快照是"写入**前**"拍的，工具随后失败时文件没变。若它留在栈里，
    父会话下一次 ``/undo`` 会"恢复"成同一份内容 —— 用户按一下、什么也没变，而那一步
    真正的撤销机会已经被消耗掉了。
    """
    target = workspace / "h.txt"
    target.write_text("v0", encoding="utf-8")
    child_session = "sub:s1:r-fixed"

    runner = _runner_with_events(
        monkeypatch,
        [
            _evt(
                type="tool_call",
                tool_call_id="tc-w",
                tool_name="write_file",
                tool_arguments=json.dumps({"path": str(target), "content": "x"}),
            ),
            _evt(
                type="tool_result",
                tool_call_id="tc-w",
                tool_name="write_file",
                content="",
                error="permission denied (guard: approval required)",
            ),
        ],
    )
    # 模拟工具在写入前已经拍下的快照（子键 + 爹的镜像）
    await undo_stack.snapshot_before_write(child_session, target, "write_file")
    assert len(_stack(fake_redis, "s1")) == 1

    await runner.run("写个文件", run_id="r-fixed")

    assert await undo_stack.peek(child_session) == [], "失败的那次写入必须从子的栈里消失"
    assert await undo_stack.peek("s1") == [], "父会话更不该为它留一步撤销"


async def test_子Agent成功的写调用保留快照(
    fake_redis: _FakeRedis, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照组：同样的路径，结果没有 error ⇒ 快照**必须留着**（否则真撤销不了了）。"""
    target = workspace / "i.txt"
    target.write_text("v0", encoding="utf-8")
    child_session = "sub:s1:r-ok"

    runner = _runner_with_events(
        monkeypatch,
        [
            _evt(
                type="tool_call",
                tool_call_id="tc-w",
                tool_name="write_file",
                tool_arguments=json.dumps({"path": str(target), "content": "x"}),
            ),
            _evt(type="tool_result", tool_call_id="tc-w", tool_name="write_file", content="ok"),
        ],
    )
    await undo_stack.snapshot_before_write(child_session, target, "write_file")

    await runner.run("写个文件", run_id="r-ok")

    assert len(_stack(fake_redis, child_session)) == 1
    assert len(_stack(fake_redis, "s1")) == 1


async def test_子Agent被guard拒绝的写调用同样不进账本(
    fake_redis: _FakeRedis, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被 guard 拒绝时工具**没有执行**，结果是一个 ``denied`` 载荷（没有 ``error`` 字段）：
    这条形状也必须被识别，否则"被拒"这一类会漏掉。"""
    target = workspace / "j.txt"
    target.write_text("v0", encoding="utf-8")
    child_session = "sub:s1:r-deny"

    runner = _runner_with_events(
        monkeypatch,
        [
            _evt(
                type="tool_call",
                tool_call_id="tc-d",
                tool_name="write_file",
                tool_arguments=json.dumps({"path": str(target), "content": "x"}),
            ),
            _evt(
                type="tool_result",
                tool_call_id="tc-d",
                tool_name="write_file",
                content='{"denied": true, "error": "requires user confirmation"}',
            ),
        ],
    )
    await undo_stack.snapshot_before_write(child_session, target, "write_file")

    await runner.run("写个文件", run_id="r-deny")

    assert await undo_stack.peek("s1") == []
    assert await undo_stack.peek(child_session) == []


# ── 7. 二进制快照必须原样回来（`_decode` 丢掉 content_b64 的回归）──────────


async def test_入栈再出栈后二进制内容逐字节还原(fake_redis: _FakeRedis, workspace: Path) -> None:
    """``push`` → ``pop`` 之后 ``content_b64`` 不能丢：丢了 restore 只能用文本兜底，
    而文本是 ``errors="replace"`` 出来的 —— 撤销会把图片/压缩包写成 U+FFFD。"""
    import base64

    target = workspace / "k.bin"
    original = bytes(range(256))
    target.write_bytes(original)

    await undo_stack.snapshot_before_write("s1", target, "write_file")
    target.write_bytes(b"\x00changed")

    message, _ = await undo_stack.pop_and_restore("s1")

    assert "restored" in message, message
    assert target.read_bytes() == original
    assert base64.b64encode(target.read_bytes()).decode("ascii") == base64.b64encode(original).decode(
        "ascii"
    )
