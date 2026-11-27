#!/usr/bin/env python3
"""代码注释里的文档引用守卫。

## 为什么需要它

注释里的 ``docs/xxx.md §N`` 是**软引用**：文档改名、删节、整篇删除时，编译器与测试
都不会报错，只有下一个读者点进去才发现是死链。本仓已经发生过整批设计稿删除后，
几十处注释仍在引用它们。这个脚本把"引用必须能落地"变成断言：

1. 注释里出现的 ``docs/*.md`` 必须存在；
2. 随引用写出的章节号（``§9.3`` / ``第 5 节``）必须在该文档的标题里出现过；
3. 已删除的规划稿（带编号的方案/评审、roadmap 或 spec 的章节引用、vendor 下的
   规划文档）不允许再出现在注释里 —— 它们没有替代品，只能把结论写进注释。

## 边界

* 只扫**代码注释与 Python docstring**；字符串字面量里的路径（测试夹具、示例内容）
  不在此列 —— 它们不是引用。
* 跳过 URL（如 ``https://github.com/.../docs/openai.md``）与占位路径
  （``docs/a.md``、``docs/readme.md`` 等测试夹具）。
* 章节号只在"引用与章节号位于同一条注释"时校验，避免跨行猜测。
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CODE_SUFFIXES = {".go", ".py", ".ts", ".vue", ".cjs", ".mjs", ".js"}

DOC_REF = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")
SECTION = re.compile(r"§\s*(\d+(?:\.\d+)*)")
SECTION_CN = re.compile(r"第\s*(\d+)\s*节")
HEADING_SECTION = re.compile(r"^#{1,4}\s+(?:§\s*)?(\d+(?:\.\d+)*)(?:[.、\s]|$)")
HEADING_SECTION_INLINE = re.compile(r"^#{1,4}\s+§\s*(\d+(?:\.\d+)*)")

# 字符串/文档里出现的占位路径，不是真引用。
SYNTHETIC_REFS = {
    "docs/a.md",
    "docs/b.md",
    "docs/1.md",
    "docs/2.md",
    "docs/3.md",
    "docs/readme.md",
    "docs/gateway.md",
    "docs/design.md",
    "docs/xxx.md",
}

# 已删除的规划稿：引用它们等于死链，必须把结论写进注释本身。
STALE_PATTERNS = [
    (re.compile(r"(?:方案|评审)\s*0\d"), "已删除的设计稿引用（方案/评审 0X）"),
    (re.compile(r"方案\s*§"), "已删除的设计稿引用（方案 §）"),
    (re.compile(r"roadmap\s*(?:§|P\d)"), "已删除的 roadmap 引用"),
    (re.compile(r"spec\s*§"), "已删除的 spec 引用"),
    (re.compile(r"vendor/(?:规划|方案|设计)"), "已删除的 vendor 规划稿引用"),
]


def tracked_code_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    files = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        p = ROOT / raw.decode("utf-8")
        if p.suffix in CODE_SUFFIXES and p.is_file():
            files.append(p)
    return files


def python_comment_lines(text: str) -> dict[int, str]:
    """注释 + docstring 的行号 → 文本。语法错误时退化为只看 `#`。"""
    lines: dict[int, str] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                lines[i] = line
        return lines

    doc_ranges: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc_ranges.append((first.lineno, first.end_lineno or first.lineno, first.value.value))

    for i, line in enumerate(text.splitlines(), 1):
        for start, end, _ in doc_ranges:
            if start <= i <= end:
                lines[i] = line
                break
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                lines[tok.start[0]] = tok.line
    except (tokenize.TokenError, IndentationError):
        pass
    return lines


def c_like_comment_lines(text: str) -> dict[int, str]:
    """`//`、`/* */` 与 `<!-- -->` 的行号 → 文本（Vue/TS/Go 共用）。"""
    lines: dict[int, str] = {}
    in_block = False
    in_html = False
    for i, line in enumerate(text.splitlines(), 1):
        if in_block or in_html:
            lines[i] = line
            if in_block and "*/" in line:
                in_block = False
            if in_html and "-->" in line:
                in_html = False
            continue
        slash = line.find("//")
        block = line.find("/*")
        html = line.find("<!--")
        starts = [x for x in (slash, block, html) if x >= 0]
        if not starts:
            continue
        first = min(starts)
        # `http://` 不是注释。
        if first > 0 and line[first - 1] == ":":
            first = line.find("//", first + 2)
            if first < 0:
                continue
        lines[i] = line
        if first == block:
            in_block = "*/" not in line[block + 2 :]
        elif first == html:
            in_html = "-->" not in line[html + 4 :]
    return lines


def comment_lines(path: Path, text: str) -> dict[int, str]:
    if path.suffix == ".py":
        return python_comment_lines(text)
    return c_like_comment_lines(text)


def doc_sections(doc: Path) -> set[str]:
    sections: set[str] = set()
    try:
        text = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return sections
    for line in text.splitlines():
        m = HEADING_SECTION.match(line) or HEADING_SECTION_INLINE.match(line)
        if m:
            sections.add(m.group(1))
        for s in SECTION.findall(line):
            sections.add(s)
    return sections


def check() -> list[str]:
    errors: list[str] = []
    section_cache: dict[Path, set[str]] = {}

    for path in tracked_code_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in sorted(comment_lines(path, text).items()):
            for pattern, why in STALE_PATTERNS:
                if pattern.search(line):
                    errors.append(f"{rel}:{lineno}: {why}: {line.strip()[:100]}")

            refs = DOC_REF.findall(line)
            if not refs:
                continue
            markers = SECTION.findall(line) + SECTION_CN.findall(line)
            for ref in refs:
                if ref in SYNTHETIC_REFS:
                    continue
                if "://" in line[: max(0, line.find(ref))]:
                    continue
                doc = ROOT / ref
                if not doc.exists():
                    errors.append(f"{rel}:{lineno}: 引用的文档不存在: {ref}")
                    continue
                if not markers:
                    continue
                if doc not in section_cache:
                    section_cache[doc] = doc_sections(doc)
                known = section_cache[doc]
                for marker in markers:
                    if marker not in known:
                        errors.append(
                            f"{rel}:{lineno}: {ref} 没有 §{marker}（现有章节: "
                            f"{', '.join(sorted(known, key=_ver_key)) or '无编号标题'}）"
                        )
    return errors


def _ver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def main() -> int:
    errors = check()
    if not errors:
        print("comment refs: OK")
        return 0
    print(f"comment refs: {len(errors)} 处死引用", file=sys.stderr)
    for e in errors:
        print(f"  {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
