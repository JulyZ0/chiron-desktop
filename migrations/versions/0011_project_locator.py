"""项目身份键：provider / device_id / root_key。

Revision ID: 0011_project_locator
Revises: 0010_user_invites
Create Date: 2026-11-23

## 为什么不是继续用 root_path 判重

`root_path` 是**执行端**路径。远程/团队模式下同一字符串可能指向不同机器上的不同
目录；只比字符串会把它们误判成同一个项目，或让不同设备上的同路径项目互相挡住。
项目身份必须由 `(provider, device_id, root_key)` 决定，`root_path` 只用于展示与访问。

## 为什么 root_key 允许为空

历史行的规范化需要访问文件系统（symlink/junction），不能在 SQL 迁移里算。
迁移先把列加上、root_key 留空；网关启动后由 `backfillProjectRootKeys` 尽力回填。
唯一索引带 `WHERE root_key_hash <> ''`，因此回填前不影响写入，回填后自动生效。

## 为什么唯一索引落在 root_key_hash 而不是 root_key

Postgres btree 的单行索引项有约 2704 字节上限。root_key 是 2048 字符的路径，
中文/emoji 路径按 UTF-8 3–4 字节算会直接超限，导致 CREATE INDEX 或 INSERT 失败。
索引改用 Go 侧计算的 sha256 十六进制（64 字节），root_key 只保留给人看/排障。

## 为什么先加 CHECK 再上线 provider

当前只有 `server`；`client`（设备文件桥）在后续切片接入。CHECK 保证任何写入路径
（包括手改 SQL）不能把未知 provider 带进来。

不要改 revision / down_revision。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_project_locator"
down_revision: str | Sequence[str] | None = "0010_user_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "session_map_workspaces",
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="server"),
    )
    op.add_column(
        "session_map_workspaces",
        sa.Column("device_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "session_map_workspaces",
        sa.Column("root_key", sa.String(length=2048), nullable=False, server_default=""),
    )
    op.add_column(
        "session_map_workspaces",
        sa.Column("root_key_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_check_constraint(
        "ck_smw_provider",
        "session_map_workspaces",
        "provider IN ('server', 'client')",
    )
    op.create_index(
        "ux_smw_project_root",
        "session_map_workspaces",
        ["tenant_id", "user_id", "provider", "device_id", "root_key_hash"],
        unique=True,
        postgresql_where=sa.text("root_key_hash <> ''"),
    )


def downgrade() -> None:
    op.drop_index("ux_smw_project_root", table_name="session_map_workspaces")
    op.drop_constraint("ck_smw_provider", "session_map_workspaces", type_="check")
    op.drop_column("session_map_workspaces", "root_key_hash")
    op.drop_column("session_map_workspaces", "root_key")
    op.drop_column("session_map_workspaces", "device_id")
    op.drop_column("session_map_workspaces", "provider")
