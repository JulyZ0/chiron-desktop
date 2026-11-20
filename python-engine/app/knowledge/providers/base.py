"""知识库 provider 抽象 —— 让"检索引擎"成为可替换的一层。

为什么要有这层抽象：知识库的检索能力从"内置的向量 + FTS"换成了外部的 PageIndex，
而 PageIndex 自身有两种形态（自托管 local / Cloud）。如果 API 层直接 ``import pageindex``，
那么"新增一种检索形态"就会变成改 controller；provider 接口把这件事收敛成"实现这个协议
并注册"。

**选 provider 是用户维度的，且二选一**（``user_knowledge_settings.provider``）：自托管用
部署方配置的 LLM，Cloud 用用户自己的 PageIndex API key。两者能力不同
（见 P0 结论：local 只吃 text-based PDF、无 folder/metadata/OCR；Cloud 全有）。

**所有 provider 调用都必须是异步边界**：PageIndex 的 local 索引是同步的长任务（分钟级），
直接在事件循环里跑会阻塞整个引擎进程 —— 实现里一律用 ``asyncio.to_thread`` 下放。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 取值刻意用宽字符串而不是 PG enum/常量类：新增检索引擎只需在这里加一个名字，
# 不产生 ``ALTER TYPE`` 迁移（见 0023 迁移的取舍说明）。
PROVIDER_SELFHOSTED = "selfhosted"
PROVIDER_CLOUD = "cloud"
KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_SELFHOSTED, PROVIDER_CLOUD)

# provider 级状态（落在 knowledge_bases.status / user_knowledge_settings.status）
STATUS_DISABLED = "disabled"  # 未配置：可用但没启用
STATUS_READY = "ready"        # 就绪
STATUS_ERROR = "error"        # 配置了但异常（凭据错、端点不可达等）


def is_known_provider(name: str) -> bool:
    return name in KNOWN_PROVIDERS


#: 上传时文件名 = `<前缀>-<随机>_<原名>`（见各 provider 的 _submit_sync），引用据此映射回
#: Chiron 的文档 —— 这是**可验证**的归属线索。
def doc_prefix(doc_id: str) -> str:
    """文档在索引器侧的文件名前缀（去连字符的 32 位十六进制）。

    uuid 自带 4 个连字符：直接拿它当前缀，"取第一个连字符之前"就会截在错误的位置
    （`3f2a…-…` 只取到第一段），于是**合法引用被误判成越界**。所以前缀必须不含分隔符。
    """
    return (doc_id or "").replace("-", "")


def citation_doc_id(doc_name: str) -> str:
    """从引用文件名解析出文档前缀（`<prefix>-<随机>_<原名>` → prefix）。"""
    head, sep, _ = (doc_name or "").strip().partition("-")
    return head if sep else ""


def citation_in_scope(doc_name: str, doc_ids: Iterable[str]) -> bool:
    """引用是否落在本次检索范围内。

    解析不出前缀时返回 ``True``（**不判越界**）：PageIndex 可能改写文档名，误杀正常引用
    比漏判更糟 —— 漏判由"绝不做账号级检索"与"不把账号级浏览工具暴露给 agent"两条兜住，
    而误杀会直接让正确回答消失。
    """
    parsed = citation_doc_id(doc_name)
    if not parsed:
        return True
    return parsed in {doc_prefix(d) for d in doc_ids if d}


def citation_doc_key(doc_name: str, doc_ids: Iterable[str]) -> str:
    """从引用文件名反解出 Chiron 侧的文档 id（``doc_name`` → ``knowledge_documents.id``）。

    反向映射与 ``citation_in_scope`` 用同一套前缀规则，因此"能通过越界校验"的引用
    一定能解出 id（除非 PageIndex 改写了文件名，那时由调用方留空降级）。
    """
    parsed = citation_doc_id(doc_name)
    if not parsed:
        return ""
    for doc_id in doc_ids:
        if doc_id and doc_prefix(doc_id) == parsed:
            return str(doc_id)
    return ""


@dataclass(frozen=True)
class ProviderContext:
    """一次 provider 调用的上下文。

    ``index_ref`` 是 provider 侧的物理位置：自托管=索引目录（相对根），Cloud=folder_id。
    ``tenant_id`` 参与路径/过滤，跨租户串读一律视为缺陷。
    """

    kb_id: str
    tenant_id: int
    user_id: str
    index_ref: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    # 该库下已索引的 provider 侧文档 id（回答问题时作为检索范围的边界：
    # PageIndex 的 chat 只接受显式文档/目录，没有"整个库"的隐式范围）
    doc_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class SubmitResult:
    doc_id: str
    page_count: int = 0


@dataclass(frozen=True)
class Citation:
    """引用：PageIndex 的两级粒度（local 页级 / Cloud 块级）。

    ``doc_id`` / ``kb_id`` 是**可定位**所需的两个坐标（P1-1）：用户点引用要能回到
    "哪个库的哪篇文档的第几页"。PageIndex 只返回文件名（``doc_name``），而文件名里带
    了我们上传时写入的文档 id 前缀（``<doc_id去连字符>-<随机>_<原名>``，见 ``doc_prefix``），
    所以文档 id 可以**从 doc_name 反解**，不需要额外查询；解不出时留空（前端降级为
    只显示文档名，不影响正文）。
    """

    doc_name: str = ""
    page: int | None = None
    block_id: str = ""
    snippet: str = ""
    doc_id: str = ""
    kb_id: str = ""


@dataclass(frozen=True)
class AnswerResult:
    """问答结果。

    注意与旧实现的**语义差异**：旧 ``query`` 返回 top-k 相似片段，PageIndex 返回的是
    "答案 + 可追溯引用"。前端与工具都按新形态消费（引用页码/文本片段）。
    """

    answer: str
    citations: list[Citation] = field(default_factory=list)


@runtime_checkable
class KnowledgeProvider(Protocol):
    """检索引擎适配器。实现必须是线程安全且幂等的（同一文档重复提交不产生重复条目）。"""

    name: str

    def health(self) -> HealthResult:
        """探测自身可用性。**不得抛异常**：不可用即返回 ok=False。"""
        ...

    def supports(self, filename: str) -> bool:
        """该 provider 是否接受这种文件。

        必须在**上传时**就问：PageIndex 的 local 模式只吃 text-based PDF，等索引阶段才拒绝
        会让用户以为"传成功了"。
        """
        ...

    async def prepare_index(self, ctx: ProviderContext) -> str:
        """准备索引位置，返回 provider 侧的引用（由 service 回写 ``index_ref``）。

        * 自托管：索引目录（相对 root）—— 这也是它的隔离边界；
        * Cloud：该库的 folder id（其上层是**以 user_id 命名的用户 folder**）。

        幂等：重复调用返回同一个位置。
        """
        ...

    async def submit_document(
        self, ctx: ProviderContext, *, doc_id: str, filename: str, data: bytes, file_type: str
    ) -> SubmitResult:
        """索引一份文档，返回 provider 侧的 doc_id（回写 knowledge_documents.pageindex_doc_id）。"""
        ...

    async def delete_document(self, ctx: ProviderContext, *, pageindex_doc_id: str) -> None:
        """删除 provider 侧文档。文档不存在时视为成功（幂等）。"""
        ...

    async def answer(self, ctx: ProviderContext, *, question: str) -> AnswerResult:
        """回答问题并给出引用。"""
        ...

    def agent_tools(self, ctx: ProviderContext) -> list[Any]:
        """暴露给 agent 的检索工具（openai-agents ``FunctionTool`` 或等价物）。"""
        ...

    def agent_instructions(self, ctx: ProviderContext) -> str:
        """与 ``agent_tools`` 配套的编排提示词。"""
        ...
