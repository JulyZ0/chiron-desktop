"""G-T3：按 realpath 的**执行期**串行队列（`app/tools/file_queue.py`）回归测试。

**这里钉住什么**

同一个文件同一时刻只能有一个写者在写 —— 无论这两个写者来自哪个入口。
现有的两道防线都盖不住这一点：

* `write_claim` 是**租约**（TTL + owner）：它让后到者**被拒**，不让它们**排队**。
  模型收到一条 error 只好重试，重试时还是同一个竞态。
* `_split_tool_batch` 是**事前静态判定**：只看得见同一批里的重复路径，
  看不见批外的并发（主 Agent 与子 Agent 同时写、`run_code` 嵌套与主循环同时写、
  API 直连与对话同时写）。

对位 pi 的 `withFileMutationQueue`。下面几条一旦回退，静默覆盖就会回来：
两个写者都报成功，最终内容取决于完成顺序，而**没有任何一处报错**。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from app.tools import file_queue
from app.tools.registry import ToolRegistry, registry


@pytest.fixture(autouse=True)
def _clean_queue():
    file_queue.reset()
    yield
    file_queue.reset()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 `safe_join` 的根指到 tmp_path，让 realpath 解析可预测。"""
    from app.tools import sandbox

    def _safe_join(rel: str) -> Path:
        base = tmp_path.resolve()
        target = (base / str(rel or "")).resolve()
        if not str(target).startswith(str(base)):
            raise ValueError(f"path escapes sandbox: {rel}")
        return target

    monkeypatch.setattr(sandbox, "safe_join", _safe_join)
    return tmp_path


# ── key 归一 ────────────────────────────────────────────


def test_same_file_different_spellings_share_one_key(workspace: Path):
    """`a.txt` / `./a.txt` / `sub/../a.txt` / 绝对路径 是**同一个文件** —— 必须同一条队。

    按书写形态做 key 等于"每个写法各排一条队"，一条也排不上。
    """
    keys = {
        file_queue.mutation_key("write_file", {"path": p})
        for p in ("a.txt", "./a.txt", "sub/../a.txt", str(workspace / "a.txt"))
    }

    assert len(keys) == 1
    assert keys.pop()  # 非空


def test_key_is_normcased_for_windows_case(workspace: Path):
    """Windows 上大小写不敏感：`A.TXT` 与 `a.txt` 是同一个文件。"""
    upper = file_queue.mutation_key("write_file", {"path": "A.TXT"})
    lower = file_queue.mutation_key("write_file", {"path": "a.txt"})

    assert upper == lower == os.path.normcase(upper)


def test_different_files_have_different_keys(workspace: Path):
    a = file_queue.mutation_key("write_file", {"path": "a.txt"})
    b = file_queue.mutation_key("write_file", {"path": "b.txt"})

    assert a and b and a != b


def test_read_tools_are_not_serialized(workspace: Path):
    """读工具不进队 —— 否则并行读会被一个毫无必要的锁串起来。"""
    assert file_queue.mutation_key("read_file", {"path": "a.txt"}) == ""
    assert file_queue.mutation_key("grep_files", {"root": "."}) == ""


def test_alternative_path_field_names_are_recognized(workspace: Path):
    """`edit_file` / `delete_file` 用的字段名与 `write_file` 不完全一致。"""
    assert file_queue.mutation_key("delete_file", {"file_path": "a.txt"})
    assert file_queue.mutation_key("edit_file", {"filename": "a.txt"})


def test_unresolvable_path_does_not_serialize(workspace: Path):
    """路径逃出工作区时不裁决 —— 让 `safe_join` 报那个更准确的错。"""
    assert file_queue.mutation_key("write_file", {"path": "../escape.txt"}) == ""


def test_missing_path_does_not_serialize(workspace: Path):
    assert file_queue.mutation_key("write_file", {}) == ""


# ── 排队语义 ────────────────────────────────────────────


def test_same_path_waits_for_the_first_writer(workspace: Path):
    """核心断言：第二个写者在第一个写完之前**进不去**。"""

    async def scenario() -> list[str]:
        order: list[str] = []
        release = asyncio.Event()

        async def first() -> None:
            async with file_queue.serialized("write_file", {"path": "a.txt"}):
                order.append("first-in")
                await release.wait()
                order.append("first-out")

        async def second() -> None:
            while "first-in" not in order:
                await asyncio.sleep(0)
            async with file_queue.serialized("write_file", {"path": "a.txt"}):
                order.append("second-in")

        t1 = asyncio.create_task(first())
        t2 = asyncio.create_task(second())
        # 给第二个充分的机会去抢 —— 它必须抢不到
        await asyncio.sleep(0.05)
        assert order == ["first-in"], f"第二个写者在第一个还没写完时就进去了：{order}"
        release.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
        return order

    assert asyncio.run(scenario()) == ["first-in", "first-out", "second-in"]


def test_different_paths_do_not_block_each_other(workspace: Path):
    """不同文件必须能同时写 —— 否则整个写通道被一把全局锁串死。"""

    async def scenario() -> list[str]:
        order: list[str] = []
        release = asyncio.Event()

        async def hold(path: str, tag: str) -> None:
            async with file_queue.serialized("write_file", {"path": path}):
                order.append(f"{tag}-in")
                await release.wait()
                order.append(f"{tag}-out")

        t1 = asyncio.create_task(hold("a.txt", "a"))
        t2 = asyncio.create_task(hold("b.txt", "b"))
        await asyncio.sleep(0.05)
        # 两个都进去了 ⇒ 粒度对了
        assert order == ["a-in", "b-in"], f"不同文件的写者被串行了：{order}"
        release.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5)
        return order

    assert sorted(asyncio.run(scenario())) == ["a-in", "a-out", "b-in", "b-out"]


def test_lock_is_released_when_handler_raises(workspace: Path):
    """写失败也必须放锁 —— 否则这个文件从此再也写不进去。"""

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with file_queue.serialized("write_file", {"path": "a.txt"}):
                raise RuntimeError("boom")
        # 还能再进：说明上一把锁确实释放了
        async with file_queue.serialized("write_file", {"path": "a.txt"}):
            pass

    asyncio.run(scenario())


def test_entries_are_cleaned_up(workspace: Path):
    """最后一个使用者离开就删条目：既防字典随写过的文件数无限长，
    也保证 `asyncio.Lock` 不跨 loop 复用。"""

    async def scenario() -> None:
        async with file_queue.serialized("write_file", {"path": "a.txt"}):
            assert file_queue.pending(file_queue.mutation_key("write_file", {"path": "a.txt"})) == 1
        assert file_queue._entries == {}, "条目残留 ⇒ 会无限增长"

    asyncio.run(scenario())


def test_noop_passthrough_is_free():
    """非写工具不建条目 —— 直通路径不能留下任何痕迹。"""
    assert file_queue._entries == {}


# ── registry 集成（真实执行路径）────────────────────────


def _write_tool(sink: list[str], hold: asyncio.Event | None = None) -> ToolRegistry:
    reg = ToolRegistry()

    async def handler(path: str, content: str = "") -> dict[str, Any]:
        sink.append(f"{path}:in")
        if hold is not None:
            await hold.wait()
        sink.append(f"{path}:out")
        return {"path": path, "bytes": len(content)}

    reg.register(
        name="write_file",
        description="w",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path"],
        },
        handler=handler,
    )
    return reg


def test_registry_serializes_concurrent_writes(workspace: Path):
    """`registry.execute` 是所有写入口的唯一汇聚点 —— 并发写在这里被排队。"""
    sink: list[str] = []
    reg = _write_tool(sink)

    async def scenario() -> None:
        # 两个并发调用同一个文件：不能交错
        await asyncio.gather(
            reg.execute("write_file", {"path": "a.txt", "content": "1"}),
            reg.execute("write_file", {"path": "a.txt", "content": "2"}),
        )

    asyncio.run(scenario())
    assert sink == ["a.txt:in", "a.txt:out", "a.txt:in", "a.txt:out"], (
        f"写被交错了：{sink}"
    )


def test_registry_lets_different_files_run_concurrently(workspace: Path):
    sink: list[str] = []
    release = asyncio.Event()
    reg = _write_tool(sink, hold=release)

    async def scenario() -> None:
        task = asyncio.gather(
            reg.execute("write_file", {"path": "a.txt", "content": "1"}),
            reg.execute("write_file", {"path": "b.txt", "content": "2"}),
        )
        # 两个都进去了（都卡在 hold 上）⇒ 证明没被同一把锁串起来
        for _ in range(50):
            if len([s for s in sink if s.endswith(":in")]) == 2:
                break
            await asyncio.sleep(0.01)
        release.set()
        await task

    asyncio.run(scenario())
    assert len([s for s in sink if s.endswith(":in")]) == 2


def test_readonly_tool_is_not_queued(workspace: Path):
    """读工具走的还是直通路径，不能被写队列牵连。"""
    calls: list[str] = []
    reg = ToolRegistry()

    async def handler(path: str) -> dict[str, Any]:
        calls.append(path)
        return {"path": path}

    reg.register(
        name="read_file",
        description="r",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=handler,
    )

    async def scenario() -> None:
        await asyncio.gather(
            reg.execute("read_file", {"path": "a.txt"}),
            reg.execute("read_file", {"path": "a.txt"}),
        )
        assert file_queue._entries == {}

    asyncio.run(scenario())
    assert calls == ["a.txt", "a.txt"]


# ── 口径一致性 ──────────────────────────────────────────


def test_tool_sets_match_write_claim():
    """队列与租约必须**同口径**：覆盖集合不一致就会长出
    "被队列排队的那个没被租约保护"（或反过来）的洞。"""
    from app.agent.write_claim import FILE_WRITE_TOOLS

    assert file_queue.FILE_MUTATION_TOOLS == FILE_WRITE_TOOLS


def test_global_registry_still_works():
    """接线后全局注册表的核心行为不变（工具不存在时仍回结构化错误）。"""
    async def scenario() -> None:
        assert "error" in await registry.execute("__absent__", {})

    asyncio.run(scenario())
