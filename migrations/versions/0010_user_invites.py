"""用户邀请链接：一次性 token 换初始密码。

Revision ID: 0010_user_invites
Revises: 0009_personas
Create Date: 2026-11-22

## 为什么单独建表

初始密码只展示一次的交付方式依赖聊天工具外传；邀请链接把"凭据交付"挪到一次性的
HTTPS 链接上。token 只存 sha256 摘要（与 API Key 同口径），有效期 24 小时、用后即焚。

## 不改 users 表

`must_change_password` 状态机已经完整：邀请者设完密码仍要求首登改密（这里反过来，
接受邀请时设的就是用户自己的密码，不再强制改）。因此只需要一张 token 表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_user_invites"
down_revision: str | Sequence[str] | None = "0009_personas"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_invite_tokens",
        sa.Column("token_hash", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_user_invite_tokens_user", "user_invite_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_invite_tokens_user", table_name="user_invite_tokens")
    op.drop_table("user_invite_tokens")
