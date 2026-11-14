"""读取工具的三层预算：行数 → 单行长度 → 总字节。

为什么需要这三条而不是只测一条：读爆上下文的路径不止一种，而"只加行数上限"看起来
已经很像在节制了 ——

1. 文件有一万行 → 行数上限管用；
2. 文件只有三行，但第一行是 2MB 的 minified bundle → **行数上限完全无效**，
   一次 read_file 就把上下文灌满，而模型看到的还只是"第 1 行"，它连"这文件很大"
   都判断不出来；
3. 每行都不算长，但 2000 行加起来仍是 1MB → 需要总字节闸门。

grep 同理但更严重：一次命中 200 行、每行都是长行时，**定位工具自己的产出**就能撑爆
上下文，而它真正要回答的只是"哪个文件第几行匹配到了"。
"""

from __future__ import annotations

import pytest

from app.backends.context import set_backend
from app.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileStat,
    GlobResult,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.tools.core import (
    GREP_MAX_BYTES,
    GREP_MAX_LINE_CHARS,
    READ_FILE_MAX_BYTES,
    READ_FILE_MAX_LINE_CHARS,
    grep_files,
    read_file,
)


class _StubBackend:
    """只实现读取路径的内存后端：本文件的结论只依赖 read/glob/stat。"""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        if path not in self.files:
            return ReadResult(path=path, error=f"file not found: {path}")
        text = self.files[path]
        lines = text.splitlines()
        if limit <= 0:
            return ReadResult(path=path, content=text, total_lines=len(lines), limit=0)
        return ReadResult(
            path=path,
            content="\n".join(lines[offset : offset + limit]),
            total_lines=len(lines),
            offset=offset,
            limit=limit,
            truncated=(offset + limit) < len(lines),
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        return ReadBytesResult(path=path, data=self.files.get(path, "").encode())

    async def stat(self, path: str) -> FileStat | None:
        if path not in self.files:
            return None
        return FileStat(path=path, size=len(self.files[path].encode()), is_file=True)

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        return GlobResult(paths=sorted(self.files))

    async def write(self, path: str, content: str) -> WriteResult:
        self.files[path] = content
        return WriteResult(path=path, bytes_written=len(content.encode()))

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        return EditResult(path=path, occurrences=1)

    async def delete(self, path: str) -> DeleteResult:
        self.files.pop(path, None)
        return DeleteResult(path=path, deleted=True)

    async def ls(self, path: str = ".") -> LsResult:
        return LsResult(entries=sorted(self.files))

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        return GrepResult()

    def supports_execution(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _restore_default_backend():
    yield
    set_backend(None)


def _install(files: dict[str, str]) -> _StubBackend:
    backend = _StubBackend(files)
    assert isinstance(backend, BackendProtocol)
    set_backend(backend)
    return backend


@pytest.mark.asyncio
async def test_one_huge_line_cannot_blow_the_context():
    """核心失败模式：一行就够大。这里行数上限完全不起作用。"""
    huge = "x" * (READ_FILE_MAX_LINE_CHARS * 40)
    _install({"bundle.js": huge})

    out = await read_file("bundle.js")

    assert len(out["content"]) < len(huge) / 4, (
        "a single oversized line must be clipped; row count alone does not protect"
    )
    assert "line truncated" in out["content"]
    # 必须标注**真实**长度：模型要知道自己被截了，否则会把半行当成完整语句去改。
    assert str(len(huge)) in out["content"]


@pytest.mark.asyncio
async def test_total_bytes_cap_stops_the_window_and_names_the_offset():
    """行数没到上限、单行也不算长，合计仍可能过大 —— 第三层闸门兜住它。"""
    line = "y" * 200
    _install({"big.txt": "\n".join([line] * 2000)})

    out = await read_file("big.txt")

    assert out["truncated"] is True
    assert len(out["content"].encode()) <= READ_FILE_MAX_BYTES + 400  # 尾部提示占一点
    # 必须告诉模型从哪继续，否则它只能整份重读。
    assert "offset=" in out["content"]
    assert "byte budget reached" in out["content"]


@pytest.mark.asyncio
async def test_offset_advances_by_lines_actually_returned():
    """续读偏移要按**实际返回的行数**算，不能按请求的 limit 算。

    按 limit 算会跳过被字节闸门砍掉的那部分 —— 模型以为读完了，实际中段整段没看到。
    """
    _install({"big.txt": "\n".join(["z" * 200] * 2000)})

    first = await read_file("big.txt")
    marker = "[byte budget reached"
    tail = first["content"].split(marker, 1)[1]
    next_offset = int(tail.split("offset=", 1)[1].split(" ", 1)[0])

    returned = len(first["content"].split(marker)[0].splitlines())
    assert next_offset == returned, (
        f"offset must advance by returned lines ({returned}), got {next_offset}"
    )


@pytest.mark.asyncio
async def test_normal_file_is_untouched():
    """预算不能改变正常文件的观感 —— 否则每处读都多一行噪音。"""
    _install({"a.py": "def f():\n    return 1\n"})

    out = await read_file("a.py")

    assert out["content"] == "def f():\n    return 1"
    assert out["truncated"] is False
    assert "truncated" not in out["content"]


@pytest.mark.asyncio
async def test_grep_clips_long_match_lines():
    """定位工具的产出是线索，不是正文。"""
    long_line = "needle " + "q" * (GREP_MAX_LINE_CHARS * 20)
    _install({"app.py": long_line})

    out = await grep_files("needle")

    assert out["count"] == 1
    text = out["matches"][0]["text"]
    assert len(text) < len(long_line) / 10
    assert "line truncated" in text
    # 定位信息必须还在：路径与行号是 grep 存在的理由。
    assert out["matches"][0]["path"] == "app.py"
    assert out["matches"][0]["line"] == 1


@pytest.mark.asyncio
async def test_grep_byte_budget_reports_truncation():
    """命中数没到 max_results，字节也可能已经超了 —— 要如实说，而不是静默少返回。"""
    lines = "\n".join(
        f"needle {i} " + "w" * GREP_MAX_LINE_CHARS for i in range(400)
    )
    _install({"a.txt": lines})

    out = await grep_files("needle", max_results=10_000)

    assert out.get("truncated") is True
    assert out["count"] < 400
    assert out["count"] > 0
    assert "budget" in out["note"]
    used = sum(len(m["text"].encode()) + 1 for m in out["matches"])
    assert used <= GREP_MAX_BYTES + 1


@pytest.mark.asyncio
async def test_invalid_pattern_still_reports_an_error():
    """预算是附加约束，不能改变错误语义。"""
    _install({"a.txt": "x"})

    out = await grep_files("(unclosed")

    assert "invalid pattern" in out["error"]
    assert out["count"] == 0
