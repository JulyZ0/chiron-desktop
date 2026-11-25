"""H44：PageIndex 引用作用域判定（解析不出即拒绝）。

缺陷形态：`citation_in_scope` 在"文档名里没有 `前缀-`"时直接 `return True` ——
fail-open。只要 PageIndex 把文件名改写成别的形状、或返回一条来路不明的引用，
越界内容就会原样返回给用户，知识库边界形同虚设。
"""

from __future__ import annotations

import pytest

from app.adapter import (
    OutOfScopeCitations,
    citation_doc_id,
    citation_in_scope,
    doc_prefix,
    guard_citations,
)

DOC_A = "abcdef0123456789abcdef0123456789"  # 32 hex（uuid 去连字符）
DOC_B = "11111111222233334444555566667777"


def test_doc_prefix_strips_hyphens() -> None:
    assert doc_prefix("abcdef01-2345-6789-abcd-ef0123456789") == DOC_A


def test_citation_of_own_document_is_in_scope() -> None:
    assert citation_in_scope(f"{DOC_A}-report.pdf", [DOC_A]) is True


def test_citation_of_other_document_is_out_of_scope() -> None:
    assert citation_in_scope(f"{DOC_B}-other.pdf", [DOC_A]) is False


@pytest.mark.parametrize("name", ["report.pdf", "no-prefix-here.txt", ""])
def test_unparsable_citation_is_rejected(name: str) -> None:
    """**这是本次修复的核心**：解析不出前缀不得再 fail-open 放行。"""
    assert citation_in_scope(name, [DOC_A]) is False


def test_citation_doc_id_returns_empty_without_separator() -> None:
    assert citation_doc_id("report.pdf") == ""
    assert citation_doc_id(f"{DOC_A}-x.pdf") == DOC_A


def test_guard_rejects_out_of_scope_citations() -> None:
    citations = [{"doc_name": f"{DOC_B}-other.pdf", "text": "秘密"}]
    with pytest.raises(OutOfScopeCitations):
        guard_citations(citations, [DOC_A])


def test_guard_rejects_unparsable_citation_now() -> None:
    """旧行为会放行这条；现在必须整条拒绝。"""
    citations = [{"doc_name": "unknown.pdf", "text": "来路不明"}]
    with pytest.raises(OutOfScopeCitations):
        guard_citations(citations, [DOC_A])


def test_guard_accepts_in_scope_citations() -> None:
    citations = [{"doc_name": f"{DOC_A}-report.pdf", "text": "自家内容"}]
    guard_citations(citations, [DOC_A])  # 不抛异常即通过
