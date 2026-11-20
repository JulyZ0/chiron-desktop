"""单文档重建（``scope='doc'``）的单元测试 —— 知识库验收闭环 P1-8。

钉住四件事：

1. ``scope=doc`` 缺 ``doc_id`` 直接 400：空 id 会静默退化成"整库增量"，
   而用户点的是"只重试这一篇"；
2. 文档必须属于**这个库**（否则越权者能借任意库的重建端点重置别人的文档）；
3. 只把这一篇重置为 ``pending`` 并投递**增量**任务（``force=False``）——
   不碰其余已就绪的文档，也不新建索引；
4. 库正在索引时幂等返回（不重复投递 = 不重复花 LLM 费用）。

不依赖 PG/Redis/PageIndex：pool、provider gate、重建锁与投递全部替换成桩。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.knowledge import reindex_knowledge_base


def _kb_row(**overrides):
    base = {
        "id": "kb-1",
        "user_id": "u1",
        "provider": "selfhosted",
        "status": "error",
        "enabled": True,
        "document_count": 3,
    }
    base.update(overrides)
    return base


def _pool(rows):
    """pool 桩：``fetchrow`` 按调用顺序返回 ``rows``（kb 行 → 文档行 → 计数行）。"""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(side_effect=list(rows))
    pool.execute = AsyncMock()
    return pool


COUNTS = {"failed": 1, "not_ready": 1}


def _patches(pool, *, enqueued=True):
    return (
        patch("app.api.knowledge.get_pool", return_value=pool),
        patch("app.api.knowledge._assert_provider_matches", AsyncMock(return_value=None)),
        patch("app.api.knowledge._resolve_provider_or_error", AsyncMock(return_value=object())),
        patch("app.api.knowledge._acquire_reindex_lock", AsyncMock(return_value=(True, None))),
        patch("app.api.knowledge._release_reindex_lock", AsyncMock(return_value=None)),
        patch(
            "app.api.knowledge._enqueue_kb_index",
            AsyncMock(return_value="task-1" if enqueued else ""),
        ),
    )


async def test_doc_scope_requires_doc_id() -> None:
    pool = _pool([])
    with _patches(pool)[0]:
        with pytest.raises(HTTPException) as exc:
            await reindex_knowledge_base("kb-1", "u1", scope="doc")
    assert exc.value.status_code == 400
    assert "doc_id" in exc.value.detail


async def test_doc_scope_rejects_unknown_scope() -> None:
    pool = _pool([])
    with _patches(pool)[0]:
        with pytest.raises(HTTPException) as exc:
            await reindex_knowledge_base("kb-1", "u1", scope="everything")
    assert exc.value.status_code == 400


async def test_doc_scope_rejects_document_from_another_kb() -> None:
    # 文档行查不到 = 不属于这个库 → 404，且**不能**继续投递
    pool = _pool([_kb_row(), None])
    p = _patches(pool)
    with p[0], p[1], p[2], p[5] as enqueue:
        with pytest.raises(HTTPException) as exc:
            await reindex_knowledge_base("kb-1", "u1", scope="doc", doc_id="doc-other")
    assert exc.value.status_code == 404
    enqueue.assert_not_awaited()


async def test_doc_scope_resets_only_that_document_and_enqueues_incremental() -> None:
    pool = _pool([_kb_row(), {"id": "doc-1"}, COUNTS])
    p = _patches(pool)
    with p[0], p[1], p[2], p[3], p[4], p[5] as enqueue:
        result = await reindex_knowledge_base("kb-1", "u1", scope="doc", doc_id="doc-1")

    assert result["enqueued"] is True
    assert result["scope"] == "doc"
    assert result["count"] == 1
    assert result["doc_id"] == "doc-1"
    # 增量投递：force=False —— 整库重建才会 force
    assert enqueue.await_args.kwargs["force"] is False

    resets = [
        call
        for call in pool.execute.await_args_list
        if "knowledge_documents" in str(call.args[0])
    ]
    assert len(resets) == 1, "只应重置这一篇文档"
    sql, doc_id, kb_id = resets[0].args
    assert "status = 'pending'" in sql
    assert doc_id == "doc-1"
    assert kb_id == "kb-1"


async def test_doc_scope_is_idempotent_while_indexing() -> None:
    pool = _pool([_kb_row(status="indexing"), {"id": "doc-1"}])
    p = _patches(pool)
    with p[0], p[1], p[2], p[3], p[5] as enqueue:
        result = await reindex_knowledge_base("kb-1", "u1", scope="doc", doc_id="doc-1")
    assert result["enqueued"] is False
    assert result["reason"] == "already indexing"
    assert result["count"] == 0
    enqueue.assert_not_awaited()


async def test_all_scope_still_forces_full_rebuild() -> None:
    pool = _pool([_kb_row(status="error"), COUNTS])
    p = _patches(pool)
    with p[0], p[1], p[2], p[3], p[4], p[5] as enqueue:
        result = await reindex_knowledge_base("kb-1", "u1", scope="all")
    assert enqueue.await_args.kwargs["force"] is True
    assert result["count"] == 3  # all = 全库篇数，而不是失败数
