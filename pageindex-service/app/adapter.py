"""PageIndex 调用面 —— 服务与 worker 共用的**唯一出口**。

## 为什么这个文件与引擎侧的 provider "看起来重复"

引擎镜像装不了 PageIndex SDK（pageindex → litellm → openai<3，引擎固定 openai 3.x），
所以两边**不能共享代码**。这份适配器是"索引侧/检索侧"的实现，引擎侧只保留一个 HTTP
客户端 + 一份轻量的引用守卫（纵深防御）。重复是隔离的代价，刻意接受。

## 三条必须留在这里的东西

1. **文件名前缀 = 去连字符的 doc_id**（``<prefix>-<随机>_<原名>``）：引用才能映射回
   文档做归属校验；uuid 自带 4 个连字符，直接用它会截在错误位置（用 `-` 分割只取到第一段）。
2. **越界引用即拒绝返回**：PageIndex 的 ``doc_id`` 官方语义是 "targets at the prompt
   level"（引导，不是硬范围，0.4.4 release notes 原话）。只删引用不行 —— 答案正文可能
   已经写进了范围外内容。
3. **folder 两层**：顶层以 user_id 命名（用户在 PageIndex 账号里的 Chiron 空间），其下每库
   一层。它是**组织与审计**手段，不是安全边界。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_FOLDER_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: local 只吃 text-based PDF；Cloud 支持 PDF/PPTX/Word
_LOCAL_SUFFIXES = {".pdf"}
_CLOUD_SUFFIXES = {".pdf", ".pptx", ".ppt", ".docx", ".doc"}


class _ModelErrorCapture(logging.Handler):
    """抓取 PageIndex/litellm 在摘要失败时打到 root logger 的真实错误。

    PageIndex 把每个节点的异常吞成空摘要，最后只抛一句 "summary generation
    failed"；真正的 429/上下文超限/401 只出现在 logging.error 里。索引失败时
    把这句最近错误带出去，用户才不用去翻进程日志。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.last = ""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 纯旁路
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if "LLM completion failed" in msg or msg.startswith("Error:"):
            # 错误串可能带 URL/模型名，但不该带凭据；保守地抹掉常见 key 形态。
            self.last = re.sub(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+", r"***", msg)[-500:]


class PageIndexError(RuntimeError):
    """适配器抛出的统一错误（服务侧映射成 HTTP 状态码）。"""


class OutOfScopeCitations(PageIndexError):
    """检索结果含范围外引用 —— **整条拒绝返回**。"""


class UnsupportedDocument(PageIndexError):
    """当前模式不支持该文档类型。"""


def sdk_version() -> str:
    """当前 PageIndex SDK 版本（写入 knowledge_bases.index_engine，并出现在 /healthz）。"""
    try:
        import importlib.metadata as md

        return f"pageindex=={md.version('pageindex')}"
    except Exception:  # noqa: BLE001
        return "pageindex==unknown"


# ── 引用归属（与引擎侧同一套语义）──────────────────────────────────────────


def doc_prefix(doc_id: str) -> str:
    """文档在索引侧的文件名前缀：去连字符的 32 位十六进制（uuid 带 4 个连字符）。"""
    return (doc_id or "").replace("-", "")


def citation_doc_id(doc_name: str) -> str:
    head, sep, _ = (doc_name or "").strip().partition("-")
    return head if sep else ""


def citation_in_scope(doc_name: str, doc_ids: Iterable[str]) -> bool:
    """引用是否属于本次检索的文档集合（H44：**解析不出即拒绝**）。

    旧实现在"文档名里没有 `前缀-`"时直接 `return True` —— 那是 fail-open：
    只要 PageIndex 把文件名改写成别的形状（或返回一条来路不明的引用），越界引用
    就会原样返回给用户，知识库边界形同虚设。

    现在的口径与引擎侧一致：**无法解析 ⇒ 不认**。代价是 PageIndex 改写命名后可能
    出现"该显示的引用被挡掉"；这比"越界内容被当成自家知识返回"要轻，而且会留
    warning，能立刻发现。
    """
    parsed = citation_doc_id(doc_name)
    if not parsed:
        logger.warning("knowledge: citation without a recognizable doc prefix: %r", doc_name)
        return False
    return parsed in {doc_prefix(d) for d in doc_ids if d}
def guard_citations(citations: list[dict[str, Any]], doc_ids: Iterable[str]) -> None:
    allowed = {d for d in doc_ids if d}
    strays = [
        c.get("doc_name")
        for c in citations
        if c.get("doc_name") and not citation_in_scope(str(c["doc_name"]), allowed)
    ]
    if strays:
        logger.warning("knowledge: out-of-scope citations: %s", strays[:3])
        raise OutOfScopeCitations("检索结果包含知识库范围外的文档，已拒绝返回")


def _folder_name(value: str, fallback: str) -> str:
    cleaned = _UNSAFE_FOLDER_RE.sub("-", value or "").strip("-")
    return cleaned[:60] or fallback


@dataclass(frozen=True)
class RequestContext:
    """一次请求的上下文（服务与 worker 都用它传参）。"""

    kb_id: str
    tenant_id: str = ""
    user_id: str = ""
    doc_ids: tuple[str, ...] = ()
    index_ref: str = ""       # 库 folder（cloud）
    user_folder_id: str = ""   # 用户 folder（cloud）
    api_key: str = ""          # 用户级 Cloud key（不出现在日志里）
    llm: dict[str, Any] = field(default_factory=dict)  # {api_key, base_url}（local 建树/问答）

    def redacted(self) -> dict[str, Any]:
        """可安全进日志的摘要（凭据只报"有没有"）。"""
        return {
            "kb_id": self.kb_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "doc_count": len(self.doc_ids),
            "has_api_key": bool(self.api_key),
            "has_llm": bool(self.llm),
        }


@dataclass(frozen=True)
class SubmitResult:
    doc_id: str
    page_count: int = 0


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)


class Adapter:
    """一个请求一个实例（无跨请求状态，便于服务化与水平扩展）。"""

    def __init__(self, *, mode: str, cfg: Any, models: Any = None) -> None:
        self._mode = mode
        self._cfg = cfg
        # 解析后的模型（env → 网关 /models，见 app/models.py）。None = 只看 cfg（老行为）。
        self._models = models

    # ── 客户端 ──

    def _selfhosted_models(self, *, session_id: str = "") -> tuple[str, str, dict[str, object]]:
        """自托管索引/问答模型与 LLM 连接（**唯一的取值口径**）。

        顺序：``models``（env → 网关 /models 解析结果）优先，回落到 ``cfg`` 的 env 直读值。
        两个来源并存是有意的：前者覆盖"模型只在网关 PG 里配过"的桌面形态，后者保证
        手工 export 了 ``KNOWLEDGE_SELFHOSTED_*`` 的部署行为**逐字不变**。
        """
        models = self._models
        if models is not None and getattr(models, "ready", lambda: False)():
            return (
                models.index_model or models.chat_model,
                models.chat_model,
                models.llm_backend(session_id=session_id),
            )
        return (
            self._cfg.selfhosted_index_model,
            self._cfg.selfhosted_chat_model,
            self._cfg.llm_backend(session_id=session_id),
        )

    def _client(self, ctx: RequestContext, *, summary_concurrency: int | None = None) -> Any:
        if self._mode == "cloud":
            from pageindex import PageIndexClient

            if not ctx.api_key:
                raise PageIndexError("Cloud 模式缺少用户 API key")
            return PageIndexClient(
                api_key=ctx.api_key,
                index="cloud",
                chat_model=self._cfg.cloud_chat_model,
                chat_backend=ctx.llm or None,
            )

        from pageindex import PageIndexLocalClient

        index_model, chat_model, cfg_backend = self._selfhosted_models(
            session_id=f"chiron-kb-{ctx.kb_id or 'default'}"
        )
        if not index_model or not chat_model:
            # 不把空模型名交给 SDK（它会在内部拼出一个 404 请求，报错点离原因很远）
            from app.models import unavailable_detail

            if self._models is not None:
                raise PageIndexError(unavailable_detail(self._models))
            raise PageIndexError("自托管索引/问答模型未配置（KNOWLEDGE_SELFHOSTED_*）")
        storage = self.storage_path(ctx)
        # search 服务以 **只读** 挂载索引卷（worker 才是写方）：在 :ro 上 mkdir 会抛 EROFS。
        # 目录由 worker 建；这里只在不存在时尝试创建，失败时给出可定位的错误，
        # 而不是把异常埋进 SDK 内部。
        if not Path(storage).is_dir():
            try:
                Path(storage).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PageIndexError(
                    f"索引目录不可用: {storage}"
                    "（search 侧应为只读挂载，目录需由 pageindex-worker 先创建）"
                ) from exc
        return PageIndexLocalClient(
            index_model=index_model,
            index_backend=ctx.llm or cfg_backend or None,
            chat_model=chat_model,
            chat_backend=ctx.llm or cfg_backend or None,
            storage_path=storage,
            # 显式传（0.4.4 起默认 "full" = 加一轮 LLM expand）：不显式传就会跟着默认值漂移，
            # 而默认值本身在版本间会变，索引成本与耗时也跟着变。
            optimize=self._cfg.selfhosted_optimize,
            # PageIndex flash 默认 64 路并发摘要。本地网关/单模型端点扛不住：
            # 全部请求 429/超时后 SDK 只会抛 "summary generation failed"，
            # 真正的错误被聚合掉了。默认收到 4 路，可用 env 调整。
            summary_concurrency=summary_concurrency or getattr(
                self._cfg, "selfhosted_summary_concurrency", 4
            ) or 4,
        )

    def storage_path(self, ctx: RequestContext) -> str:
        """local 模式的索引目录：**租户/库**两级（库级就是它的隔离边界）。"""
        return os.path.join(self._cfg.selfhosted_root, ctx.tenant_id or "_", ctx.kb_id or "_")

    def supports(self, filename: str) -> bool:
        suffix = Path(filename).suffix.lower()
        return suffix in (_CLOUD_SUFFIXES if self._mode == "cloud" else _LOCAL_SUFFIXES)

    # ── folder（仅 cloud）──

    def _find_folder(self, client: Any, name: str, parent_id: str | None) -> str:
        data = client.list_folders(parent_folder_id=parent_id) or {}
        for item in data.get("folders") or []:
            if not isinstance(item, dict) or str(item.get("name") or "") != name:
                continue
            if (item.get("parent_folder_id") or None) == parent_id:
                return str(item.get("id") or "")
        return ""

    def _ensure_folder(self, client: Any, name: str, parent_id: str | None) -> str:
        found = self._find_folder(client, name, parent_id)
        if found:
            return found
        try:
            created = client.create_folder(name, parent_folder_id=parent_id)
        except Exception as exc:  # noqa: BLE001 — 并发下可能已被另一任务建好
            again = self._find_folder(client, name, parent_id)
            if again:
                return again
            raise PageIndexError(f"创建 PageIndex folder 失败: {exc}") from exc
        folder_id = str(((created or {}).get("folder") or {}).get("id") or "")
        if not folder_id:
            raise PageIndexError("创建 PageIndex folder 未返回 id")
        return folder_id

    def ensure_kb_folder(self, ctx: RequestContext) -> tuple[str, str]:
        """确保"用户空间 → 库"两层存在，返回 (库 folder, 用户 folder)。幂等。"""
        if self._mode != "cloud":
            return "", ""
        if ctx.index_ref:
            return ctx.index_ref, ctx.user_folder_id
        client = self._client(ctx)
        user_folder = ctx.user_folder_id or self._ensure_folder(
            client, _folder_name(ctx.user_id, "chiron-user"), None
        )
        kb_folder = self._ensure_folder(
            client, _folder_name(ctx.kb_id, "kb"), user_folder
        )
        return kb_folder, user_folder

    # ── 索引 ──

    def submit_bytes(
        self, ctx: RequestContext, *, doc_id: str, filename: str, data: bytes
    ) -> SubmitResult:
        if not self.supports(filename or f"{doc_id}.pdf"):
            raise UnsupportedDocument(
                f"{self._mode} 模式不支持该类型: {Path(filename).suffix or '未知'}"
            )
        client = self._client(ctx)
        folder_id = ""
        if self._mode == "cloud":
            folder_id, _ = self.ensure_kb_folder(ctx)

        stem = _UNSAFE_NAME_RE.sub("_", Path(filename).stem)[:60] or "doc"
        suffix = Path(filename).suffix.lower() or ".pdf"
        fd, path = tempfile.mkstemp(prefix=f"{doc_prefix(doc_id)}-", suffix=f"_{stem}{suffix}")
        capture = _ModelErrorCapture()
        root_logger = logging.getLogger()
        root_logger.addHandler(capture)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                result = client.submit_document(path, folder_id=folder_id or None, wait=True)
            except Exception as first_exc:  # noqa: BLE001
                # 摘要聚合失败通常是并发打满：PageIndex 把所有单次错误吞成一句
                # "summary generation failed"。退到单路重试一次，能救回 429/瞬时
                # 超时；仍是上下文/凭据问题则第二次会给出同样的原因（由外层补全）。
                if (
                    self._mode == "selfhosted"
                    and "Summary generation failed for all nodes" in str(first_exc)
                    and (getattr(self._cfg, "selfhosted_summary_concurrency", 4) or 4) > 1
                ):
                    logger.warning(
                        "pageindex summary failed with concurrency=%s; retrying serially",
                        getattr(self._cfg, "selfhosted_summary_concurrency", 4),
                    )
                    client = self._client(ctx, summary_concurrency=1)
                    result = client.submit_document(path, folder_id=folder_id or None, wait=True)
                else:
                    raise
            provider_doc_id = str((result or {}).get("doc_id") or "")
            page_count = 0
            try:
                meta = client.get_document(provider_doc_id) or {}
                page_count = int(meta.get("pageNum") or 0)
            except Exception:  # noqa: BLE001 — 页数拿不到不影响索引成功
                page_count = 0
            return SubmitResult(doc_id=provider_doc_id, page_count=page_count)
        except PageIndexError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or f"{type(exc).__name__}"
            if "Summary generation failed for all nodes" in message:
                model = self._selfhosted_models()[0] if self._mode != "cloud" else "cloud"
                concurrency = getattr(self._cfg, "selfhosted_summary_concurrency", 4) or 4
                message += (
                    f"（索引模型 {model}，摘要并发 {concurrency}；"
                    "请检查该模型端点/key 与上下文窗口，"
                    "或调低 KNOWLEDGE_SUMMARY_CONCURRENCY 后重建索引）"
                )
            if capture.last and capture.last not in message:
                message += f"；最近一次模型调用错误：{capture.last}"
            raise PageIndexError(f"索引文档失败: {message}") from exc
        finally:
            root_logger.removeHandler(capture)
            try:
                os.unlink(path)
            except OSError:
                pass

    def delete_document(self, ctx: RequestContext, pageindex_doc_id: str) -> None:
        if not pageindex_doc_id:
            return
        try:
            self._client(ctx).delete_document(pageindex_doc_id)
        except Exception as exc:  # noqa: BLE001 — 删除失败不该让上层事务回滚
            logger.warning("delete_document failed id=%s: %s", pageindex_doc_id, exc)

    # ── 检索 ──

    def answer(self, ctx: RequestContext, question: str) -> AnswerResult:
        doc_ids = [d for d in ctx.doc_ids if d]
        if not doc_ids:
            # 绝不放宽到"整个账号/整个索引目录"：正确答案是"没有可检索范围"。
            raise PageIndexError("该知识库还没有已索引的文档")
        client = self._client(ctx)
        try:
            raw = client.chat(question, doc_id=doc_ids, citations=True)
        except Exception as exc:  # noqa: BLE001
            raise PageIndexError(f"检索失败: {exc}") from exc
        result = _normalize(raw)
        guard_citations(result.citations, doc_ids)
        return result

    # ── 健康 ──

    def health(self, models: Any = None) -> dict[str, Any]:
        try:
            import pageindex  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "disabled", "detail": f"pageindex 未安装: {exc}"}
        models = self._models if models is None else models
        # 自托管的可用性以**解析后的模型**为准（env → 网关 /models）：只看 env 的
        # `selfhosted_ready()` 正是"管理员在 /models 配了模型、healthz 却说未配置"的根源。
        selfhosted_ready = (
            models.ready() if models is not None else self._cfg.selfhosted_ready()
        )
        if not selfhosted_ready and not self._cfg.cloud_ready():
            if models is not None:
                from app.models import unavailable_detail

                detail = unavailable_detail(models)
            else:
                detail = "未配置自托管模型，也未配置 Cloud 问答模型"
            return {"ok": False, "status": "disabled", "detail": detail}
        payload: dict[str, Any] = {"ok": True, "status": "ready", "engine": sdk_version()}
        if selfhosted_ready:
            # 来源与模型名**不是秘密**，但要能一眼看出"模型是从哪来的"——
            # 排障第一个问题永远是"它到底读到了哪份配置"。
            payload["model_source"] = str(getattr(models, "source", "env") or "env")
            payload["chat_model"] = str(getattr(models, "chat_model", "") or "")
        return payload


def _normalize(raw: Any) -> AnswerResult:
    """``chat(citations=True)`` 的返回归一化（形态随模式/版本而异）。"""
    if isinstance(raw, str):
        return AnswerResult(answer=raw)
    if isinstance(raw, dict):
        answer = str(raw.get("answer") or raw.get("content") or raw.get("text") or "")
        citations: list[dict[str, Any]] = []
        for item in raw.get("citations") or []:
            if not isinstance(item, dict):
                continue
            citations.append(
                {
                    "doc_name": str(item.get("doc_name") or item.get("name") or ""),
                    "page": _as_int(item.get("page") or item.get("page_num")),
                    "block_id": str(item.get("block_id") or ""),
                    "snippet": str(item.get("snippet") or item.get("text") or "")[:500],
                }
            )
        return AnswerResult(answer=answer, citations=citations)
    return AnswerResult(answer=str(raw))


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
