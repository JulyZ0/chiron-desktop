"""G29①：grep 超上限的**显式契约**（修前是默默按扫描顺序截断）。

## 修前会怎样

`grep_files` 命中数超过 `max_results` / 字节预算时，旧实现只有一条路径：

1. 后端按"路径序"流式扫描，凑满 `max_results` 条就 `truncated=True`；
2. 工具层再从池子里按**同一个扫描顺序**截字节预算；
3. trailer 只说"result limit reached (N hits)"，**没说保留的是哪些**。

于是模型看到的是"前 N 条"——而"前"是**扫描顺序**，纯属实现细节。它既不知道
被丢掉的命中里有没有自己真正要找的，也没有任何通道把丢掉的取回来，只能换更窄的
正则重搜（于是把一个"翻页"问题变成"反复重试"问题）。

## 为什么必须**显式**选策略（对齐 DSH `sampleOverCapGlobResults` 的思路）

"超上限时保留哪一批"是**部署方的产品决策**，不是实现细节：

* 改代码/看报错 ⇒ 想要**最新**的（按修改时间）；旧的 `head` 恰恰不是这个语义；
* 通读一个陌生仓库 ⇒ 想要**每个顶层目录都有代表**（均匀抽样），否则第一个
  大目录的命中会吃掉整个配额，后面的目录一条都看不到。

两种都合理，所以默认值必须被写明、可覆盖、且非法值**不许静默回落** ——
"猜一个"等于把错误的批次当成答案交给模型。本文件断言的就是这四条：
默认策略语义、可配置策略语义、非法值报错、以及全量命中仍可取回。
"""

from __future__ import annotations

import json
import os
import re

import pytest

from app.backends.context import set_backend
from app.backends.protocol import FileStat, GlobResult, GrepMatch, GrepResult
from app.tools.core import (
    GREP_COLLECT_MAX_HITS,
    GREP_OVER_CAP_ENV,
    grep_files,
)


class _StubBackend:
    """内存替身：命中与 mtime 都由用例显式给定。

    为什么用替身而不是真文件：本文件的断言对象是"**顺序契约**"——谁排在前面、
    谁被保留。真文件的 mtime 只能精确到文件系统的时间粒度，两个文件常常落在同一
    个时间戳上，"按修改时间取最新"就变成了在测文件系统的舍入行为。替身让 mtime
    成为用例的输入，断言才是在测工具，而不是在测 tmpfs。
    """

    def __init__(self, files: dict[str, list[str]], mtimes: dict[str, float] | None = None) -> None:
        self.files = {name: list(lines) for name, lines in files.items()}
        self.mtimes = dict(mtimes or {})
        self.stored: list[str] = []

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
        import fnmatch

        suffix = pattern.split("/")[-1]
        return GlobResult(paths=sorted(n for n in self.files if fnmatch.fnmatch(n, suffix)))

    def supports_execution(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """默认把策略环境变量清空 —— 否则开发机的环境会悄悄改变本文件的结论。"""
    monkeypatch.delenv(GREP_OVER_CAP_ENV, raising=False)
    monkeypatch.delenv("GREP_OVER_CAP", raising=False)
    yield
    set_backend(None)  # contextvar 不随用例结束消失，必须显式恢复


@pytest.fixture
def captured_store(monkeypatch):
    """截获落盘：本文件要断言"全量命中确实进了既有 result_store，且能取回"。

    真 Redis 在单测里不可用（`result_store.store` 在无 Redis 时返回 False），
    而"落盘成功"恰恰是契约的一部分，所以这里替换掉 store 而不是让它失败。
    """
    stored: dict[str, str] = {}

    async def _store(session_id: str, ref: str, text: str) -> bool:
        stored[ref] = text
        return True

    from app.agent import result_store

    monkeypatch.setattr(result_store, "store", _store)
    return stored


def _files(count: int, *, prefix: str = "f") -> dict[str, list[str]]:
    return {f"{prefix}{i}.py": ["needle"] for i in range(count)}


def _mtimes(count: int, *, prefix: str = "f", step: float = 10.0) -> dict[str, float]:
    """`f0` 最旧、`f{count-1}` 最新 —— 顺序契约才可断言。"""
    return {f"{prefix}{i}.py": 1000.0 + i * step for i in range(count)}


@pytest.mark.asyncio
async def test_默认策略按修改时间保留最新命中并在_trailer_里写明(captured_store):
    """默认 = `head`：取**最新**的命中，且 trailer 必须自陈"按修改时间保留前 N 条"。

    修前：保留哪 3 条取决于后端扫描顺序（路径序），trailer 只写 "result limit reached"
    —— 模型无法判断自己看到的是不是最新的那批，也无法判断被丢掉的 3 条是什么。
    """
    backend = _StubBackend(_files(6), _mtimes(6))
    set_backend(backend)

    out = await grep_files("needle", max_results=3)

    assert [m["path"] for m in out["matches"]] == ["f5.py", "f4.py", "f3.py"], (
        "超上限时必须保留**最新**的三条（修前保留的是路径序前三条）"
    )
    assert out["total"] == 6
    assert out["truncated"] is True
    over = out["over_cap"]
    assert over["policy"] == "head"
    assert over["total"] == 6
    # 默认策略必须写在 trailer 里：模型读得到"这是最新的一批"，而不是回去猜。
    assert "按修改时间保留前 3 条" in out["note"]

    # 完整列表必须仍可取回：全量 6 条写进了既有 result_store，且 locator 在 trailer 里。
    ref = over["result_ref"]
    assert ref, "超上限必须给出 locator，否则被裁掉的命中再也没有取回通道"
    assert ref in out["note"]
    rows = [json.loads(line) for line in captured_store[ref].splitlines()]
    assert [row["path"] for row in rows] == ["f5.py", "f4.py", "f3.py", "f2.py", "f1.py", "f0.py"], (
        "落盘的必须是**全量**命中，且按同一顺序 —— 否则取回的内容与翻页对不上"
    )


@pytest.mark.asyncio
async def test_sample_策略按顶层目录均匀抽样(captured_store, monkeypatch):
    """可配置策略 `sample`：每个顶层目录都有代表，而不是被第一个大目录吃光配额。

    为什么这条必须存在：`head`（按修改时间）在"通读陌生仓库"场景下是**错的** ——
    刚 clone 的仓库所有文件 mtime 几乎相同，`head` 退化成路径序，第一个目录的命中
    会占满配额。抽样策略让"每个模块先看一眼"成为可能。
    """
    monkeypatch.setenv(GREP_OVER_CAP_ENV, "sample")
    files: dict[str, list[str]] = {}
    mtimes: dict[str, float] = {}
    for index, top in enumerate(("alpha", "beta", "gamma")):
        for row in range(4):
            name = f"{top}/f{row}.py"
            files[name] = ["needle"]
            # alpha 整体最新：若走 head，前 6 条会全是 alpha —— 抽样必须打散这一点。
            mtimes[name] = 2000.0 + index * 100 + row
    set_backend(_StubBackend(files, mtimes))

    out = await grep_files("needle", max_results=6)

    tops = [m["path"].split("/")[0] for m in out["matches"]]
    assert out["count"] == 6
    assert set(tops) == {"alpha", "beta", "gamma"}, f"抽样必须覆盖每个顶层目录，实际：{tops}"
    assert {tops.count(top) for top in tops} == {2}, f"每个目录应当拿到均等的份额，实际：{tops}"
    assert out["over_cap"]["policy"] == "sample"
    assert "按顶层目录均匀抽样保留前 6 条" in out["note"]

    # 抽样策略下**翻页仍须拼回全集**：抽样决定的是"谁先来"，不是"谁能被看到"。
    second = await grep_files("needle", max_results=6, offset=out["next_offset"])
    assert second["next_offset"] == 12 and second["eof"] is True
    joined = [m["path"] for m in out["matches"] + second["matches"]]
    assert sorted(joined) == sorted(files), "两页拼起来必须正好是全部 12 条，不重不漏"
    assert second["over_cap"]["policy"] == "sample"
    assert "按同一顺序续页" in second["note"], "续页的 trailer 要说清这是同一顺序的第几页"


@pytest.mark.asyncio
async def test_非法策略值必须报错而不是默默回落(captured_store, monkeypatch):
    """部署方写了无法识别的策略值 ⇒ **点名报错**，不许猜一个继续跑。

    "默默选一个"正是 G29 要消灭的东西：一旦回落，部署方以为自己换成了抽样，
    实际仍在按修改时间取最新，而这种偏差不会出现在任何日志里。
    """
    monkeypatch.setenv(GREP_OVER_CAP_ENV, "newest-ish")
    set_backend(_StubBackend(_files(6), _mtimes(6)))

    out = await grep_files("needle", max_results=3)

    assert "error" in out, "非法策略值必须成为一条结构化错误，而不是被当成 head 用"
    assert GREP_OVER_CAP_ENV in out["error"]
    assert "head" in out["error"] and "sample" in out["error"], "错误里必须列出合法取值"
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_未超上限时不落盘也不加_trailer(captured_store):
    """没超上限就别加噪音：正常结果的观感必须与修前一致。"""
    set_backend(_StubBackend(_files(2), _mtimes(2)))

    out = await grep_files("needle")

    assert out["count"] == 2
    assert "truncated" not in out, "没被截断就不该出现 truncated —— 否则每个正常结果都多一层噪音"
    assert "over_cap" not in out
    assert "note" not in out
    assert captured_store == {}, "没超上限不该付落盘的代价"


@pytest.mark.asyncio
async def test_后端不给修改时间时退化为路径序并不假装按时间():
    """后端没有 mtime（远端 / FileStore）时，顺序退化为路径序，且**不谎称**按时间。

    契约不能承诺一个后端给不出来的东西：`mtime` 全为 0 时若还写"按修改时间保留"，
    模型就会把路径序当成时间序来推理（比如"改最近的三个文件"）。
    """
    set_backend(_StubBackend(_files(4), {}))

    out = await grep_files("needle", max_results=2)

    assert [m["path"] for m in out["matches"]] == ["f0.py", "f1.py"]
    assert out["over_cap"]["policy"] == "head"
    assert out["over_cap"]["mtime_available"] is False
    assert "按路径序" in out["note"]
    assert "按修改时间保留" not in out["note"]


@pytest.mark.asyncio
async def test_扫描上限也要如实说明(captured_store):
    """收集上限（`GREP_COLLECT_MAX_HITS`）是另一条隐式边界，同样不能沉默。

    修前这条边界就是 `max_results` 本身，且理由不说；现在收集上限与交付上限分开，
    一旦撞上就必须告诉模型"还有更多命中没被扫到"，并指路（缩小 root/glob）。
    """
    many = [f"needle {i}" for i in range(GREP_COLLECT_MAX_HITS + 50)]
    set_backend(_StubBackend({"big.txt": many}))

    out = await grep_files("needle", max_results=5)

    assert out["scan_truncated"] is True
    assert "扫描上限" in out["note"]
    assert str(GREP_COLLECT_MAX_HITS) in out["note"]


@pytest.mark.asyncio
async def test_本地后端真实文件也按修改时间取最新():
    """端到端：真实 `LocalWorkspaceBackend` 必须把 mtime 交上来（否则策略形同虚设）。

    替身测试只能证明工具层"会按 mtime 排序"；mtime 若在后端没被填出来，生产里
    每条命中都是 0.0，策略就静默退化成路径序 —— 也就是修前的行为。
    """
    import tempfile
    from pathlib import Path

    from app.backends.local import LocalWorkspaceBackend
    from app.tools.context import get_all, restore_context, set_tool_context

    snapshot = get_all()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for index in range(4):
            target = root / f"old{index}.py"
            target.write_text("needle\n", encoding="utf-8")
            # 显式拉开修改时间：文件系统的时间粒度不该决定本用例的结论。
            stamp = 1_600_000_000 + index * 3600
            os.utime(target, (stamp, stamp))
        set_tool_context(project_root=str(root))
        set_backend(LocalWorkspaceBackend())
        try:
            out = await grep_files("needle", max_results=2)
        finally:
            restore_context(snapshot)
            set_backend(None)

    assert [Path(m["path"]).name for m in out["matches"]] == ["old3.py", "old2.py"], (
        "真实后端的 mtime 必须传上来，否则默认策略在生产里退化成路径序"
    )
