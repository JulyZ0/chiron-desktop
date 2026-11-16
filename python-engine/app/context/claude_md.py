# CLAUDE.md hierarchical loader
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 项目指令文件的**候选名**，按优先级从高到低（G-P2）。
#:
#: 对齐 pi `resource-loader.ts:213` 的候选集：
#: ``["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]``。
#:
#: 为什么需要 `AGENTS.md`：它是跨工具的事实约定（Codex / Cursor / pi 等都读它），
#: 而 chiron 此前只认 `CLAUDE.md` —— 用户为一套工具写好的项目约束，换到 chiron
#: 上等于不存在。反过来保留 `CLAUDE.md` 是为了不打断既有用户。
#:
#: `AGENTS.override.md` 的语义是**覆盖**：同一目录里它存在时，`AGENTS.md` 与
#: `CLAUDE.md` 都不再读。这与 pi 一致 —— 覆盖文件的意义就是"别再看别的"，
#: 三个都拼进去会让"我只想临时盖掉一段"变成"又叠加了一段"。
#:
#: 小写变体一起列出：Windows / macOS 上文件名大小写不敏感，但 Linux 上不敏感是
#: 错的假设 —— 用户写 `agents.md` 时应当仍能被认出来。
PROJECT_INSTRUCTION_NAMES: tuple[str, ...] = (
    "AGENTS.override.md",
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    "Claude.md",
)

# Well-known locations (highest → lowest priority)
_HOME_DIR = Path.home() / ".claude" / "CLAUDE.md"


def _pick_instruction_file(directory: Path) -> Path | None:
    """在**单个目录**里按优先级挑一份项目指令文件（无则 None）。

    `AGENTS.override.md` 命中后立即返回 —— 覆盖语义见 `PROJECT_INSTRUCTION_NAMES`
    的文档。其余名字之间是"第一个存在的胜出"，不是全部拼接。
    """
    for name in PROJECT_INSTRUCTION_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _global_instruction_file() -> Path | None:
    """全局项目指令（``~/.claude/`` 下）。

    从 `_HOME_DIR` 的**父目录**找，而不是写死 `CLAUDE.md` —— 这样既支持全局
    `AGENTS.md`，又让测试可以通过 monkeypatch `_HOME_DIR` 精确关闭全局读取
    （父目录不存在 ⇒ 什么都找不到）。
    """
    return _pick_instruction_file(_HOME_DIR.parent)


class ClaudeMdLoader:
    """Load and merge CLAUDE.md files from the project hierarchy.

    Scans from *root* upward to the filesystem root, then checks
    ``~/.claude/CLAUDE.md``.  All found files are concatenated (highest
    priority first = nearest to *root*), separated by a divider.
    Results are cached so repeated calls are cheap.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, root: str = ".") -> str:
        """Load and merge project instruction files found in the hierarchy.

        Args:
            root: Starting directory (default ``"."``).

        Returns:
            Merged content of all instruction files, or an empty string if none
            are found.
        """
        resolved = str(Path(root).resolve())
        if resolved in self._cache:
            return self._cache[resolved]

        files = self._find_claude_md(root)

        if not files:
            self._cache[resolved] = ""
            return ""

        parts: list[str] = []
        for filepath in files:
            try:
                text = Path(filepath).read_text(encoding="utf-8")
                if text.strip():
                    # 标签用**真实文件名**而不是写死 `CLAUDE.md`（G-P2）：现在候选名
                    # 有 `AGENTS.md` / `AGENTS.override.md` 等，写死会让模型以为读的是
                    # 另一个文件，排查"我明明改了 AGENTS.md 怎么没生效"时直接被误导。
                    header = f"<!-- {Path(filepath).name}: {filepath} -->"
                    parts.append(f"{header}\n{text.strip()}")
            except OSError as exc:
                logger.warning("Could not read %s: %s", filepath, exc)

        merged = "\n\n---\n\n".join(parts)
        self._cache[resolved] = merged
        return merged

    def clear_cache(self) -> None:
        """Invalidate the internal cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _find_claude_md(root: str) -> list[str]:
        """Find project instruction files in the directory hierarchy.

        Walks from *root* to its parents (inclusive), then checks the global
        ``~/.claude/`` directory.  Returns paths ordered from nearest to
        *root* (highest priority) to farthest.

        **每个目录只取一份**（`_pick_instruction_file` 的优先级），而不是把该目录
        下所有候选名都读进来 —— 同一目录同时存在 `AGENTS.md` 与 `CLAUDE.md` 时
        通常一份是另一份的副本或旧版，两份都拼进去等于把同一段约束说两遍。

        **按 realpath 去重**：符号链接或 worktree 会让"同一个文件"以不同路径被
        祖先链重复发现（pi 的 `resource-loader.ts:283` 为此专门按目录 realpath 比较）。
        重复注入的后果不是省 token，而是让模型以为约束被强调了两次。

        Args:
            root: Starting directory.

        Returns:
            List of absolute paths to existing instruction files.
        """
        results: list[str] = []
        seen: set[str] = set()
        current = Path(root).resolve()

        def _add(path: Path) -> None:
            # realpath 解析符号链接后比较：不同字面路径可能是同一份文件。
            try:
                key = str(path.resolve())
            except OSError:  # pragma: no cover — 路径不可解析时退回字面值
                key = str(path)
            if key in seen:
                return
            seen.add(key)
            results.append(str(path))

        # Walk upward from root
        while True:
            chosen = _pick_instruction_file(current)
            if chosen is not None:
                _add(chosen)

            parent = current.parent
            if parent == current:
                # Reached filesystem root
                break
            current = parent

        # Global fallback
        global_file = _global_instruction_file()
        if global_file is not None:
            _add(global_file)

        return results
