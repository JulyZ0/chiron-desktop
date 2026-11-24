"""H77：知识库检索必须按**调用者**授权，不能用库主身份绕过。

修复前的事实（PRD 9.10）：
- `app/api/agents.py` 的对话注入按 `kb_id` 直接查库，把**库主**的 `user_id` 传给
  `answer_question`，而后者只按 ID 加载、不比较调用者 —— 于是"知道别人的库 ID"
  就能把它人的私有库答案与引用注入到自己的对话里；
- 授权因此被"以资源所有者身份调用"绕过去了（这比"忘了校验"更隐蔽：代码里确实
  有身份，只是那身份属于资源方）。

这组用例钉住 service 层的**唯一**判定点：`answer_question` 现在要求调用者身份，
且用与列表/工具层同一份 `kb_access_allowed` 判据。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.knowledge import service as kb_service
from app.knowledge.service import KnowledgeAccessDeniedError, answer_question


class _FakePool:
    """只实现 answer_question 用到的两条查询。"""

    def __init__(self, kb_row: dict[str, Any] | None, docs: list[dict[str, Any]] | None = None):
        self.kb_row = kb_row
        self.docs = docs or []
        self.fail_retrieval = False

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "knowledge_bases" in sql:
            return self.kb_row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.docs


def _kb(owner: str = "u-owner", visibility: str = "private", tenant: int = 1) -> dict[str, Any]:
    return {
        "id": "kb-1",
        "tenant_id": tenant,
        "user_id": owner,
        "provider": "openai",
        "index_ref": "folder-1",
        "config": {},
        "status": "ready",
        "enabled": True,
        "visibility": visibility,
    }


class _Provider:
    name = "fake-provider"

    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, ctx: Any, *, question: str) -> Any:
        self.calls += 1
        return _Answer()


class _Answer:
    answer = "secret answer"
    citations: list[Any] = []


class _Health:
    ok = True
    detail = ""


async def _run(pool: _FakePool, provider: _Provider, **kwargs: Any):
    async def _resolve(*_a: Any, **_kw: Any):
        return provider, None, _Health()

    from app.knowledge import metrics as kb_metrics

    with patch.object(kb_service, "_pool", return_value=pool), patch.object(
        kb_service.registry, "resolve_for_user", side_effect=_resolve
    ), patch.object(kb_metrics, "record_retrieval", return_value=None):
        return await answer_question(kb_id="kb-1", question="q", **kwargs)


@pytest.mark.asyncio
async def test_missing_caller_identity_is_rejected() -> None:
    """没有调用者身份就没有授权依据 —— 必须拒绝，而不是沿用"库主"当调用者。"""
    pool = _FakePool(_kb())
    provider = _Provider()

    with pytest.raises(KnowledgeAccessDeniedError):
        await _run(pool, provider, user_id="u-owner")

    assert provider.calls == 0, "被拒后不得触达 provider（否则已经取到内容了）"


@pytest.mark.asyncio
async def test_foreign_private_kb_is_rejected_even_with_owner_identity() -> None:
    """典型攻击：调用者把**库主**的 user_id 当自己的传进来。"""
    pool = _FakePool(_kb(owner="u-owner", visibility="private"))
    provider = _Provider()

    with pytest.raises(KnowledgeAccessDeniedError):
        await _run(pool, provider, user_id="u-owner", caller_user_id="u-intruder", caller_tenant_id=1)

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_cross_tenant_shared_kb_is_rejected() -> None:
    pool = _FakePool(_kb(owner="u-owner", visibility="tenant", tenant=2))
    provider = _Provider()

    with pytest.raises(KnowledgeAccessDeniedError):
        await _run(pool, provider, user_id="u-owner", caller_user_id="u-intruder", caller_tenant_id=1)


@pytest.mark.asyncio
async def test_owner_and_public_and_tenant_shared_are_allowed() -> None:
    for row, caller, tenant in (
        (_kb(owner="u-caller", visibility="private", tenant=1), "u-caller", 1),
        (_kb(owner="u-owner", visibility="public", tenant=9), "u-caller", 1),
        (_kb(owner="u-owner", visibility="tenant", tenant=1), "u-caller", 1),
    ):
        pool = _FakePool(row, docs=[{"pageindex_doc_id": "d1"}])
        provider = _Provider()
        with patch.object(kb_service, "_context", return_value=object()):
            answer, _prov = await _run(
                pool, provider, user_id=str(row["user_id"]), caller_user_id=caller, caller_tenant_id=tenant
            )
        assert answer.answer == "secret answer"
        assert provider.calls == 1
