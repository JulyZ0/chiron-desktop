"""`POST /v1/internal/market/kb-import` 的契约。

这个端点是市场「文档合集」安装的落点：网关传 `url`，由引擎自己出网抓取并直接
入库。用例锁死四件事，每一件都对应一个"看起来能跑、实际是坏"的形态：

1. **只有 `url`**：内联正文字段已移除。知识库 provider 的白名单是 PDF / Office
   二进制，md/txt 会被上传阶段 400 拒绝 —— 保留内联分支只会让发布方写下一份
   注定装不上的条目。
2. **抓取失败是 `ok=false` + 稳定原因码**，而不是抛栈 / 4xx：网关据此归类，
   不必解析错误串。
3. **入库被拒时状态码译成原因码**（`unsupported_document` 等），并把引擎的
   可操作原因原样带回。
4. **合集体积上限与知识库上传同口径**：两边数字不一致，就会出现"知识库页能传、
   市场装不了"的割裂。
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.api.internal_market as market_api
from app.api.knowledge import MAX_UPLOAD_BYTES
from app.main import create_app

#: 网关代理路径的身份形态：body 身份 + `X-Internal-Token`（由 conftest 注入）。
IDENTITY = {"user_id": "u1", "tenant_id": "1"}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, *, raw: bytes | None = None) -> list[tuple]:
    """替换出网抓取，返回调用记录（写定 `(url, max_bytes, timeout)`）。"""
    calls: list[tuple] = []

    async def _fake(url: str, max_bytes: int, timeout: float | None = None):
        calls.append((url, max_bytes, timeout))
        if raw is None:
            raise ValueError("cannot resolve host: example.invalid")
        return raw, "application/pdf"

    monkeypatch.setattr(market_api, "_fetch_document", _fake)
    return calls


def _patch_upload(monkeypatch: pytest.MonkeyPatch, *, raises: Exception | None = None) -> list[dict]:
    """替换入库：`kb-import` 是函数内 import，所以打在 `app.api.knowledge` 上。"""
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        if raises is not None:
            raise raises
        return {"id": "doc-1", "name": kwargs.get("name"), "status": "pending"}

    monkeypatch.setattr("app.api.knowledge.upload_document", _fake)
    return calls


async def _import(**body):
    async with _client() as c:
        return await c.post(
            "/v1/internal/market/kb-import",
            json={"kb_id": "kb-1", "user_id": "u1", "name": "a.pdf", **body},
            params=IDENTITY,
        )


def test_collection_limit_matches_knowledge_upload():
    """合集文档上限必须与知识库页上传同口径。

    不一致的表现是"知识库页能传、市场装不了" —— 这类割裂用户最难自己诊断，
    所以用一条断言把它钉死，而不是在两处各写一个魔法数字。
    """
    assert market_api.MAX_COLLECTION_DOCUMENT_BYTES == MAX_UPLOAD_BYTES


async def test_import_fetches_then_uploads(monkeypatch):
    raw = b"%PDF-1.7 fake"
    fetch_calls = _patch_fetch(monkeypatch, raw=raw)
    upload_calls = _patch_upload(monkeypatch)

    resp = await _import(url="https://example.com/a.pdf")

    body = resp.json()
    assert body["ok"] is True, body
    assert body["bytes"] == len(raw)
    # 抓取用的是**合集**上限与长超时，不是目录/manifest 那套。
    assert fetch_calls == [("https://example.com/a.pdf", market_api.MAX_COLLECTION_DOCUMENT_BYTES, 60.0)]
    # 入库拿到的是原始字节，不是 base64。
    assert upload_calls[0]["content"] == raw
    assert upload_calls[0]["file_size_bytes"] == len(raw)
    assert upload_calls[0]["user_id"] == "u1"


async def test_import_requires_url(monkeypatch):
    """内联正文已移除：只给 content 不再被接受。"""
    fetch_calls = _patch_fetch(monkeypatch, raw=b"x")
    upload_calls = _patch_upload(monkeypatch)

    resp = await _import(content=base64.b64encode(b"hello").decode("ascii"))

    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_request"
    assert "url" in body["detail"]
    assert fetch_calls == [] and upload_calls == []


async def test_import_missing_identity_is_rejected(monkeypatch):
    _patch_fetch(monkeypatch, raw=b"x")
    _patch_upload(monkeypatch)

    async with _client() as c:
        resp = await c.post(
            "/v1/internal/market/kb-import",
            json={"kb_id": "kb-1", "name": "a.pdf", "url": "https://example.com/a.pdf"},
            params=IDENTITY,
        )

    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "invalid_request"
    assert "user_id" in body["detail"]


async def test_import_fetch_failure_is_stable_code(monkeypatch):
    """抓取失败 → `ok=false` + `source_*`，HTTP 仍是 200（网关按 code 归类）。"""
    _patch_fetch(monkeypatch, raw=None)
    upload_calls = _patch_upload(monkeypatch)

    resp = await _import(url="https://example.invalid/a.pdf")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "source_unreachable"
    assert upload_calls == []


async def test_import_maps_upload_rejection_to_code(monkeypatch):
    """引擎拒绝入库时，把状态码译成稳定原因码，原因原样带回。"""
    _patch_fetch(monkeypatch, raw=b"%PDF")
    _patch_upload(
        monkeypatch,
        raises=HTTPException(status_code=400, detail="当前 provider 不支持该文件类型"),
    )

    body = (await _import(url="https://example.com/a.pdf")).json()

    assert body["ok"] is False
    assert body["error"] == "unsupported_document"
    # 可操作的原因不能被吞掉 —— 用户要靠它决定下一步。
    assert "不支持" in body["detail"]


async def test_import_maps_unexpected_upload_error(monkeypatch):
    """存储/DB 故障不是调用方错误：归 `kb_upload_failed`，不抛栈。"""
    _patch_fetch(monkeypatch, raw=b"%PDF")
    _patch_upload(monkeypatch, raises=RuntimeError("disk full"))

    body = (await _import(url="https://example.com/a.pdf")).json()

    assert body["ok"] is False
    assert body["error"] == "kb_upload_failed"


async def test_import_does_not_build_index(monkeypatch):
    """只落 `pending`，不触发索引 —— 索引会烧 provider 额度，得由用户显式发起。"""
    _patch_fetch(monkeypatch, raw=b"%PDF")
    _patch_upload(monkeypatch)

    body = (await _import(url="https://example.com/a.pdf")).json()

    assert body["ok"] is True
    assert body["document"]["status"] == "pending"
