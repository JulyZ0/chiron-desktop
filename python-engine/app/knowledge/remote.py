"""引擎侧的 PageIndex 接入 —— 通过内网 HTTP 调用，**不再持有 SDK**。

S4 之后职责一分为三：

* **检索**：引擎 → ``pageindex-search``（本模块的 ``answer``）；
* **索引**：引擎只投递 ``engine:tasks:kb``，由 ``pageindex-worker`` 执行（本模块的
  ``submit_document`` 因此明确报错，而不是悄悄走进程内实现）；
* **位置/删除**：同样走服务（``prepare_index`` / ``delete_document``）。

**上层一行都不用改**：这里实现的是既有的 ``KnowledgeProvider`` 协议，``app/api/knowledge.py``、
``app/tools/kb.py``、``app/knowledge/service.py`` 只认协议。换实现不换契约。

**越界守卫在引擎侧也留一份**（纵深防御）：服务侧已经拦过，但引擎是唯一面向用户的一层，
引用解析不出归属时宁可拒绝返回 —— 这与 ``chat_result`` 时期的语义一致。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.knowledge.errors import ProviderFailedError, ProviderUnavailableError
from app.knowledge.providers.base import (
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_READY,
    AnswerResult,
    Citation,
    HealthResult,
    ProviderContext,
    SubmitResult,
    citation_doc_key,
    citation_in_scope,
)

logger = logging.getLogger(__name__)

_LOCAL_SUFFIXES = {".pdf"}
_CLOUD_SUFFIXES = {".pdf", ".pptx", ".ppt", ".docx", ".doc"}


class RemoteProvider:
    """``pageindex-search`` 的内网客户端（实现 KnowledgeProvider 协议）。"""

    def __init__(
        self,
        *,
        mode: str,
        api_key: str = "",
        folder_id: str = "",
        user_folder_id: str = "",
    ) -> None:
        self.name = mode                     # selfhosted | cloud（沿用既有取值语义）
        self._mode = mode
        self._api_key = api_key
        self._folder_id = folder_id
        self._user_folder_id = user_folder_id

    # ── 传输 ──

    def _base_url(self) -> str:
        """索引服务地址 —— **唯一读取入口**（见 ``app/knowledge/runtime.py``）。

        不直接读 ``settings.knowledge_service_url``：那个值会被后台「知识库」配置热改写，
        而本类实例可能早就建好了。经由 ``runtime.current_url()`` 读取，才能让
        "后台切了服务"对**下一次请求**立刻生效，而不是等下一个请求重新构造 provider。
        """
        from app.knowledge import runtime

        return runtime.current_url()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        base = self._base_url()
        if not base:
            # 前缀是给用户看的，括号里是给排查看的 —— 只留变量名时用户完全无从下手
            raise ProviderUnavailableError(
                "内置检索服务未启动（KNOWLEDGE_SERVICE_URL 未配置）：重启应用会自动拉起，"
                "或手动运行 pageindex-service"
            )
        token = str(getattr(settings, "internal_token", "") or "")
        if not token:
            raise ProviderUnavailableError("INTERNAL_TOKEN 未配置，无法调用知识库服务")
        try:
            async with httpx.AsyncClient(
                base_url=base,
                timeout=float(getattr(settings, "knowledge_service_timeout", 120.0)),
                headers={"X-Internal-Token": token},
            ) as client:
                resp = await client.post(path, json=payload)
        except Exception as exc:  # noqa: BLE001 — 服务不可达 = provider 不可用，不是 500
            raise ProviderUnavailableError(f"检索服务不可达（{base}）：{exc}") from exc
        if resp.status_code == 401:
            raise ProviderUnavailableError("检索服务拒绝内部令牌（INTERNAL_TOKEN 与引擎不一致）")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise ProviderFailedError(detail or f"知识库服务返回 {resp.status_code}")
        try:
            return dict(resp.json() or {})
        except Exception as exc:  # noqa: BLE001
            raise ProviderFailedError(f"知识库服务返回非 JSON: {exc}") from exc

    def _ctx_payload(self, ctx: ProviderContext) -> dict[str, Any]:
        # 逐库模型覆盖（P1-6）：只有显式配置过的键才会出现，空 dict 时服务侧
        # 完全按自己的解析顺序（env → 网关 /models）走，行为与引入本项之前逐字相同。
        from app.knowledge.settings_store import kb_model_overrides

        return {
            "mode": self._mode,
            "kb_id": ctx.kb_id,
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "doc_ids": list(ctx.doc_ids),
            "index_ref": ctx.index_ref or self._folder_id,
            "user_folder_id": self._user_folder_id,
            "api_key": self._api_key,
            # 自托管的 LLM 连接在**索引服务侧**配置（索引组件凭据独立轮换），这里不传
            "llm": {},
            # 模型覆盖随请求走：问答在服务侧执行，引擎不持有模型实例
            "models": kb_model_overrides(ctx.config),
        }

    # ── 协议实现 ──

    def health(self) -> HealthResult:
        """本地可判定部分（不触网）：服务地址与凭据是否齐备。

        真正的连通性是"测试连接"（``api/knowledge.py`` 的 verify）才做的 —— 启动路径上
        对每个用户发一次网络请求会把"某用户 key 失效"变成服务启动延迟。
        """
        base = self._base_url()
        if not base:
            # 这句话会原样显示在「知识库设置」的自托管卡片上，所以先说人话，
            # 括号里留变量名给排查用 —— 只给变量名时用户完全无从下手。
            return HealthResult(
                False,
                STATUS_DISABLED,
                "内置检索服务未启动（KNOWLEDGE_SERVICE_URL 未配置）：重启应用会自动拉起，"
                "或手动运行 pageindex-service",
            )
        if not str(getattr(settings, "internal_token", "") or ""):
            return HealthResult(
                False,
                STATUS_DISABLED,
                "内部凭据未就绪（INTERNAL_TOKEN 未配置），引擎与检索服务无法互信；请重启应用",
            )
        if self._mode == "cloud" and not self._api_key:
            return HealthResult(False, STATUS_DISABLED, "未配置 PageIndex Cloud API key")
        return HealthResult(True, STATUS_READY, "")

    async def probe(self) -> HealthResult:
        """真实探测（显式触发）：问服务的 /healthz。"""
        import httpx

        base = self._base_url()
        if not base:
            return HealthResult(
                False,
                STATUS_DISABLED,
                "内置检索服务未启动（KNOWLEDGE_SERVICE_URL 未配置）：重启应用会自动拉起，"
                "或手动运行 pageindex-service",
            )
        try:
            async with httpx.AsyncClient(base_url=base, timeout=10.0) as client:
                resp = await client.get("/healthz")
            body = dict(resp.json() or {})
            if resp.status_code >= 400:
                return HealthResult(
                    False, STATUS_ERROR, f"检索服务健康检查失败（HTTP {resp.status_code}，{base}）"
                )
            if not body.get("ok"):
                # 服务能连上、但自报不可用（例如索引模型未配置）：把它的原话透出去，
                # 否则用户只知道"连不上"，而真相是"连上了但没配模型"。
                status = str(body.get("status") or STATUS_ERROR)
                detail = str(body.get("detail") or "").strip()
                return HealthResult(
                    False,
                    status if status in (STATUS_DISABLED, STATUS_ERROR) else STATUS_ERROR,
                    f"{detail or '检索服务自报不可用'}（{base}）",
                )
            return HealthResult(True, STATUS_READY, "")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, STATUS_ERROR, f"检索服务不可达（{base}）：{exc}")

    def supports(self, filename: str) -> bool:
        suffix = filename[filename.rfind("."):].lower() if "." in filename else ""
        return suffix in (_CLOUD_SUFFIXES if self._mode == "cloud" else _LOCAL_SUFFIXES)

    async def prepare_index(self, ctx: ProviderContext) -> str:
        body = await self._post("/v1/prepare", self._ctx_payload(ctx))
        return str(body.get("index_ref") or "")

    async def submit_document(
        self, ctx: ProviderContext, *, doc_id: str, filename: str, data: bytes, file_type: str
    ) -> SubmitResult:
        # 引擎**不索引**：索引由 pageindex-worker 消费 engine:tasks:kb 执行。
        # 这里显式报错而不是"悄悄走进程内实现"——后者正是 S4 要消除的双路径。
        raise ProviderUnavailableError(
            "索引由 pageindex-worker 执行（引擎不装 PageIndex SDK）；请通过 /build 投递任务"
        )

    async def delete_document(self, ctx: ProviderContext, *, pageindex_doc_id: str) -> None:
        if not pageindex_doc_id:
            return
        try:
            await self._post(
                "/v1/delete",
                {**self._ctx_payload(ctx), "pageindex_doc_id": pageindex_doc_id},
            )
        except Exception as exc:  # noqa: BLE001 — 与既有语义一致：删除失败不阻断本地删除
            logger.warning("remote delete failed id=%s: %s", pageindex_doc_id, exc)

    async def answer(self, ctx: ProviderContext, *, question: str) -> AnswerResult:
        body = await self._post("/v1/answer", {**self._ctx_payload(ctx), "question": question})
        citations = [
            Citation(
                doc_name=str(item.get("doc_name") or ""),
                page=item.get("page") if isinstance(item.get("page"), int) else None,
                block_id=str(item.get("block_id") or ""),
                snippet=str(item.get("snippet") or ""),
                # 可定位坐标（P1-1）：doc_id 由文件名前缀反解，kb_id 来自本次请求的上下文。
                # 有了这两个字段，前端才能把引用点成 `/knowledge/:kbId?doc=:docId`。
                doc_id=citation_doc_key(
                    str(item.get("doc_name") or ""), ctx.doc_ids
                ),
                kb_id=ctx.kb_id,
            )
            for item in (body.get("citations") or [])
            if isinstance(item, dict)
        ]
        # 纵深防御：服务侧已拦过越界引用，引擎侧再确认一次（它才是面向用户的那层）
        strays = [
            c.doc_name
            for c in citations
            if c.doc_name and not citation_in_scope(c.doc_name, ctx.doc_ids)
        ]
        if strays:
            logger.warning("remote answer out-of-scope citations: %s", strays[:3])
            raise ProviderFailedError("检索结果包含知识库范围外的文档，已拒绝返回")
        return AnswerResult(answer=str(body.get("answer") or ""), citations=citations)

    def agent_tools(self, ctx: ProviderContext) -> list[Any]:
        """不暴露（账号级浏览工具；见 providers/base.py 的协议说明）。"""
        return []

    def agent_instructions(self, ctx: ProviderContext) -> str:
        return ""
