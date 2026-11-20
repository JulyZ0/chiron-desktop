"""分享运营：访问日志与保护字段。

预留文件：revision 链已排好。本迁移由「分享运营能力」任务填充 —— 访问统计、
有效期/密码保护所需列与访问记录表。

不要改 revision / down_revision。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op  # noqa: F401

revision: str = "0005_share_ops"
down_revision: str | Sequence[str] | None = "0004_deps_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """待实现：share_access_log 表 + conversation_shares 保护列（如 password_hash）。"""


def downgrade() -> None:
    """待实现：回滚上述表与列。"""
