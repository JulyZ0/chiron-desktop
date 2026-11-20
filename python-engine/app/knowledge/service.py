"""知识库业务编排：把数据库行、原文对象存储与 provider 串起来。

这一层只做"编排"，不含任何 PageIndex 细节（那是 provider 的事），也不做 HTTP
（那是 ``app/api/knowledge.py`` 的事）。队列任务（``kb_index``）与 API 共用这里的函数，
所以两条路径对"文档状态怎么流转"必须只有一份实现。

状态流转（文档级，见 0023）：

    pending ──(队列取件)──> indexing ──> ready
                                 └────> error（error_message 记录原因）

库级状态由文档聚合而来：任一文档 error ⇒ 库 error；全部 ready ⇒ 库 ready。
**不做乐观置 ready**：索引是真跑完才回写（旧实现把"投递任务"当成功，检索时才暴露空库）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from app.knowledge.errors import KnowledgeError, ProviderUnavailableError
from app.knowledge.providers import registry
from app.knowledge.providers.base import (
    AnswerResult,
    ProviderContext,
)
from app.knowledge.status import STATUS_INDEXING
from app.tools.context import to_tenant_int

logger = logging.getLogger(__name__)

#: 兼容旧引用（曾在此就地定义）；取值权威源是 ``app/knowledge/status.py``。
__all__ = ["STATUS_INDEXING"]


async def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 列的取回形态随驱动配置而异（dict 或 str）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _context(kb: Any, doc_ids: tuple[str, ...] = ()) -> ProviderContext:
    return ProviderContext(
        kb_id=str(kb["id"]),
        tenant_id=to_tenant_int(kb["tenant_id"]),
        user_id=str(kb["user_id"] or ""),
        index_ref=str(kb["index_ref"] or ""),
        config=_as_dict(kb["config"]),
        doc_ids=doc_ids,
    )


async def _set_kb_status(pool: Any, kb_id: str, status: str, error_message: str = "") -> None:
    await pool.execute(
        """UPDATE knowledge_bases
              SET status = $2, error_message = $3, updated_at = NOW()
            WHERE id = $1""",
        kb_id,
        status,
        error_message or None,
    )


async def _load_kb(pool: Any, kb_id: str) -> Any:
    """取 KB 行（含 ``enabled``）。

    ``enabled`` 必须一起取：检索入口要靠它判断"这个库被停用了没有"。漏掉这一列
    会让下面 ``_require_enabled`` 的判定静默失效（Row 取不存在的键会抛，但从
    dict 取不会）—— 于是"停用"退化成纯展示字段。
    """
    kb = await pool.fetchrow(
        """SELECT id, tenant_id, user_id, provider, index_ref, config, status, enabled
             FROM knowledge_bases WHERE id = $1""",
        kb_id,
    )
    if kb is None:
        raise KnowledgeError(f"knowledge base not found: {kb_id}")
    return kb


async def index_knowledge_base(*, kb_id: str, user_id: str) -> dict[str, Any]:
    """**索引已移出引擎**（S4）：由 ``pageindex-worker`` 消费 ``engine:tasks:kb`` 执行。

    引擎不装 PageIndex SDK（``pageindex → litellm → openai<3`` 与引擎的 ``openai 3.x``
    无法共存）。这里保留一个**显式的失败点**而不是静默降级：有人调到它，说明还在走旧路径，
    应当改为投递队列任务（见 ``app/api/knowledge.py::build_knowledge_base``）。
    """
    raise KnowledgeError(
        "索引由 pageindex-worker 执行（引擎不装 PageIndex SDK）；请通过 "
        "POST /v1/kb/{id}/build 投递任务"
    )


class KnowledgeBaseDisabledError(KnowledgeError):
    """知识库已被停用（用户开关关闭）。

    单独一个类型（而不是复用 ``ProviderUnavailableError``）：前者是**用户意图**、
    调用方应当提示"去把开关打开"，后者是**配置/上游故障**、提示"检查设置"。混在一起
    时前端只能给一句"不可用"，用户照着去查设置却找不到问题。
    """


def kb_enabled(kb_row: Any) -> bool:
    """行里的 ``enabled``；缺列时按启用处理（见 ``app/api/knowledge.py::_row_enabled``）。"""
    try:
        keys = kb_row.keys()
    except AttributeError:  # dict 之外的实现
        return True
    if "enabled" not in keys:
        return True
    value = kb_row["enabled"]
    return True if value is None else bool(value)


def _require_enabled(kb: Any) -> None:
    """**检索与索引的公共闸门**：停用的库在这里被拦住。

    放在 service 层而不是只放在 API 层：``kb_search``（Agent 工具）与 ``kb_index``
    （队列 worker 调用的 handle 路径）都走 service，只在 API 加等于留了两个后门 ——
    Agent 照样能检索停用的库，队列里排着的任务照样会给停用的库建索引。
    """
    if not kb_enabled(kb):
        raise KnowledgeBaseDisabledError(
            "该知识库已停用；请在知识库详情页重新启用后再检索"
        )


async def answer_question(
    *, kb_id: str, user_id: str, question: str
) -> tuple[AnswerResult, str]:
    """在知识库内提问。返回 (答案, provider 名)。

    检索范围 = 该库中**已就绪**文档的 provider doc_id 集合：PageIndex 没有"整库"隐式范围，
    不传文档就等于让模型在空集上推理。
    """
    pool = await _pool()
    kb = await _load_kb(pool, kb_id)
    _require_enabled(kb)

    provider, _settings, health = await registry.resolve_for_user(
        user_id, folder_id=str(kb["index_ref"] or "")
    )
    if provider is None or not health.ok:
        raise ProviderUnavailableError(health.detail or "provider 不可用")

    rows = await pool.fetch(
        """SELECT pageindex_doc_id FROM knowledge_documents
            WHERE knowledge_base_id = $1 AND status = 'ready'
              AND pageindex_doc_id IS NOT NULL""",
        kb_id,
    )
    doc_ids = tuple(str(row["pageindex_doc_id"]) for row in rows if row["pageindex_doc_id"])
    ctx = _context(kb, doc_ids)
    result = await provider.answer(ctx, question=question)
    return result, provider.name


def serialize_answer(result: AnswerResult) -> dict[str, Any]:
    """答案 → JSON 可序列化结构（API 与工具共用同一形状）。"""
    return {
        "answer": result.answer,
        "citations": [asdict(c) for c in result.citations],
    }


#: 状态端点返回的文档列 —— 只取进度需要的几列，不把 metadata / storage_key 带出去。
_STATUS_DOC_COLUMNS = (
    "id, name, status, page_count, error_message, file_size_bytes, updated_at"
)


async def index_status(*, kb_id: str) -> dict[str, Any]:
    """库级索引进度（按文档状态聚合，**没有** pageindex-service 的 job 端点可透传）。

    为什么在 service 而不是 API：队列 worker（``kb_index``）、状态端点、以及将来的
    巡检都要读同一份口径。聚合逻辑本身在 ``app/knowledge/progress.py``（纯函数），
    这里只负责取行。

    权限校验不在这里 —— service 不知道 HTTP 语义（403/404 的取舍见 API 层注释）。
    调用方必须**先**做 ``kb_access_allowed``，否则这个函数会把任意库的文档状态吐给
    任何知道 id 的人。
    """
    from app.knowledge.progress import aggregate

    pool = await _pool()
    kb = await pool.fetchrow(
        "SELECT id, status, error_message FROM knowledge_bases WHERE id = $1", kb_id
    )
    if kb is None:
        raise KnowledgeError(f"knowledge base not found: {kb_id}")
    rows = await pool.fetch(
        f"""SELECT {_STATUS_DOC_COLUMNS}
             FROM knowledge_documents
            WHERE knowledge_base_id = $1
            ORDER BY created_at""",
        kb_id,
    )
    return aggregate(
        kb_id=kb_id,
        kb_status=str(kb["status"] or ""),
        kb_error=str(kb["error_message"] or ""),
        documents=list(rows),
    )
