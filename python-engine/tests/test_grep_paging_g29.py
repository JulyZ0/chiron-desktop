"""G29②：`grep_files` 的结果分页（修前**没有**续页参数）。

## 修前会怎样

`grep_files(query, root, glob, max_results)` 只有"一次给多少条"这一个旋钮，
没有任何"从第几条继续"的入口。于是当命中超过字节预算时：

* 模型的唯一出路是换一个**更窄的正则**（更窄 ≠ 更全：可能刚好漏掉要找的那个）；
* 或者把 `max_results` 调大 —— 而真正卡住它的是字节预算，调大命中数没用；
* 两页之间也无法拼回全集：第二页从哪开始、顺序是否与第一页一致，都没有契约。

## 现在的契约（本文件断言的就是这三条）

1. `offset`（0 基）是在**稳定顺序**里的命中序号，不是行号也不是文件序号；
2. 返回 `next_offset` / `eof` / `total`，`next_offset` 按**实际返回条数**推进
   （字节预算会砍掉页尾，按请求的 `max_results` 推进就会跳过被砍掉的那几条）；
3. 顺序确定：**修改时间降序 → 路径升序 → 行号升序**（后端给不出 mtime 时退化为
   路径序）。页面只是这个顺序的窗口，所以"两页拼接 == 全集"逐条成立。
"""

from __future__ import annotations

import re

import pytest

from app.backends.context import set_backend
from app.backends.protocol import FileStat, GlobResult, GrepMatch, GrepResult
from app.tools.core import GREP_MAX_BYTES, GREP_MAX_LINE_CHARS, grep_files


class _StubBackend:
    """内存替身（与 test_grep_over_cap_g29 同形）：命中与 mtime 都是用例的输入。"""

    def __init__(self, files: dict[str, list[str]], mtimes: dict[str, float] | None = None) -> None:
        self.files = {name: list(lines) for name, lines in files.items()}
        self.mtimes = dict(mtimes or {})

    async def grep(self, pattern: str, *, path: str = ".", max_results: int = 100) -> GrepResult:
        regex = re.compile(pattern)
        matches: list[GrepMatch] = []
        for name in sorted(self.files):
            for number, line in enumerate(self.files[name], start=1):
                if not regex.search(line):
                    continue
                if len(matches) >= max_results:
                    return GrepResult(matches=matches, truncated=True, reason="budget")
                matches.append(GrepMatch(path=name, line_number=number, line=line))
        return GrepResult(matches=matches)

    async def stat(self, path: str) -> FileStat | None:
        if path not in self.files:
            return None
        return FileStat(path=path, size=1, is_file=True, mtime=self.mtimes.get(path, 0.0))

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return GlobResult(paths=sorted(self.files))

    def supports_execution(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.delenv("CHIRON_GREP_OVER_CAP", raising=False)
    yield
    set_backend(None)


def _hit_files(count: int) -> tuple[dict[str, list[str]], dict[str, float]]:
    files = {f"f{i:02d}.py": ["needle"] for i in range(count)}
    mtimes = {name: 1000.0 + index for index, name in enumerate(files)}
    return files, mtimes


def _keys(page: dict) -> list[tuple[str, int]]:
    return [(m["path"], m["line"]) for m in page["matches"]]


@pytest.mark.asyncio
async def test_不传_offset_时就是第一页_形态与修前一致():
    """兼容性：既有调用方（不传 `offset`）拿到的仍是"最多 max_results 条"的一页。"""
    files, mtimes = _hit_files(20)
    set_backend(_StubBackend(files, mtimes))

    out = await grep_files("needle", max_results=8)

    assert out["offset"] == 0
    assert out["count"] == 8
    assert out["next_offset"] == 8
    assert out["eof"] is False
    assert out["total"] == 20
    assert set(out["matches"][0]) == {"path", "line", "text"}, "既有字段形状不能变"


@pytest.mark.asyncio
async def test_两页拼接等于全集且顺序一致():
    """分页的全部意义：拼回去必须**逐条**等于全集，顺序不重不漏。

    修前没有任何续页入口，这条断言根本无法表达 —— 模型只能"换个正则再猜一次"。
    """
    files, mtimes = _hit_files(20)
    set_backend(_StubBackend(files, mtimes))

    first = await grep_files("needle", max_results=8)
    second = await grep_files("needle", max_results=8, offset=first["next_offset"])
    third = await grep_files("needle", max_results=8, offset=second["next_offset"])

    joined = _keys(first) + _keys(second) + _keys(third)
    full = await grep_files("needle", max_results=20)

    assert joined == _keys(full), "拼接结果必须与一次性取回的全集逐条相同（顺序也相同）"
    assert len(joined) == 20 == len(set(joined))
    assert third["eof"] is True
    assert first["total"] == second["total"] == third["total"] == 20


@pytest.mark.asyncio
async def test_next_offset_按实际返回条数推进而不是按请求的_limit():
    """字节预算砍掉页尾时，`next_offset` 必须跟着**实际**返回条数走。

    按请求的 `max_results` 推进会静默跳过被砍掉的那几条 —— 模型以为翻完了，
    实际中段整段没看到（这正是 `read_file` 分页踩过的同一个坑）。
    """
    long_lines = [f"needle {i} " + "w" * GREP_MAX_LINE_CHARS for i in range(400)]
    set_backend(_StubBackend({"big.txt": long_lines}))

    first = await grep_files("needle", max_results=10_000)

    assert first["count"] < 400, "字节预算必须仍然生效（先确认这条用例真的被预算卡住了）"
    assert first["next_offset"] == first["count"], (
        f"next_offset 必须等于实际返回条数（{first['count']}），实际 {first['next_offset']}"
    )
    used = sum(len(m["text"].encode()) + 1 for m in first["matches"])
    assert used <= GREP_MAX_BYTES + 1
    # 老调用方依赖的措辞不能消失：它说明"卡住我的是字节，不是命中数"。
    assert "budget" in first["note"]
    assert f"offset={first['next_offset']}" in first["note"], "被预算截断时必须点名续页偏移"


@pytest.mark.asyncio
async def test_字节预算下的两页拼接仍然等于全集():
    """带字节预算时也必须拼得回来 —— 这是"分页可用"的真正判据。"""
    long_lines = [f"needle {i} " + "w" * GREP_MAX_LINE_CHARS for i in range(60)]
    set_backend(_StubBackend({"big.txt": long_lines}))

    collected: list[tuple[str, int]] = []
    offset = 0
    for _ in range(50):  # 防死循环的硬上限：契约要求每次前进
        page = await grep_files("needle", max_results=10_000, offset=offset)
        collected.extend(_keys(page))
        if page["eof"]:
            break
        assert page["next_offset"] > offset, "非 eof 的页必须前进，否则翻页会原地打转"
        offset = page["next_offset"]

    full = await grep_files("needle", max_results=10_000)
    assert collected == _keys(full)
    assert len(collected) == 60


@pytest.mark.asyncio
async def test_超出末尾的_offset_返回空页并标记_eof():
    """翻过尾巴不能报错、不能回绕：返回空页 + `eof`，`next_offset` 归一化到末尾。"""
    files, mtimes = _hit_files(5)
    set_backend(_StubBackend(files, mtimes))

    out = await grep_files("needle", max_results=2, offset=999)

    assert out["count"] == 0
    assert out["matches"] == []
    assert out["eof"] is True
    assert out["next_offset"] == 5 == out["total"], "越界 offset 必须归一到末尾，而不是回绕到头部"


@pytest.mark.asyncio
async def test_负_offset_按第一页处理():
    """`offset` 是模型给的数：负值不报错，按第一页处理（与 `read_file` 一致）。"""
    files, mtimes = _hit_files(5)
    set_backend(_StubBackend(files, mtimes))

    out = await grep_files("needle", max_results=2, offset=-3)

    assert out["offset"] == 0
    assert out["count"] == 2


@pytest.mark.asyncio
async def test_分页顺序按修改时间降序_路径_行号():
    """稳定顺序必须写清并可用：修改时间降序 → 路径升序 → 行号升序。

    为什么不是"路径序"：G29 的默认超上限策略是"保留最新命中"，分页顺序若与
    交付顺序不一致（页内按时间、翻页按路径），拼接就必然错位 —— 顺序只有一份。
    """
    files = {
        "new.py": ["needle", "needle"],  # 同文件内按行号升序
        "old.py": ["needle"],
        "mid.py": ["needle"],
    }
    mtimes = {"new.py": 3000.0, "mid.py": 2000.0, "old.py": 1000.0}
    set_backend(_StubBackend(files, mtimes))

    out = await grep_files("needle", max_results=2)

    assert [(m["path"], m["line"]) for m in out["matches"]] == [("new.py", 1), ("new.py", 2)]
    rest = await grep_files("needle", max_results=2, offset=out["next_offset"])
    assert [(m["path"], m["line"]) for m in rest["matches"]] == [("mid.py", 1), ("old.py", 1)]


@pytest.mark.asyncio
async def test_offset_出现在工具_schema_里():
    """分页参数必须对模型可见 —— 否则能力存在但没人知道怎么用。"""
    import app.tools  # noqa: F401 — 工具注册的唯一入口
    from app.tools.registry import registry

    tool = registry.get("grep_files")
    assert tool is not None
    assert "offset" in tool.parameters["properties"]
