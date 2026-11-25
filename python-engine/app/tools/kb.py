"""kb 工具集 — 知识库能力工具化。

- ``kb_list``：列出当前用户可见的知识库（自有 + public + 同租户共享）
- ``kb_search``：在指定知识库内提问（PageIndex 推理式检索），返回答案与引用

**工具名刻意保持不变**：它们是 agent 能力面的稳定契约 —— ``app/core/capabilities``、
``app/core/task_router``、``app/core/agent_skill_selector`` 都按这些名字编排与选择能力，
改名等于在所有编排点上做一次同步手术。换掉的是实现（向量 + FTS → PageIndex）。

**provider 解析用知识库所有者的设置**：Cloud key 是用户级的（只有所有者配过），
调用者身份只用于权限校验（``kb_access_allowed``）。两件事混用会得到"能看见库却查不了"
（拿别人的身份去找 key）。

**返回形状变了**：旧 ``kb_search`` 返回 ``{results: [片段]}``，现在返回
``{answer, citations}`` —— PageIndex 是推理式检索，没有"top-k 相似片段"这一层。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from app.config import DEFAULT_TENANT_ID
from app.kb_access import kb_access_allowed
from app.knowledge.errors import KnowledgeError
from app.tools.context import get_tenant_id_int, get_user_id
from app.tools.registry import registry

logger = logging.getLogger(__name__)


async def answer_question(*args: Any, **kwargs: Any) -> Any:
    """惰性转发到 ``app.knowledge.service.answer_question``（H77）。

    为什么要有这一层而不是模块级直接 import：
    ``app.knowledge.service`` 的导入链会回到 ``app.tools`` 包（
    service → providers.registry → settings_store → tools.context → tools/__init__
    → tools.kb → service 半初始化），模块级导入会成环，表现是"直接 import 服务层
    就 ImportError"。

    为什么不能干脆删掉这个模块属性：既有单测用
    ``patch("app.tools.kb.answer_question", ...)`` 打桩，删掉就等于让这条路径
    从"有覆盖"退回"没人守"。
    """
    from app.knowledge.service import answer_question as _impl

    return await _impl(*args, **kwargs)


async def _pool() -> Any:
    from app.db import get_pool

    try:
        return get_pool()
    except RuntimeError as e:
        raise RuntimeError(f"database not available: {e}") from e


async def kb_list(query: str = "", limit: int = 20) -> dict[str, Any]:
    """列出当前用户可见的知识库，可按名称过滤。"""
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}
    if limit <= 0 or limit > 100:
        limit = 20
    try:
        pool = await _pool()
    except RuntimeError as e:
        return {"error": str(e)}

    # `tenant_id` 是 smallint：asyncpg 收到 str 会直接抛
    # `invalid input for query argument $2: '1' (expected int)`，而不是返回空列表。
    # 用 `get_tenant_id_int()` 而不是 `get_tenant_id()`（后者是给格式化场景用的 str）。
    tenant_id = get_tenant_id_int() or DEFAULT_TENANT_ID
    params: list[Any] = [user_id, tenant_id]
    where = (
        "(user_id = $1 OR visibility = 'public'"
        " OR (visibility = 'tenant' AND tenant_id = $2))"
    )
    if query:
        where += f" AND name ILIKE ${len(params) + 1}"
        params.append(f"%{query}%")
    params.append(limit)

    rows = await pool.fetch(
        f"""SELECT id, name, COALESCE(description, '') AS description, provider, visibility,
                   status, document_count
              FROM knowledge_bases
             WHERE {where}
             ORDER BY created_at DESC
             LIMIT ${len(params)}""",
        *params,
    )
    kbs = [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "provider": r["provider"],
            "visibility": r["visibility"],
            "status": r["status"],
            "document_count": r["document_count"],
        }
        for r in rows
    ]
    return {"count": len(kbs), "knowledge_bases": kbs}


async def kb_search(kb_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
    """在指定知识库内提问，返回答案与引用。

    ``top_k`` 仅为兼容旧调用方保留（推理式检索由 provider 决定读多少节点）。
    """
    if not kb_id or not query:
        return {"error": "kb_id and query are required"}
    user_id = get_user_id() or ""
    if not user_id:
        return {"error": "authentication context missing"}
    try:
        pool = await _pool()
    except RuntimeError as e:
        return {"error": str(e)}

    kb_row = await pool.fetchrow(
        """SELECT id, user_id, tenant_id, visibility, provider, status
             FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb_row is None:
        return {"error": f"knowledge base not found: {kb_id}"}
    if not kb_access_allowed(kb_row, user_id, get_tenant_id_int()):
        return {"error": "not authorized to query this knowledge base"}

    owner_id = str(kb_row["user_id"] or "")
    try:
        result, provider_name = await answer_question(
            kb_id=kb_id,
            user_id=owner_id,
            question=query,
            # H77：库主身份只用于解析 provider；授权判据用**调用者**身份。
            caller_user_id=str(user_id or ''),
            caller_tenant_id=int(get_tenant_id_int()),
        )
    except KnowledgeError as exc:
        logger.warning("kb_search failed (kb_id=%s): %s", kb_id, exc)
        return {"error": str(exc), "provider": str(kb_row["provider"] or "")}

    citations = [asdict(c) for c in result.citations]
    return {
        "provider": provider_name,
        "answer": result.answer,
        "citations": citations,
        "count": len(citations),
    }


registry.register(
    name="kb_list",
    description=(
        "List knowledge bases visible to the current user (own + public + same-tenant). "
        "Use this before kb_search to find a knowledge base id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "default": "", "description": "Optional name filter"},
            "limit": {"type": "integer", "default": 20},
        },
    },
    handler=kb_list,
    # G15：只读知识库清单 ⇒ 可并发。
    concurrency_safe=True,
)

registry.register(
    name="kb_search",
    description=(
        "Ask a question against a knowledge base; returns a reasoned answer with citations "
        "(document name / page). Use kb_list first to obtain kb_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kb_id": {"type": "string", "description": "Knowledge base id (from kb_list)"},
            "query": {"type": "string", "description": "Natural-language question"},
            "top_k": {"type": "integer", "default": 5, "description": "Kept for compatibility"},
        },
        "required": ["kb_id", "query"],
    },
    handler=kb_search,
    # G15：只读知识库检索 ⇒ 可并发。
    concurrency_safe=True,
)
