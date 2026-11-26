"""Schema 契约：worker/store 引用的列必须真的在迁移里存在。

## 为什么需要这条

修前 `pageindex-service/app/store.py` 的 `get_kb` 读了 `knowledge_bases.index_engine`，
索引成功收尾还要写它；但权威基线（`migrations/versions/0001_*`）与到 0013 的迁移链
**都没有这一列**。于是每一次建索引都在第一步报
`column "index_engine" does not exist`，worker 重试 3 次后把库置 `error`，
文档停在 `pending` —— 用户看到的就是「知识库不能用、索引显示异常」。

单测没有发现，是因为 `tests/test_service.py` 把 Store 整个 mock 了，行里手工带了
`index_engine`：**mock 与真实 schema 脱钩**。这条用例只做静态解析（不连库）：
把 `store.py` 里 `knowledge_bases` 的列名抽出来，逐个到迁移文本里找。

## 边界

* 只覆盖 `knowledge_bases` 的读/写列（本次事故的表）；不追求通用 SQL 解析。
* 迁移是唯一权威（`migrations/versions/*.py`，本仓库没有 `.sql`）。
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
STORE_FILE = SERVICE_DIR / "app" / "store.py"

#: `get_kb` 的 SELECT 会读这些列；`set_kb_index_engine` 会写 index_engine。
EXPECTED_KB_COLUMNS = frozenset(
    {"id", "tenant_id", "user_id", "provider", "index_ref", "status", "index_engine", "config"}
)


def _migrations_text() -> str:
    files = sorted(MIGRATIONS_DIR.glob("*.py"))
    assert files, f"没有找到迁移文件：{MIGRATIONS_DIR}"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def _store_text() -> str:
    return STORE_FILE.read_text(encoding="utf-8")


def test_store_references_only_columns_that_migrations_create():
    """store 用到的 knowledge_bases 列，每一个都能在迁移里找到。"""
    source = _store_text()
    # 从 store.py 里取 knowledge_bases 的 SELECT 列清单（`get_kb` 唯一一处）。
    select = re.search(
        r"SELECT(?P<cols>.*?)FROM\s+knowledge_bases", source, re.S | re.I
    )
    assert select, "store.py 里找不到 knowledge_bases 的 SELECT —— 结构调整后请同步本用例"
    referenced = {
        col.strip()
        for col in select.group("cols").replace("\n", " ").split(",")
        if re.fullmatch(r"[a-z_][a-z0-9_]*", col.strip(), re.I)
    }
    assert referenced == set(EXPECTED_KB_COLUMNS), (
        f"store.py 的列清单变了（{sorted(referenced)}）；"
        "请同步 EXPECTED_KB_COLUMNS 并确认迁移里有这些列"
    )

    migrations = _migrations_text()
    missing = sorted(col for col in referenced if not re.search(rf"\b{col}\b", migrations))
    assert not missing, (
        f"迁移里缺少 knowledge_bases 的列：{missing} —— "
        "worker 一取任务就会报 column does not exist，索引必然失败"
    )


def test_index_engine_column_is_created_by_a_migration():
    """`index_engine` 必须有**新增列**的迁移（不是只出现在注释里）。

    修前的迁移文本里完全没有该列；worker 的 `get_kb` 因此每次必失败。
    """
    migrations = _migrations_text()
    assert re.search(
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?index_engine\b", migrations, re.I
    ), "缺少 `ALTER TABLE knowledge_bases ADD COLUMN index_engine` 迁移"


def test_update_targets_are_known_columns():
    """`set_kb_index_engine` 的 UPDATE 也只碰已知列，避免写侧再漂移。"""
    source = _store_text()
    updates = re.findall(r"UPDATE\s+knowledge_bases\s+SET\s+([a-z_][a-z0-9_]*)", source, re.I)
    assert updates, "store.py 里找不到 knowledge_bases 的 UPDATE"
    unexpected = sorted(set(updates) - EXPECTED_KB_COLUMNS)
    assert not unexpected, f"UPDATE 写了 EXPECTED_KB_COLUMNS 之外的列：{unexpected}"
