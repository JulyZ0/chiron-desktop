"""G17：知识库文档预览按 ``offset`` 分页（回归）。

修前会怎样：端点 ``preview_document`` 只有"一次拿整块 content"，且引擎侧硬截在
``_PREVIEW_CHARS``（10000）字符 —— 前端想在预览里继续往后读，没有任何入口。
传 ``offset`` / ``limit`` 会直接 ``TypeError``，响应里也没有 ``next_offset`` / ``eof``
/ ``total_chars``，前端无从判断"还有没有下一页"。

本文件钉住四条契约（对应 PRD G17 的完成定义）：
① 不传 ``offset`` / ``limit`` 时与旧行为**逐字节一致**（content 仍是前 10000 字符，
   ``preview_truncated`` 仍是 `len(text) >= 10000`），新增字段只是附带；
② 分页两页拼起来等于全文；
③ ``eof`` / ``next_offset`` 正确（末页 eof=True、next_offset 落在全文末尾）；
④ 越界 ``offset`` 返回空内容且 eof=True（否则前端"加载更多"会死循环）。

这里的 pool / 对象存储全是桩：预览的取值逻辑与数据库无关。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

from app.api.knowledge import _PREVIEW_CHARS, preview_document

#: 分页用例用的页大小。刻意远小于 ``_PREVIEW_CHARS``，让"两页拼回全文"能在小文本上验证，
#: 同时另一批用例用长文本验证"能读到 10000 之后"——两者缺一不可。
PAGE = 4000


def _kb_row() -> dict:
    """预览只用到 owner + 可见性三列（``kb_access_allowed`` 的判据）。"""
    return {"id": "kb-1", "user_id": "u1", "visibility": "private", "tenant_id": "t1"}


def _doc_row(storage_key: str = "t1/kb-1/doc-1/a.txt") -> dict:
    return {
        "id": "doc-1",
        "knowledge_base_id": "kb-1",
        "name": "a.txt",
        "file_type": "txt",
        "file_size_bytes": 10,
        "page_count": 3,
        "status": "ready",
        "pageindex_doc_id": "pi-1",
        "error_message": None,
        "metadata": None,
        "storage_key": storage_key,
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }


class _FakeBlobStore:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def get(self, key: str) -> bytes:  # noqa: ARG002 — 桩不关心 key
        return self.data


async def _preview(text: str, storage_key: str = "t1/kb-1/doc-1/a.txt", **kwargs):
    """按给定原文调用预览，返回 ``data`` 段。"""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(side_effect=[_kb_row(), _doc_row(storage_key)])
    with (
        patch("app.api.knowledge.get_pool", return_value=pool),
        patch(
            "app.knowledge.blob.get_blob_store",
            return_value=_FakeBlobStore(text.encode("utf-8")),
        ),
    ):
        out = await preview_document("kb-1", "doc-1", "u1", **kwargs)
    return out["data"]


def _text(n: int) -> str:
    """构造长度恰好为 ``n`` 的可定位文本（每 16 字符一行，便于人工核对偏移）。"""
    unit = "0123456789abcdef"
    return (unit * (n // len(unit) + 1))[:n]


# ── ① 向后兼容：不传分页参数 ────────────────────────────────────────────────


async def test_without_paging_params_matches_legacy_behaviour() -> None:
    """修前：这里只断言 content/preview_truncated；新增字段不能改变旧口径。"""
    text = _text(_PREVIEW_CHARS * 2)
    data = await _preview(text)
    # 旧行为：内容硬截在 _PREVIEW_CHARS
    assert data["content"] == text[:_PREVIEW_CHARS]
    # 旧行为：`len(text) >= _PREVIEW_CHARS` 即视为截断（含恰好等于的边界）
    assert data["preview_truncated"] is True
    # 元信息字段一名不少（前端依赖它们渲染标题/页数）
    assert data["name"] == "a.txt" and data["page_count"] == 3 and data["status"] == "ready"


async def test_without_paging_params_short_text_is_not_truncated() -> None:
    """短文本：旧行为是"没截断"，且整段都能拿到。"""
    text = _text(120)
    data = await _preview(text)
    assert data["content"] == text
    assert data["preview_truncated"] is False
    assert data["total_chars"] == 120


# ── ② 分页拼回全文 ─────────────────────────────────────────────────────────


async def test_two_pages_concatenate_to_full_text() -> None:
    """两页拼起来等于全文（PRD 完成定义 ②）。"""
    text = _text(PAGE * 2)
    first = await _preview(text, offset=0, limit=PAGE)
    second = await _preview(text, offset=first["next_offset"], limit=PAGE)
    assert first["content"] + second["content"] == text
    assert len(first["content"]) == PAGE and len(second["content"]) == PAGE


async def test_paging_reaches_beyond_legacy_cap() -> None:
    """修前：10000 字符之后的内容永远拿不到。分页必须能读到那里。"""
    text = _text(_PREVIEW_CHARS * 2 + 500)
    last = await _preview(text, offset=_PREVIEW_CHARS, limit=PAGE)
    assert last["content"] == text[_PREVIEW_CHARS : _PREVIEW_CHARS + PAGE]
    assert last["content"], "分页不能把 10000 之后的内容一律当空"
    assert last["offset"] == _PREVIEW_CHARS


# ── ③ eof / next_offset ────────────────────────────────────────────────────


async def test_next_offset_and_eof_are_correct() -> None:
    """逐页前进：offset 单调、末页 eof=True 且 next_offset 落在全文末尾。"""
    text = _text(PAGE * 2 + 100)
    offset = 0
    pages = []
    for _ in range(10):  # 上限 10 页，防止实现出错时这里变成死循环
        page = await _preview(text, offset=offset, limit=PAGE)
        pages.append(page)
        assert page["total_chars"] == len(text)
        assert page["offset"] == offset
        if page["eof"]:
            break
        offset = page["next_offset"]
        assert offset > page["offset"], "eof=False 时 next_offset 必须前进"
    else:  # pragma: no cover - 只有实现坏掉才会走到
        raise AssertionError("分页没有在 10 页内结束")

    assert len(pages) == 3
    assert "".join(p["content"] for p in pages) == text
    assert pages[0]["eof"] is False and pages[1]["eof"] is False
    assert pages[-1]["eof"] is True
    assert pages[-1]["next_offset"] == len(text)


# ── ④ 越界 offset ──────────────────────────────────────────────────────────


async def test_offset_beyond_end_returns_empty_and_eof() -> None:
    """越界 offset：空内容 + eof=True（否则前端会一直显示"加载更多"）。"""
    text = _text(300)
    data = await _preview(text, offset=len(text) + 5000, limit=PAGE)
    assert data["content"] == ""
    assert data["eof"] is True
    assert data["next_offset"] == len(text)
    assert data["total_chars"] == len(text)


async def test_offset_at_exact_end_is_empty_and_eof() -> None:
    """边界：offset == total_chars 也是"没有下一页"。"""
    text = _text(300)
    data = await _preview(text, offset=len(text))
    assert data["content"] == ""
    assert data["eof"] is True


# ── 防御：超大 limit 与非法 offset ──────────────────────────────────────────


async def test_huge_limit_is_clamped_to_single_page() -> None:
    """limit 由调用方给，但单页必须有上限：否则一次请求又能把整本书拖进响应。"""
    text = _text(_PREVIEW_CHARS * 2)
    data = await _preview(text, offset=0, limit=10**9)
    assert len(data["content"]) < len(text), "超大 limit 必须被夹到单页上限"
    assert data["eof"] is False


async def test_negative_offset_is_clamped_to_zero() -> None:
    """负数 offset 按 0 处理：报 500 不如给第一页。"""
    text = _text(200)
    data = await _preview(text, offset=-42, limit=100)
    assert data["offset"] == 0
    assert data["content"] == text[:100]


# ── 无原文副本时也要给得出分页字段 ─────────────────────────────────────────


async def test_document_without_local_copy_reports_eof() -> None:
    """云端文档没有本机原文：没有内容可翻页，必须 eof=True（不能诱导前端去翻页）。"""
    data = await _preview("", storage_key="")
    assert data["content"] == ""
    assert data["eof"] is True
    assert data["total_chars"] == 0
    assert data["preview_note"], "没有原文时要说明原因"
