"""迁移链单 head 守卫：`migrations/versions` 里必须**恰好一个** head。

为什么单独成脚本：`scripts/ci-smoke.sh` 的 schema 环节与
`scripts/check-release-ready.sh` 的发布门禁要的是**同一个**判据（分叉会让
`internal/db.ParseMigrationHead` 直接拒绝启动网关）。这段解析此前内联在
ci-smoke.sh 的 heredoc 里，第二个调用点只能靠复制 —— 两份正则各自漂移时，
本地"绿"与发布"红"就会打架。

判据与 `internal/db/schema_version.go::ParseMigrationHead` 同构：只读
`revision` / `down_revision` 声明，不依赖 alembic（本地未必装了
`requirements-migrate.txt`）。

用法：
    python scripts/check_migration_head.py                 # 默认 migrations/versions
    python scripts/check_migration_head.py <versions 目录>  # 换目录（测试/演练用）

退出码：0 = 恰好 1 个 head；1 = 分叉 / 缺 revision 声明 / 目录不存在。
"""

from __future__ import annotations

import pathlib
import re
import sys

DEFAULT_DIR = pathlib.Path("migrations/versions")
#: `revision = "abc"` / `revision: str = "abc"` 两种写法都要认（含类型标注）
REVISION_RE = re.compile(r"^revision\s*(?::[^=\n]+)?=\s*[\"']([^\"']+)[\"']", re.M)
DOWN_RE = re.compile(r"^down_revision\s*(?::[^=\n]+)?=\s*(.+)$", re.M)
QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")


def parse(target: pathlib.Path) -> tuple[dict[str, str], set[str]]:
    """返回 ({revision: 文件名}, {被引用为父节点的 revision})。"""
    revisions: dict[str, str] = {}
    parents: set[str] = set()
    for f in sorted(target.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        m = REVISION_RE.search(text)
        if not m:
            raise ValueError(f"{f.name}: 找不到 revision 声明")
        revisions[m.group(1)] = f.name
        d = DOWN_RE.search(text)
        if d:
            # down_revision 可能是 None / "abc" / ("a", "b") —— 只取引号内的 revision
            parents.update(QUOTED_RE.findall(d.group(1)))
    return revisions, parents


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    if not target.is_dir():
        print(f"   ✘ 找不到迁移目录：{target}", file=sys.stderr)
        return 1
    try:
        revisions, parents = parse(target)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"   ✘ {exc}", file=sys.stderr)
        return 1
    if not revisions:
        print(f"   ✘ {target} 里没有任何带 revision 的迁移文件", file=sys.stderr)
        return 1
    heads = sorted(set(revisions) - parents)
    print(f"   revisions={len(revisions)} heads={heads}")
    if len(heads) != 1:
        print("   ✘ 迁移链必须只有 1 个 head（分叉会让网关拒绝启动）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
