"""依赖快照入库（多副本一致）。

预留文件：revision 链已排好。本迁移由「依赖管理多副本」任务填充 —— 目标是把
本地 `deps-snapshots/*.json` 迁到 PG，让任一副本都能看到同一份历史。

不要改 revision / down_revision。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op  # noqa: F401

revision: str = "0004_deps_snapshots"
down_revision: str | Sequence[str] | None = "0003_metrics_rollup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """待实现：deps_snapshots 表（id/tenant/created_at/trigger/label/packages jsonb）。"""


def downgrade() -> None:
    """待实现：删除 deps_snapshots 表。"""
