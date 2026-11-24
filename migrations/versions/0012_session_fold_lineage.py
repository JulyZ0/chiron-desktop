"""Persist fold lineage and database-backed projection CAS state.

Revision ID: 0012_session_fold_lineage
Revises: 0011_project_locator
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_session_fold_lineage"
down_revision: str | Sequence[str] | None = "0011_project_locator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session_folds", sa.Column("lineage", postgresql.JSONB(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("message_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "session_projection_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("source_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("message_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="claimed"),
        sa.Column("projection_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_digest ~ '^[0-9a-f]{64}$'", name="ck_session_projection_source_digest"
        ),
        sa.CheckConstraint(
            "projection_digest IS NULL OR projection_digest ~ '^[0-9a-f]{64}$'",
            name="ck_session_projection_digest",
        ),
        sa.CheckConstraint(
            "status IN ('claimed', 'installed')", name="ck_session_projection_status"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], name="session_projection_claims_session_fkey", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="session_projection_claims_pkey"),
        sa.UniqueConstraint(
            "session_id", "source_digest", name="ux_session_projection_claims_source"
        ),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.bump_session_message_revision()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                UPDATE public.sessions
                SET message_revision = message_revision + 1
                WHERE id = OLD.session_id;
                RETURN OLD;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                UPDATE public.sessions
                SET message_revision = message_revision + 1
                WHERE id = OLD.session_id;
                IF NEW.session_id IS DISTINCT FROM OLD.session_id THEN
                    UPDATE public.sessions
                    SET message_revision = message_revision + 1
                    WHERE id = NEW.session_id;
                END IF;
            ELSE
                UPDATE public.sessions
                SET message_revision = message_revision + 1
                WHERE id = NEW.session_id;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    for table, trigger in (
        ("messages", "trg_messages_session_revision"),
        ("tool_calls", "trg_tool_calls_session_revision"),
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger} ON public.{table}; "
            f"CREATE TRIGGER {trigger} AFTER INSERT OR UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.bump_session_message_revision();"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_messages_session_revision ON public.messages")
    op.execute("DROP TRIGGER IF EXISTS trg_tool_calls_session_revision ON public.tool_calls")
    op.execute("DROP FUNCTION IF EXISTS public.bump_session_message_revision()")
    op.drop_table("session_projection_claims")
    op.drop_column("sessions", "message_revision")
    op.drop_column("session_folds", "lineage")
