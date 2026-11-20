"""运行时监控的分钟级持久化聚合。

预留文件（revision 链已排好，避免并行开发时迁移头分叉）：本迁移由「监控持久化」
任务填充 —— 目标是把进程内窗口采样落成 PG 分钟聚合，供多副本汇总与历史趋势查询。

不要改 revision / down_revision：后续迁移依赖这条链。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op  # noqa: F401

revision: str = "0003_metrics_rollup"
down_revision: str | Sequence[str] | None = "0002_inapp_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """待实现：metrics_rollup_minute 表 + 索引。"""


def downgrade() -> None:
    """待实现：删除 metrics_rollup_minute 表。"""
