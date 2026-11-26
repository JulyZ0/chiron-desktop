"""Add knowledge_bases.index_engine (PageIndex SDK version).

Revision ID: 0014_kb_index_engine
Revises: 0013_tenant_identity
Create Date: 2026-11-26

修复：知识库索引**每次都失败**，页面只显示「异常」。

`pageindex-service` 的索引 worker 第一步就读这一列：

    SELECT id, tenant_id, user_id, provider, index_ref, status, index_engine, config
      FROM knowledge_bases WHERE id = $1        -- app/store.py::get_kb

索引成功收尾还要写它（`set_kb_index_engine`，记录 PageIndex SDK 版本，升级后据此
判断是否需要重建）。但 `knowledge_bases` 的权威基线（0001）里**没有这一列**，
迁移链到 0013 也没有补过 —— 于是：

* 每次 `POST /v1/kb/{id}/build` / `reindex` 都在 `get_kb` 处报
  `column "index_engine" does not exist`；
* worker 重试 3 次后把库置 `error` 并进 DLQ；
* 文档一直停在 `pending`，而库级失败没有 `failed_documents` 可渲染 ⇒
  界面只剩一个「异常」标签，看不到原因（这正是"索引显示异常"的现场）。

单测没发现：`pageindex-service/tests/test_service.py` 把 store 整个 mock 了，
行里手工带了 `index_engine` —— 与真实 schema 脱钩。本次同时补一条**静态 schema
契约**用例（`pageindex-service/tests/test_schema_contract.py`），从迁移里解析
列名，防止再次漂移。

幂等：`ADD COLUMN IF NOT EXISTS`，重跑安全。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_kb_index_engine"
down_revision: str | Sequence[str] | None = "0013_tenant_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.knowledge_bases "
        "ADD COLUMN IF NOT EXISTS index_engine varchar(64)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.knowledge_bases DROP COLUMN IF EXISTS index_engine"
    )
