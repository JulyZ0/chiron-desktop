"""PageIndex 适配器的摘要并发必须收敛到本机可承受的范围。

背景：PageIndex flash 默认 64 路并发摘要。本地网关 / 单模型端点通常只有几条
并发额度，64 路会全部 429/超时，SDK 把它们聚合成
「Summary generation failed for all nodes」—— 用户看到的就是索引失败，
却看不到真正的 429。适配器必须显式传一个保守值（默认 4）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.adapter import Adapter, RequestContext


def test_adapter_passes_conservative_summary_concurrency(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeLocalClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import pageindex

    monkeypatch.setattr(pageindex, "PageIndexLocalClient", FakeLocalClient)

    cfg = SimpleNamespace(
        selfhosted_root=str(tmp_path),
        selfhosted_optimize="full",
        selfhosted_summary_concurrency=4,
        selfhosted_index_model="index-model",
        selfhosted_chat_model="chat-model",
        llm_backend=lambda **_kwargs: {},
    )
    models = SimpleNamespace(
        ready=lambda: True,
        index_model="index-model",
        chat_model="chat-model",
        llm_backend=lambda **_kwargs: {},
    )
    adapter = Adapter(mode="selfhosted", cfg=cfg, models=models)
    adapter._client(RequestContext(kb_id="kb1", tenant_id="1"))

    assert captured["summary_concurrency"] == 4
    assert captured["index_model"] == "index-model"
    assert captured["optimize"] == "full"


def test_summary_failure_retries_serially(monkeypatch, tmp_path):
    """并发摘要全失败 ⇒ 自动降到单路重试一次（429/瞬时超时的自救路径）。"""
    clients: list[dict] = []

    class FlakyLocalClient:
        def __init__(self, **kwargs):
            clients.append(kwargs)

        def submit_document(self, path, **_kwargs):
            if len(clients) == 1:
                raise RuntimeError(
                    "Summary generation failed for all nodes "
                    "(every summary call failed or returned empty)"
                )
            return {"doc_id": "doc-ok"}

        def get_document(self, _doc_id):
            return {"pageNum": 3}

    import pageindex

    monkeypatch.setattr(pageindex, "PageIndexLocalClient", FlakyLocalClient)
    cfg = SimpleNamespace(
        selfhosted_root=str(tmp_path),
        selfhosted_optimize="full",
        selfhosted_summary_concurrency=4,
        selfhosted_index_model="index-model",
        selfhosted_chat_model="chat-model",
        llm_backend=lambda **_kwargs: {},
    )
    models = SimpleNamespace(
        ready=lambda: True,
        index_model="index-model",
        chat_model="chat-model",
        llm_backend=lambda **_kwargs: {},
    )
    adapter = Adapter(mode="selfhosted", cfg=cfg, models=models)
    result = adapter.submit_bytes(
        RequestContext(kb_id="kb1", tenant_id="1"),
        doc_id="d1",
        filename="doc.pdf",
        data=b"%PDF-1.4 fake",
    )

    assert result.doc_id == "doc-ok"
    assert [c["summary_concurrency"] for c in clients] == [4, 1]
