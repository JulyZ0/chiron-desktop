"""索引**进度聚合**：文档级状态 → 库级进度（纯函数，无 IO）。

## 为什么要这一层

`pageindex-service` 是**无状态检索服务**，它只暴露 `/healthz`、`/metrics`（队列深度、
worker 心跳计数）与几个同步端点，**没有"某个库/某篇文档的索引进度"这种任务端点**
（见 `pageindex-service/app/search_api.py`）。真正执行索引的 `pageindex-worker` 没有 HTTP
端口，它只把结果写回数据库（`app/index_worker.py` 的 `set_doc_indexing` /
`set_doc_ready` / `set_doc_error`）。

所以"进度"只能由引擎按**文档状态聚合**：文档的 `pending / indexing / ready / error`
就是唯一可信的进度事实源。把聚合放在纯函数里（不碰数据库）有两个好处：

1. 可单测 —— 进度算错（比如 8/10 完成却显示 0%，或全部失败仍显示"索引中"）是
   用户最直接的不信任来源，必须能被用例钉住；
2. 只有一份实现 —— API 端点与前端轮询读同一个口径，不会"接口说 80%、页面自己算 50%"。

## 一个刻意不做的优化

**不从 `/metrics` 推断进度**。那里的 `kb_processed_total` 是全服务的累计计数，
不属于任何一个库；拿它算百分比会让 A 库的进度随 B 库跑动而涨。
"""

from __future__ import annotations

from typing import Any

from app.knowledge.status import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_INDEXING,
    STATUS_READY,
)

#: 文档状态取值（DB 里的 `knowledge_documents.status`，由 worker 写入）。
DOC_PENDING = "pending"
DOC_INDEXING = "indexing"
DOC_READY = "ready"
DOC_ERROR = "error"

#: 库级状态的约定取值（与 `app/knowledge/status.py` 同源）。
_KB_STATUSES: frozenset[str] = frozenset(
    {STATUS_DISABLED, STATUS_INDEXING, STATUS_READY, STATUS_ERROR}
)

#: 只用于展示的分组顺序：未开始 → 进行中 → 完成 → 失败。
_DOC_STATUSES: tuple[str, ...] = (DOC_PENDING, DOC_INDEXING, DOC_READY, DOC_ERROR)


def _doc_item(row: Any) -> dict[str, Any]:
    """一行文档 → 进度条目（只取进度需要的列，缺列时按 pending 处理）。"""
    data = dict(row)
    status = str(data.get("status") or "").strip().lower() or DOC_PENDING
    if status not in _DOC_STATUSES:
        # 未知取值按 pending：宁可显示"还没开始"，也不能谎报"已完成"
        status = DOC_PENDING
    page_count = data.get("page_count")
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
        "status": status,
        "page_count": page_count if isinstance(page_count, int) else 0,
        # 失败原因按文档给出：库级 error_message 只是拼接，定位要看具体哪一篇
        "error_message": str(data.get("error_message") or ""),
        "file_size_bytes": data.get("file_size_bytes") or 0,
    }


def aggregate(
    *,
    kb_id: str,
    kb_status: str | None = None,
    kb_error: str | None = None,
    documents: list[Any] | None = None,
) -> dict[str, Any]:
    """按文档状态聚合出库级进度。

    返回字段（前端直接消费，不要在前端重算）：

    * ``status``：库级索引状态（``indexing`` / ``error`` / ``ready`` / ``disabled``）；
    * ``percent``：完成百分比（``ready + error`` / 总数）。失败也算"处理完"——
      否则 10 篇里 3 篇失败会让进度永远停在 70%，看起来像卡住；
    * ``active``：是否需要继续轮询（有文档或库本身在 ``indexing``）；
    * ``documents`` / ``failed_documents``：逐篇状态与失败原因。

    **状态优先级**（顺序即语义，改顺序前先看下面三条理由）：

    1. 有文档在 ``indexing``，或库本身 ``indexing`` 且**还有文档排队**（``pending``）
       → ``indexing``。第二条不能省：worker 逐篇处理，某篇先失败、其余还在排队时
       若据"已有失败"判成 ``error``，界面会停止轮询，而任务其实还在跑。
    2. 有失败文档 → ``error``（此时没有排队中的文档，即"跑完了但没跑全"）。
    3. 全部 ``ready`` → ``ready``。即便库上的 ``status`` 还停在 ``indexing``（worker 的
       库级回写晚一拍），也不该让界面继续转圈。
    4. 其余（空库 / 全部 pending 但 worker 还没取件）沿用库上的原值。
    """
    docs = [_doc_item(row) for row in (documents or [])]
    total = len(docs)
    counts = {name: 0 for name in _DOC_STATUSES}
    for doc in docs:
        counts[doc["status"]] += 1

    done = counts[DOC_READY] + counts[DOC_ERROR]
    percent = round(done * 100 / total) if total else 0

    failed_docs = [d for d in docs if d["status"] == DOC_ERROR]
    # 库级失败原因：worker 写的整句优先（它可能包含"索引位置准备失败"这类非文档级原因），
    # 否则用逐篇原因拼一句 —— 前者为空时用户否则只看到"异常"两个字。
    error_message = str(kb_error or "").strip()
    if not error_message and failed_docs:
        error_message = "; ".join(
            f"{d['name']}: {d['error_message']}" if d["error_message"] else d["name"]
            for d in failed_docs[:3]
        )

    kb_indexing = str(kb_status or "") == STATUS_INDEXING
    if counts[DOC_INDEXING] or (kb_indexing and counts[DOC_PENDING]):
        status = STATUS_INDEXING
    elif counts[DOC_ERROR]:
        status = STATUS_ERROR
    elif total and counts[DOC_READY] == total:
        status = STATUS_READY
    else:
        # 空库 / 全部 pending：沿用库上的值（新库是 disabled）。
        #
        # 历史取值（failed / building / active / draft）一律归一成 error：
        # 直接透传会让前端拼出 `knowledge.status_failed` 这种原始 key，
        # 也掩盖了"数据里存在非约定状态"这件事。error 至少与事实相符（该库不可用）。
        raw = str(kb_status or STATUS_DISABLED).strip().lower()
        status = raw if raw in _KB_STATUSES else STATUS_ERROR

    return {
        "kb_id": kb_id,
        "status": status,
        "percent": percent,
        "active": status == STATUS_INDEXING,
        "total": total,
        "indexed": counts[DOC_READY],
        "failed": counts[DOC_ERROR],
        "pending": counts[DOC_PENDING],
        "indexing": counts[DOC_INDEXING],
        "error_message": error_message,
        "documents": docs,
        "failed_documents": failed_docs,
    }
