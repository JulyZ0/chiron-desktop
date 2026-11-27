"""knowledge_documents.content_sha256 for cross-session dedupe.

Revision ID: 0016_kb_content_hash
Revises: 0015_kb_status_normalize
Create Date: 2026-11-27

同一个文件被重复导入（批量选文件夹、换个人再传一次）时，库里会出现多条内容完全
相同的文档：索引重复烧 token，检索也会返回同一段内容的多个副本。这里为文档增加
内容哈希列，上传收尾阶段按 (tenant, kb, sha256) 判重。

幂等：列/索引不存在时才创建，重跑安全。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_kb_content_hash"
down_revision: str | Sequence[str] | None = "0015_kb_status_normalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.knowledge_documents "
        "ADD COLUMN IF NOT EXISTS content_sha256 character varying(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_content_sha256 "
        "ON public.knowledge_documents (tenant_id, knowledge_base_id, content_sha256)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_knowledge_documents_content_sha256")
    op.execute("ALTER TABLE public.knowledge_documents DROP COLUMN IF EXISTS content_sha256")
