"""replace_in_files 的回归测试。

重点不是"能替换"，而是**批量替换的爆炸半径可控**：
默认只预览、跨行匹配预览不漏、字面量模式不被反向引用解释、到量即停且如实报告。
"""

from __future__ import annotations

import pytest

from app.backends.context import set_backend
from app.backends.local import LocalWorkspaceBackend
from app.tools.replace_tool import replace_in_files


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """隔离工作区：只改这一轮的临时目录，绝不碰仓库。

    落盘位置由 `workspace_dir()` 给出（`SANDBOX_ROOT/tenant/user/workspace`），
    **不能**手拼 `tmp_path` —— 那会让 `backend.glob` 扫到空目录，测试全绿假象之外
    还会掩盖真实的目录形状问题。
    """
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path))
    from app.tools.sandbox import workspace_dir

    root = workspace_dir()
    set_backend(LocalWorkspaceBackend())
    yield root
    set_backend(LocalWorkspaceBackend())


def _write(root, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(root, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(workspace):
    """默认必须只预览 —— 一个写错的正则能在一次调用里改坏整个仓库。"""
    _write(workspace, "a.py", "old_name = 1\n")
    _write(workspace, "b.py", "print(old_name)\n")

    out = await replace_in_files("old_name", "new_name")

    assert out["dry_run"] is True
    assert out["files_matched"] == 2
    assert out["replacements"] == 2
    # 文件内容必须原封不动
    assert _read(workspace, "a.py") == "old_name = 1\n"
    assert _read(workspace, "b.py") == "print(old_name)\n"
    # 预览要能让模型核对命中位置
    assert any("1:" in line for line in out["files"][0]["preview"])


@pytest.mark.asyncio
async def test_apply_writes_every_matching_file(workspace):
    _write(workspace, "a.py", "old_name = 1\n")
    _write(workspace, "nested/b.py", "print(old_name)\nprint(old_name)\n")

    out = await replace_in_files("old_name", "new_name", dry_run=False)

    assert out["files_matched"] == 2
    assert out["replacements"] == 3
    assert _read(workspace, "a.py") == "new_name = 1\n"
    assert _read(workspace, "nested/b.py") == "print(new_name)\nprint(new_name)\n"
    # 每个落盘文件都要带 diff，模型才能核对改了什么
    assert all("diff" in entry for entry in out["files"])


@pytest.mark.asyncio
async def test_glob_narrows_the_blast_radius(workspace):
    _write(workspace, "a.py", "target\n")
    _write(workspace, "a.txt", "target\n")

    out = await replace_in_files("target", "done", glob="*.py", dry_run=False)

    assert out["files_matched"] == 1
    assert _read(workspace, "a.py") == "done\n"
    assert _read(workspace, "a.txt") == "target\n", "glob 之外的扩展名不该被改"


@pytest.mark.asyncio
async def test_cross_line_pattern_is_previewed_not_hidden(workspace):
    """预览用 `finditer` 而非逐行 `search`：跨行匹配必须在预览里看得见。

    逐行搜索会让跨行匹配在预览中完全消失 —— 预览看不见的改动最危险。
    """
    _write(workspace, "a.py", "def f(\n    x,\n):\n    pass\n")

    out = await replace_in_files(r"def f\(\n    x,\n\)", "def f(x)", dry_run=True)

    assert out["files_matched"] == 1
    assert out["files"][0]["matches"] == 1
    assert out["files"][0]["preview"], "跨行匹配的预览不能是空的"


@pytest.mark.asyncio
async def test_fixed_mode_does_not_interpret_backreferences(workspace):
    """`fixed=True` 时替换文本按字面量处理 —— 否则 `\\1` 会被当成反向引用。"""
    _write(workspace, "a.txt", "price: $5\n")

    out = await replace_in_files("price", r"cost\\1 $", fixed=True, dry_run=False)

    assert out["files_matched"] == 1
    assert _read(workspace, "a.txt") == r"cost\\1 $: $5" + "\n"


@pytest.mark.asyncio
async def test_regex_mode_supports_backreferences(workspace):
    _write(workspace, "a.py", "value = get_old_thing()\n")

    await replace_in_files(r"get_(\w+)_thing", r"fetch_\1", dry_run=False)

    assert _read(workspace, "a.py") == "value = fetch_old()\n"


@pytest.mark.asyncio
async def test_replacement_budget_stops_and_reports_leftovers(workspace):
    """到量即停，并且必须**明说没覆盖完** —— 否则模型会以为改完了。"""
    _write(workspace, "a.py", "hit\n" * 5)
    _write(workspace, "b.py", "hit\n" * 5)

    out = await replace_in_files("hit", "done", dry_run=False, max_files=1)

    assert out["truncated"] is True
    assert out["files_matched"] == 1
    assert "NOT cover everything" in out["note"]
    # 只动了一个文件，另一个保持原样
    assert _read(workspace, "b.py") == "hit\n" * 5


@pytest.mark.asyncio
async def test_invalid_regex_reports_error_without_touching_files(workspace):
    _write(workspace, "a.py", "keep me\n")

    out = await replace_in_files("(unclosed", "x", dry_run=False)

    assert "invalid pattern" in out["error"]
    assert _read(workspace, "a.py") == "keep me\n"


@pytest.mark.asyncio
async def test_empty_pattern_is_rejected(workspace):
    """空 pattern 会匹配每个位置 —— 直接拒绝，而不是"改掉全文"。"""
    _write(workspace, "a.py", "abc\n")

    out = await replace_in_files("", "x", dry_run=False)

    assert "must not be empty" in out["error"]
    assert _read(workspace, "a.py") == "abc\n"


@pytest.mark.asyncio
async def test_non_utf8_files_are_skipped_not_fatal(workspace):
    """一个二进制文件不该让整批替换失败。

    载荷刻意**不含 UTF-16 BOM**：`_looks_binary` 把带 BOM 的 UTF-16 当文本放行
    （那是合法文本），带 BOM 的内容根本走不到这条分支。
    """
    (workspace / "blob.bin").write_bytes(b"PK\x03\x04\x00\x00target")
    _write(workspace, "a.py", "target\n")

    out = await replace_in_files("target", "done", dry_run=False)

    assert _read(workspace, "a.py") == "done\n"
    # glob 返回的是绝对路径，断言按结尾比对
    assert any(entry["path"].endswith("blob.bin") for entry in out.get("skipped", []))
