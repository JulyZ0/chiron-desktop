"""Normalize legacy knowledge index status values.

Revision ID: 0015_kb_status_normalize
Revises: 0014_kb_index_engine
Create Date: 2026-11-26

背景：库级状态只有 ``disabled/indexing/ready/error`` 四个（见
``python-engine/app/knowledge/status.py``），文档级只有
``pending/indexing/ready/error``（见 ``pageindex-service/app/store.py``）。
历史上出现过 ``failed`` / ``building`` / ``active`` / ``draft`` 等取值 —— 它们会让：

* 前端拼出 ``knowledge.status_failed`` 这种原始 key（locale 只有四个）；
* ``isKbTerminal`` 判不出终态，轮询永不收敛；
* 文档聚合把未知状态当 ``pending``，"失败文档"永远不计数。

读侧已经归一（``app/knowledge/progress.py``：未知库状态 → error，未知文档状态 →
pending），本迁移把存量数据也收敛到约定取值，避免"同一个值在不同版本读出来不一样"。

幂等：只更新不在集合内的行，重跑安全。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_kb_status_normalize"
down_revision: str | Sequence[str] | None = "0014_kb_index_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 库级：未知值代表"这个库当前不可索引"——按 error 收口（与读侧归一一致）。
    op.execute(
        "UPDATE public.knowledge_bases SET status = 'error' "
        "WHERE status IS NOT NULL AND status NOT IN ('disabled', 'indexing', 'ready', 'error')"
    )
    # 文档级：未知值按 pending（宁可重跑，也不谎报 ready/error）。
    op.execute(
        "UPDATE public.knowledge_documents SET status = 'pending' "
        "WHERE status IS NOT NULL AND status NOT IN ('pending', 'indexing', 'ready', 'error')"
    )


def downgrade() -> None:
    # 数据归一不可逆：原值没有保留，回滚只撤销迁移版本号。
    pass
