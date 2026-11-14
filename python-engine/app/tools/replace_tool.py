"""replace_in_files —— 跨文件的查找替换。

**为什么需要它**（而不是让模型多调几次 `edit_file`）

`edit_file` 的核心约束是 "`old_string` 必须在单个文件里唯一"。这对"改一处"是对的
（唯一性正是防误伤的手段），但对"改同一处的所有副本"恰好相反：重命名一个符号、
统一一个 import 路径、改一个函数签名，本质上就是**多处、多文件**的同一处改动。

没有批量工具时，模型唯一的路径是：对每个文件 `read_file`（几百上千行）→ 构造
`old_string` → `edit_file`。N 个文件 = N 次全量读 + N 次写，token 成本随文件数线性
膨胀；更糟的是**漏改一个就编译不过**，模型往往要跑一轮测试才发现，于是再一轮全量读。
这正是长线任务跑到一半就耗尽预算的典型形态之一。

**为什么 `dry_run` 默认 True**

查找替换是不可逆的批量操作 —— 一个写错的正则可以在**一次调用**里改坏整个仓库。
所以默认只预览、不落盘：先看 diff，确认命中范围对得上，再带 `dry_run=False` 执行。
这与"先看 `git diff` 再提交"是同一个习惯，只是把它做进了工具契约里，而不是指望
模型每次都自觉。

**与 `edit_file` 的分工**

* 改一处、要精确 → `edit_file`（唯一性校验是保护，不是障碍）；
* 改多处 / 跨文件 → 本工具（`dry_run` 预览 → 执行）；
* 需要程序化判断（条件替换、结构性重写）→ `run_code`。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from app.tools.registry import registry

#: 单个文件的大小上限。超过就跳过并如实报告 —— 读取一个上百 MB 的文件会把整个
#: 工具调用拖死，而这类文件（构建产物、数据集、锁文件）几乎从来不是替换目标。
MAX_FILE_BYTES = 2 * 1024 * 1024

#: 每个文件在回报里最多展示的 diff 行数。回报本身也要守上下文预算：
#: 一次替换 50 个文件、每个 diff 两百行，光回报就能吃掉整个窗口。
MAX_DIFF_LINES_PER_FILE = 40

#: 单次调用最多处理的文件数 / 替换总数。这是**爆炸半径**的闸门，不是性能调优：
#: 一个 `\.` 写成 `.` 的正则能把全仓库每个字符都换掉。到量即停并如实报告剩余量，
#: 让模型自己决定要不要收窄 `glob`/`root`。
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_REPLACEMENTS = 1000

#: 预览时逐文件展示的命中行数上限（与 diff 上限同一理由）
MAX_PREVIEW_LINES = 10


def _preview_hits(original: str, regex: re.Pattern[str]) -> list[tuple[int, str]]:
    """把匹配位置映射回行号，供预览展示。

    用 `finditer` 而不是逐行 `search`：`pattern` 可以跨行（`re.sub` 作用在全文上），
    逐行搜索会让跨行匹配在预览里**完全消失** —— 预览看不见的改动最危险。
    """
    lines = original.splitlines()
    hits: dict[int, str] = {}
    for m in regex.finditer(original):
        lineno = original.count("\n", 0, m.start()) + 1
        if lineno not in hits and 0 < lineno <= len(lines):
            hits[lineno] = lines[lineno - 1]
    return sorted(hits.items())


def _preview_lines(lines: list[tuple[int, str]]) -> list[str]:
    """把命中行压成 `行号: 内容` 的短预览，超量即截并说明还剩多少。"""
    shown = [f"  {n}: {text}" for n, text in lines[:MAX_PREVIEW_LINES]]
    if len(lines) > MAX_PREVIEW_LINES:
        shown.append(f"  … (+{len(lines) - MAX_PREVIEW_LINES} more matching lines)")
    return shown


def _truncate_diff(diff: str) -> str:
    """diff 按行截断（不按字符）：保留 `@@` 头与真正改动的行，尾部说明被省略多少。"""
    rows = diff.splitlines()
    if len(rows) <= MAX_DIFF_LINES_PER_FILE:
        return diff
    kept = rows[:MAX_DIFF_LINES_PER_FILE]
    kept.append(f"… (diff truncated: {len(rows) - MAX_DIFF_LINES_PER_FILE} more lines)")
    return "\n".join(kept)


async def replace_in_files(
    pattern: str,
    replacement: str,
    glob: str = "",
    root: str = ".",
    dry_run: bool = True,
    fixed: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_replacements: int = DEFAULT_MAX_REPLACEMENTS,
) -> dict[str, Any]:
    """在工作区里批量查找替换（默认只预览）。

    Args:
        pattern: 正则（`fixed=True` 时按字面量）。作用在**整个文件内容**上，
            因此可跨行匹配（`\n` 有效）—— 与逐行匹配的 `grep_files` 不同。
        replacement: 替换文本。正则模式下支持 `\\1` / `\\g<name>` 反向引用；
            `fixed=True` 时按字面量替换（`$` / `\\` 都不会被解释）。
        glob: 只处理匹配该 glob 的文件（如 `*.py`）；空 = 全部。
        root: 搜索起点目录（相对工作区）。
        dry_run: True（默认）只预览，不落盘。
        fixed: True 表示 `pattern` 是字面量而非正则。
        max_files: 最多处理多少个文件。
        max_replacements: 最多替换多少处。

    Returns:
        `dry_run=True`：`files` 逐项含命中行预览与预计替换数；
        `dry_run=False`：额外含每个文件实际写入的 diff。

        两种情况都带 `files_scanned` / `files_matched` / `replacements` / `truncated`，
        让模型知道"还有没有剩下没处理"。
    """
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    if not pattern:
        return {"error": "pattern must not be empty"}

    # ── 正则编译 ──
    # 用 re.MULTILINE 是为了让 `^` / `$` 按行匹配（改整行注释/import 的最常见诉求），
    # 而 `re.sub` 默认作用于全文，仍可跨行匹配 —— 两者不冲突。
    source = re.escape(pattern) if fixed else pattern
    try:
        regex = re.compile(source, re.MULTILINE)
    except re.error as exc:
        return {"error": f"invalid pattern: {exc}"}

    try:
        base = safe_join(root or ".")
    except ValueError:
        return {"error": "path escapes sandbox"}

    backend = get_backend()
    try:
        globbed = await backend.glob(f"**/{glob or '*'}", path=root or ".")
    except ValueError:
        return {"error": "path escapes sandbox"}
    if globbed.error:
        return {"error": globbed.error}

    # 只保留工作区内、且确实是文件的候选
    candidates: list[str] = []
    for path in globbed.paths:
        info = await backend.stat(path)
        if info is None or not info.is_file:
            continue
        candidates.append(path)
    candidates.sort()

    files: list[dict[str, Any]] = []
    scanned = 0
    matched = 0
    total_replacements = 0
    skipped: list[dict[str, str]] = []
    truncated = False

    for path in candidates:
        if matched >= max_files:
            truncated = True
            break
        if total_replacements >= max_replacements:
            truncated = True
            break

        info = await backend.stat(path)
        if info is not None and info.size > MAX_FILE_BYTES:
            skipped.append(
                {
                    "path": path,
                    "reason": f"larger than {MAX_FILE_BYTES // 1024}KB",
                }
            )
            continue

        read = await backend.read(path, limit=0)
        if read.error:
            # 读不了：跳过而不中断整批 —— 一个 `*.png` 不该让整个替换失败。
            skipped.append({"path": path, "reason": read.error[:120]})
            continue
        scanned += 1
        original = read.content

        # **二进制保护**：`read(limit=0)` 走的是"读全文"分支，**不做** `_looks_binary`
        # 探测（那是分页路径才有的），它用 `errors="replace"` 把任意字节当文本读进来。
        # 若在此处照常 `write` 回去，整个二进制文件会被 `\ufffd` 覆盖成垃圾 ——
        # 一次正则替换就可能毁掉仓库里的图片、字体、模型权重。所以这里按**与后端
        # 分页路径一致的口径**（含 NUL 即二进制）自行拦截。
        if "\x00" in original:
            skipped.append({"path": path, "reason": "looks like a binary file (contains NUL)"})
            continue

        occurrences = len(regex.findall(original))
        if occurrences == 0:
            continue

        matched += 1
        remaining = max_replacements - total_replacements
        if occurrences > remaining:
            # 这一批会超出总闸门：只替换前 `remaining` 处，并标记截断。
            count = [0]

            def _limited(m: re.Match[str], _limit: int = remaining) -> str:
                count[0] += 1
                if count[0] > _limit:
                    return m.group(0)
                return m.expand(replacement) if not fixed else replacement

            modified = regex.sub(_limited, original)
            applied = min(occurrences, remaining)
            total_replacements += applied
            truncated = True
        else:
            # 正则模式直接把 `replacement` 交给 `re.sub`（`\1` / `\g<name>` 由它解释）；
            # `fixed` 模式必须用 lambda —— 否则替换文本里的 `\` / `$` 会被当成反向引用。
            if fixed:
                modified = regex.sub(lambda _m: replacement, original)
            else:
                modified = regex.sub(replacement, original)
            applied = occurrences
            total_replacements += occurrences

        entry: dict[str, Any] = {
            "path": path,
            "matches": occurrences,
            "replacements": applied,
        }

        if dry_run:
            entry["preview"] = _preview_lines(_preview_hits(original, regex))
            files.append(entry)
            continue

        # ── 真正落盘 ──
        target = safe_join(path)
        conflict = check_before_write(target)
        if conflict:
            entry["error"] = conflict
            entry.pop("replacements", None)
            files.append(entry)
            total_replacements -= applied
            matched -= 1
            continue

        undo_note = await undo_stack.snapshot_before_write(
            get_session_id(), target, "replace_in_files"
        )
        written = await backend.write(path, modified)
        if written.error:
            entry["error"] = written.error
            files.append(entry)
            total_replacements -= applied
            matched -= 1
            continue

        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        entry["diff"] = _truncate_diff(diff)
        if undo_note:
            entry["undo_warning"] = undo_note
        files.append(entry)

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "files_scanned": scanned,
        "files_matched": matched,
        "replacements": total_replacements,
        "files": files,
    }
    if skipped:
        result["skipped"] = skipped
    if truncated:
        result["truncated"] = True
        result["note"] = (
            "stopped at the file/replacement budget; narrow `glob` or `root` and run "
            "again for the rest — this call did NOT cover everything"
        )
    if dry_run and files:
        result["next"] = (
            "Review the previews above. If the hit set is right, call again with "
            "`dry_run=false` to apply; the same `pattern`/`glob`/`root` will select the "
            "same files."
        )
    return result


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
registry.register(
    name="replace_in_files",
    description=(
        "Find-and-replace across many files in one call (regex or literal), with a "
        "dry-run preview by default. **Use this instead of many `edit_file` calls when "
        "the same change applies to several places or several files** — renaming a "
        "symbol, updating an import path, changing a signature. `edit_file` demands a "
        "unique `old_string`, which is exactly what blocks multi-site edits. Previews "
        "first (dry_run defaults to true), then call again with `dry_run=false` to apply. "
        "Matches against whole file contents, so patterns may span lines."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex to find (a literal string when fixed=true)",
            },
            "replacement": {
                "type": "string",
                "description": "Replacement text; supports \\\\1 backreferences in regex mode",
            },
            "glob": {
                "type": "string",
                "default": "",
                "description": "Only touch files matching this glob (e.g. '*.py'); empty = all",
            },
            "root": {
                "type": "string",
                "default": ".",
                "description": "Directory to search under (relative to the workspace)",
            },
            "dry_run": {
                "type": "boolean",
                "default": True,
                "description": "Preview only; set false to actually write the changes",
            },
            "fixed": {
                "type": "boolean",
                "default": False,
                "description": "Treat pattern as a literal string instead of a regex",
            },
            "max_files": {
                "type": "integer",
                "default": DEFAULT_MAX_FILES,
                "description": "Stop after touching this many files",
            },
            "max_replacements": {
                "type": "integer",
                "default": DEFAULT_MAX_REPLACEMENTS,
                "description": "Stop after this many individual replacements",
            },
        },
        "required": ["pattern", "replacement"],
    },
    handler=replace_in_files,
)
