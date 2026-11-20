"""索引进度聚合与**多库 RAG 合并**的单元测试（P1-1）。

覆盖两处"算错了用户会立刻不信任"的纯逻辑：

1. ``app/knowledge/progress.aggregate`` —— 库级进度由文档状态聚合而来。
   典型错法是"10 篇里 3 篇失败 ⇒ 进度停在 70%""全失败仍显示索引中"，
   前者看起来像卡死、后者让用户白等。
2. ``UnifiedChatHandler._retrieve_kb_context`` —— 多库检索的**分组 + 去重 + 上限**。
   同一份文档常被导入多个库（个人库 + 团队库），不去重时同一段文字会在提示词里
   出现两遍；不分组时模型说不清某句话出自哪个库。
"""

from __future__ import annotations

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.api.unified_executor import UnifiedChatHandler
from app.knowledge.progress import aggregate
from app.knowledge.status import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_INDEXING,
    STATUS_READY,
)


def _doc(doc_id: str, status: str, *, name: str = "", error: str = "", pages: int = 0):
    return {
        "id": doc_id,
        "name": name or f"{doc_id}.pdf",
        "status": status,
        "page_count": pages,
        "error_message": error,
        "file_size_bytes": 1024,
    }


# ── 进度聚合 ────────────────────────────────────────────────────────


class TestAggregate:
    def test_empty_library_keeps_library_status(self):
        out = aggregate(kb_id="kb-1", kb_status=STATUS_DISABLED, documents=[])
        assert out["total"] == 0
        assert out["percent"] == 0
        assert out["status"] == STATUS_DISABLED
        assert out["active"] is False
        assert out["documents"] == []

    def test_all_ready_is_ready_and_full(self):
        docs = [_doc("d1", "ready"), _doc("d2", "ready")]
        out = aggregate(kb_id="kb-1", kb_status=STATUS_INDEXING, documents=docs)
        # 文档全部就绪 ⇒ 即便库上的 status 还停在 indexing，也按 ready 报（worker 的
        # 库级回写可能延后一拍，界面不该因此继续转圈）
        assert out["status"] == STATUS_READY
        assert out["percent"] == 100
        assert out["active"] is False
        assert out["indexed"] == 2

    def test_failed_documents_count_as_processed(self):
        # 失败也算"处理完"：否则 10 篇里 3 篇失败，进度永远停在 70%，像卡住
        docs = [_doc("d1", "ready"), _doc("d2", "ready"), _doc("d3", "error", error="boom")]
        out = aggregate(kb_id="kb-1", kb_status=STATUS_INDEXING, documents=docs)
        assert out["percent"] == 100
        assert out["failed"] == 1
        assert out["status"] == STATUS_ERROR
        assert out["failed_documents"][0]["id"] == "d3"
        assert out["failed_documents"][0]["error_message"] == "boom"

    def test_indexing_wins_over_stale_error(self):
        # 重建期间旧的 error 文档仍在库里：先看 indexing，否则界面在重建已开始时
        # 继续报错并停止轮询
        docs = [_doc("d1", "error", error="old"), _doc("d2", "indexing")]
        out = aggregate(kb_id="kb-1", kb_status=STATUS_INDEXING, documents=docs)
        assert out["status"] == STATUS_INDEXING
        assert out["active"] is True
        assert out["percent"] == 50  # d1 已处理（失败）, d2 处理中

    def test_partial_progress_percent(self):
        docs = [_doc("d1", "ready"), _doc("d2", "pending"), _doc("d3", "pending"), _doc("d4", "pending")]
        out = aggregate(kb_id="kb-1", kb_status=STATUS_INDEXING, documents=docs)
        assert out["percent"] == 25
        assert out["pending"] == 3
        assert out["status"] == STATUS_INDEXING

    def test_library_error_message_wins_and_fallback_built(self):
        docs = [_doc("d1", "error", name="a.pdf", error="缺少原文对象")]
        out = aggregate(kb_id="kb-1", kb_status=STATUS_ERROR, kb_error="", documents=docs)
        # 库级原因为空时用逐篇原因拼一句，否则界面只显示"异常"两个字
        assert "a.pdf" in out["error_message"]
        assert "缺少原文对象" in out["error_message"]

        out2 = aggregate(kb_id="kb-1", kb_status=STATUS_ERROR, kb_error="索引位置准备失败", documents=docs)
        assert out2["error_message"] == "索引位置准备失败"

    def test_unknown_document_status_degrades_to_pending(self):
        # 未知取值按 pending：宁可显示"还没开始"，也不能谎报"已完成"
        out = aggregate(
            kb_id="kb-1", kb_status=STATUS_INDEXING, documents=[_doc("d1", "completed")]
        )
        assert out["documents"][0]["status"] == "pending"
        assert out["percent"] == 0

    def test_pending_only_keeps_library_status(self):
        out = aggregate(
            kb_id="kb-1", kb_status=STATUS_DISABLED, documents=[_doc("d1", "pending")]
        )
        assert out["status"] == STATUS_DISABLED
        assert out["active"] is False


# ── 多库合并 ────────────────────────────────────────────────────────


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args, **_kwargs):
        return list(self._rows)


def _patch_multi_kb(monkeypatch, *, names, results):
    """把 kb_search / get_pool 换成固定返回，返回被调用的 kb_id 列表。"""
    calls: list[str] = []

    async def _kb_search(kb_id: str, query: str, top_k: int = 5):  # noqa: ARG001
        calls.append(kb_id)
        value = results.get(kb_id)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("app.tools.kb.kb_search", _kb_search)
    monkeypatch.setattr(
        "app.api.unified_executor.get_pool",
        lambda: _FakePool([{"id": k, "name": v} for k, v in names.items()]),
    )
    return calls


async def test_multi_kb_context_groups_by_kb_and_dedupes(monkeypatch):
    results = {
        "kb-a": {
            "answer": "同一段答案",
            "citations": [
                # 同一份文档（同 doc_id/页/块）在两个库里都有 ⇒ 必须只出现一次
                {"doc_name": "共同.pdf", "page": 2, "block_id": "b0", "snippet": "重复片段", "doc_id": "shared"},
                {"doc_name": "a.pdf", "page": 1, "block_id": "b1", "snippet": "A 独有", "doc_id": "da"},
            ],
        },
        "kb-b": {
            "answer": "同一段答案",
            "citations": [
                {"doc_name": "共同.pdf", "page": 2, "block_id": "b0", "snippet": "重复片段", "doc_id": "shared"},
                {"doc_name": "b.pdf", "page": 3, "block_id": "b2", "snippet": "B 独有", "doc_id": "db"},
            ],
        },
    }
    _patch_multi_kb(monkeypatch, names={"kb-a": "库A", "kb-b": "库B"}, results=results)

    handler = UnifiedChatHandler()
    block, hits, citations = await handler._retrieve_kb_context(
        kb_ids=["kb-a", "kb-b", "kb-a"],  # 重复 id 只检索一次
        query="q",
        tenant_id=1,
        trace_id="t1",
        user_id="u1",
    )

    assert block.startswith("【知识库引用】")
    assert "### 库A" in block and "### 库B" in block
    # 跨库去重：同一段片段只注入一次（答案本身也去重）
    assert block.count("重复片段") == 1
    assert block.count("同一段答案") == 1
    # 引用坐标齐备（前端据此生成 /knowledge/:kbId?doc=:docId）
    assert {c["kb_id"] for c in citations} == {"kb-a", "kb-b"}
    assert all(c["doc_id"] for c in citations)
    assert hits == 4  # 每库 2 条 citation


async def test_multi_kb_context_caps_at_five(monkeypatch):
    kb_ids = [f"kb-{i}" for i in range(8)]
    results = {kb_id: {"answer": "", "citations": []} for kb_id in kb_ids}
    calls = _patch_multi_kb(monkeypatch, names={}, results=results)

    handler = UnifiedChatHandler()
    block, hits, citations = await handler._retrieve_kb_context(
        kb_ids=kb_ids, query="q", tenant_id=1, trace_id="t1", user_id="u1"
    )
    # 手写 URL 传 8 个库也只检索 5 个：库数就是首字节延迟的倍数
    assert calls == kb_ids[:5]
    assert block == ""
    assert hits == 0
    assert citations == []


async def test_multi_kb_context_survives_single_kb_failure(monkeypatch):
    results = {
        "kb-a": RuntimeError("db down"),
        "kb-b": {"answer": "B 的答案", "citations": [
            {"doc_name": "b.pdf", "page": 1, "block_id": "b1", "snippet": "片段", "doc_id": "db"},
        ]},
    }
    _patch_multi_kb(monkeypatch, names={"kb-a": "库A", "kb-b": "库B"}, results=results)

    handler = UnifiedChatHandler()
    block, hits, citations = await handler._retrieve_kb_context(
        kb_ids=["kb-a", "kb-b"], query="q", tenant_id=1, trace_id="t1", user_id="u1"
    )
    # 单个库失败不拖垮其余库
    assert "### 库B" in block
    assert "### 库A" not in block
    assert hits == 1
    assert citations[0]["kb_id"] == "kb-b"
