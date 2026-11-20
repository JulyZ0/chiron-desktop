"""按模型的公开价格同步（M28）。

**要解决什么**：本仓的单价是**全局**的（`BillingConfig.LLMCostPerToken/Output`，1 / 2
credits per 1000 tokens），对所有模型一视同仁 —— 而不同模型的价格差几个数量级。成本核算、
`cost` 轴的单价、按成本选模型都需要**按模型**的价格，这张表（`model_prices`，迁移 `0029`）存它。

**数据从哪来**：公开的价格数据集（无需认证）。多源**交叉验证**：同一个模型至少两个源给出
**相对差 ≤ 5%** 的价格才写入；不一致 ⇒ 记分歧、**不写入**、打点。宁可没有数据，也不要一个
来源不明或自相矛盾的数字。

⚠ **跨源模型名对齐是启发式**（见 :func:`normalize_model`）：两个源对同一个模型的叫法可能差得远，
那种情况这里对齐不上 ⇒ 结果是"该模型只有一个源" ⇒ 判它未验证、不写入。这是**有意的保守**：
拿两个不同模型的价格互相"验证"，比没有数据更糟。

⚠ **它不参与计费**：计费权威是 `BillingConfig`（管理员可改、`billing_records` 按它记账）。
这张表是参考数据 —— 混进计费会让"谁说了算"不可追溯。

**为什么是定时拉取而不是运行时查询**：价格是慢变量（日更），而运行时查询会把外部依赖放进
请求路径（出站面 + 延迟 + 失败降级）。定时拉取把不确定性收在一个后台任务里，且默认**关**
（`MODEL_PRICE_SYNC`）—— 新增机制默认什么都不做。

**出站必须走 `fetch_url_safe`**（`app/tools/ssrf.py`）：本仓纪律是 9 个既有出站调用点全走它。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.observability.metrics import MODEL_PRICE_SYNC

logger = logging.getLogger(__name__)

#: 价格**统一口径**：USD / 1000 tokens。各源单位不同（portkey = 美分/token；llmrates =
#: 每百万 token + 原生货币）⇒ 每个源的解析函数自己负责换算到这一个口径，下游不再做单位转换。
UNIT = "USD/1k"

#: 交叉验证的默认容差（相对差）。5% 是"同一个价格在不同源上的正常浮动"的量级：
#: 再严会把四舍五入差异判成分歧，再松就拦不住真的不同价（例如某源的旧价）。
DEFAULT_TOLERANCE = 0.05

DEFAULT_INTERVAL_SECONDS = 1800  # 30min（用户拍板）

#: 源的目录：(解析器名, URL, 归属 provider)。
#:
#: provider 决定写进 `model_prices.provider` —— portkey 按厂商分文件，同一个解析器
#: 要跑多份；llmrates / pricetoken 是跨厂商聚合，provider 由载荷自带，这里留空。
#:
#: **只收实测可达的源**（2026-11-06 逐个 curl 验过）：portkey 的 openai/anthropic/
#: google/deepseek 均 200，mistral 403（需认证，不入目录）；llmrates 与 pricetoken 200。
#: `product.aiprice.store/api/models` 实测 404 —— 接口已不存在，**不接入**：
#: 挂一个死源只会让每轮同步多一条 fetch_failed 告警，覆盖率一点也不会涨。
SOURCES: tuple[tuple[str, str, str], ...] = (
    ("portkey", "https://configs.portkey.ai/pricing/openai.json", "openai"),
    ("portkey", "https://configs.portkey.ai/pricing/anthropic.json", "anthropic"),
    ("portkey", "https://configs.portkey.ai/pricing/google.json", "google"),
    ("portkey", "https://configs.portkey.ai/pricing/deepseek.json", "deepseek"),
    ("llmrates", "https://www.llmrates.ai/api/dataset", ""),
    ("pricetoken", "https://pricetoken.ai/api/v1/text", ""),
)


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的一条价格（已归一化到 :data:`UNIT`）。"""

    model: str
    provider: str
    input_per_1k: float
    output_per_1k: float


#: 失败原因的最大长度（与 `price_source_status.error` 的展示口径一致）。
ERROR_MAX_CHARS = 200

#: 形如 ``key=value`` 的敏感参数（错误文本里可能夹带查询串或 header 片段）。
_SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[=:]\s*\S+")
#: 完整 URL：只保留 scheme://host[:port]，路径与查询串可能含一次性凭证。
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def sanitize_reason(raw: object, *, limit: int = ERROR_MAX_CHARS) -> str:
    """把异常文本压成**可展示**的一行：去掉凭证、去掉 URL 路径/查询串、截断。

    为什么不能直接把 ``str(exc)`` 写进库：httpx 的异常会带完整 URL（可能含查询串里的
    key），SSRF 防护层还可能把整段响应体拼进来。健康面板是**所有人可见**的运营视图，
    把凭证复制到那里等于扩大泄露面。截到 200 字符是因为这里只需要"哪个环节坏了"，
    完整堆栈在各进程日志里。
    """

    def _host_only(match: re.Match[str]) -> str:
        try:
            parts = urlsplit(match.group(0))
        except ValueError:
            return "<url>"
        host = parts.hostname or ""
        if not host:
            return "<url>"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"

    text = _URL_RE.sub(_host_only, str(raw))
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)
    # 折叠空白：多行异常会让表格行高乱掉，也便于截断判断
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


@dataclass(frozen=True)
class SourceStatus:
    """一个源在一轮同步里的结果（写入 `price_source_status` 的形状）。"""

    source_key: str
    parser: str
    provider: str
    url_host: str
    status: str  # ok | empty | failed
    rows: int
    duration_ms: int
    error: str


def build_source_status(
    *,
    parser: str,
    provider: str,
    url: str,
    rows: int,
    duration_ms: int,
    error: str = "",
) -> SourceStatus:
    """把一次拉取的观测量折成健康行。

    ``status`` 三态：``ok``（解析出 ≥1 条）/ ``empty``（成功但 0 条）/ ``failed``。
    把 ``empty`` 单列出来是因为它有独立含义 —— 源活着，但对我们的模型名一个都对不上；
    与"源不可达"混为一谈会把排查引向网络。
    """
    if error:
        status = "failed"
    elif rows > 0:
        status = "ok"
    else:
        status = "empty"
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return SourceStatus(
        source_key=f"{parser}:{provider}" if provider else parser,
        parser=parser,
        provider=provider,
        url_host=host,
        status=status,
        rows=max(int(rows), 0),
        duration_ms=max(int(duration_ms), 0),
        error=sanitize_reason(error) if error else "",
    )



def normalize_model(name: str) -> str:
    """跨源对齐模型名的**启发式**：小写、去分隔符、去日期后缀。

    ⚠ 这不是权威映射。两个源对同一个模型的叫法可能差得远（`glm-4v` vs `glm-4-vision`），
    这里**对齐不上** —— 那种情况下游会判"只有一个源"、不写入。**宁可缺数据，也不要拿两个
    不同模型的价格互相"验证"**（那会造出一个来源不明的数字，比没有更糟）。
    """
    lowered = name.strip().lower()
    for sep in ("_", " ", ".", "/"):
        lowered = lowered.replace(sep, "-")
    parts = [p for p in lowered.split("-") if p]
    # 去掉纯数字的版本段（`-20241022` / `-0613` 这类日期戳）
    while parts and parts[-1].isdigit():
        parts.pop()
    return "-".join(parts)


def _to_float(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def parse_portkey(
    payload: dict[str, Any], *, provider: str = "openai"
) -> dict[str, ModelPrice]:
    """Portkey 的定价 JSON：``{model: {pricing_config: {pay_as_you_go: {...}}}}``。

    单位是**美分 / token**（文档自陈）⇒ 换算：``usd_per_1k = price_cents * 10``
    （1 美分 = 0.01 美元；per token → per 1k 乘 1000 ⇒ 0.01 × 1000 = 10）。

    同一个解析器要跑 4 份（openai/anthropic/google/deepseek 各自一个文件），
    所以 ``provider`` 由调用方按文件归属传入，而不是在这里写死 ``openai``。

    文件里的 ``"default"`` 键是全零占位，会被 ``req <= 0 and resp <= 0`` 跳过。
    """
    out: dict[str, ModelPrice] = {}
    for model, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        payg = (entry.get("pricing_config") or {}).get("pay_as_you_go") or {}
        req = _to_float((payg.get("request_token") or {}).get("price"))
        resp = _to_float((payg.get("response_token") or {}).get("price"))
        if req <= 0 and resp <= 0:
            continue
        out[normalize_model(str(model))] = ModelPrice(
            model=str(model), provider=provider, input_per_1k=req * 10, output_per_1k=resp * 10
        )
    return out


def parse_llmrates(
    payload: dict[str, Any], *, provider: str = ""
) -> dict[str, ModelPrice]:
    """LLMRates 的数据集：``{"models": [{name, provider:{slug}, prices:[{...}]}]}``。

    单位是**每百万 token + 原生货币**（`priceUnit`）⇒ 换算：``usd_per_1k = per_million / 1000``。
    ⚠ **非 USD 的条目一律跳过**：本仓不做汇率换算（汇率是另一个会漂移的外部量，且价格表不参与
    计费，没有必须换算的压力）。跳过时打点，让"某个源大部分是 CNY"这件事可见。

    ``provider`` 参数只为与其它解析器签名一致（``_fetch_source`` 统一传入）；
    本源的 provider 由载荷自带，参数仅在载荷缺失时兜底。
    """
    out: dict[str, ModelPrice] = {}
    skipped_currency = 0
    for entry in payload.get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        entry_provider = str((entry.get("provider") or {}).get("slug") or "") or provider
        for price in entry.get("prices") or []:
            if not isinstance(price, dict):
                continue
            if str(price.get("priceUnit") or "USD").upper() != "USD":
                skipped_currency += 1
                continue
            inp = _to_float(price.get("inputPricePerMillion"))
            outp = _to_float(price.get("outputPricePerMillion"))
            if inp <= 0 and outp <= 0:
                continue
            out[normalize_model(name)] = ModelPrice(
                model=name,
                provider=entry_provider,
                input_per_1k=inp / 1000,
                output_per_1k=outp / 1000,
            )
            break  # 一个模型取第一条 USD 价格（后面的通常是其它 tier/模态）
    if skipped_currency:
        MODEL_PRICE_SYNC.labels(outcome="skipped_currency", source="llmrates").inc(skipped_currency)
    return out


def parse_pricetoken(
    payload: dict[str, Any], *, provider: str = ""
) -> dict[str, ModelPrice]:
    """PriceToken 的文本模型价格：``{"data": [{modelId, provider, inputPerMTok, ...}]}``。

    单位是**每百万 token 的美元**（字段名即 ``PerMTok``，响应无币种字段 ⇒ 按美元读）
    ⇒ 换算：``usd_per_1k = per_million / 1000``。

    **只收 active**：``deprecated`` / ``preview`` 的条目照样有价格，但把退役模型写进
    价格表会让人以为还能用，且它们是同一模型名在跨源比对里的噪声来源。
    """
    out: dict[str, ModelPrice] = {}
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "active").lower() != "active":
            continue
        name = str(entry.get("modelId") or entry.get("displayName") or "")
        if not name:
            continue
        inp = _to_float(entry.get("inputPerMTok"))
        outp = _to_float(entry.get("outputPerMTok"))
        if inp <= 0 and outp <= 0:
            continue
        out[normalize_model(name)] = ModelPrice(
            model=name,
            provider=str(entry.get("provider") or ""),
            input_per_1k=inp / 1000,
            output_per_1k=outp / 1000,
        )
    return out


#: 源名 → 解析函数。加新源只需在这里加一行 + 一个解析函数（各自负责换算到 UNIT）。
PARSERS: dict[str, Any] = {
    "portkey": parse_portkey,
    "llmrates": parse_llmrates,
    "pricetoken": parse_pricetoken,
}
def _close_enough(a: ModelPrice, b: ModelPrice, tolerance: float) -> bool:
    """两条价格在容差内一致（**两轴都要**：只比输入会让"输出价差一倍"溜过去）。"""
    for x, y in ((a.input_per_1k, b.input_per_1k), (a.output_per_1k, b.output_per_1k)):
        scale = max(abs(x), abs(y), 1e-9)
        if abs(x - y) / scale > tolerance:
            return False
    return True


def cross_validate(
    per_source: dict[str, dict[str, ModelPrice]], *, tolerance: float = DEFAULT_TOLERANCE
) -> tuple[list[ModelPrice], list[str]]:
    """多源比对：至少**两个源**给出容差内一致的价格才收。

    返回 ``(可写入的价格, 分歧描述)``。分歧**不写入**（见模块头：宁可缺数据）—— 但也**不静默**：
    描述会进日志与指标，否则"价格表一直是空的"没人知道为什么。
    """
    by_model: dict[str, list[tuple[str, ModelPrice]]] = {}
    for source, prices in per_source.items():
        for norm, price in prices.items():
            by_model.setdefault(norm, []).append((source, price))

    agreed: list[ModelPrice] = []
    disagreements: list[str] = []
    for norm, entries in sorted(by_model.items()):
        if len(entries) < 2:
            # 只有一个源 —— **不写入**：单源无法交叉验证，写进去就等于给一个未验证的数字背书。
            MODEL_PRICE_SYNC.labels(outcome="single_source", source=entries[0][0]).inc()
            continue
        first_source, first = entries[0]
        if all(_close_enough(first, other, tolerance) for _, other in entries[1:]):
            agreed.append(first)
            MODEL_PRICE_SYNC.labels(outcome="agreed", source=first_source).inc()
            continue
        detail = ", ".join(f"{src}={p.input_per_1k:.4f}/{p.output_per_1k:.4f}" for src, p in entries)
        disagreements.append(f"{norm}: {detail}")
        MODEL_PRICE_SYNC.labels(outcome="disagreement", source=first_source).inc()
    return agreed, disagreements


async def _fetch_source(
    name: str,
    url: str,
    *,
    provider: str = "",
    timeout: float = 20.0,
    record_error: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """拉一个源并解析。任何失败都返回 ``None``（调用方按"这个源这轮没数据"处理）。

    ``provider`` 由源目录给出（portkey 按厂商分文件）；解析器自己决定要不要用它。

    ``record_error`` 是可选的失败原因出口：同步循环用它把"这个源为什么没数据"
    写进 `price_source_status`。**不用返回值传**是为了不破坏既有调用/测试契约
    （返回 ``None`` 就代表"这轮没数据"，其余细节走回调）。
    """
    import httpx

    from app.tools.ssrf import fetch_url_safe

    def _report(reason: str) -> None:
        if record_error is not None:
            record_error(reason)

    parser = PARSERS.get(name)
    if parser is None:
        # 源目录里有、解析器表里没有 = 代码自身不一致，必须可见而不是静默跳过。
        _report(f"no parser for source {name}")
        return None
    try:
        # follow_redirects=False 是 `fetch_url_safe` 的前置条件（它自己逐跳校验重定向）。
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            resp = await fetch_url_safe(client, url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — 外部数据源的任何问题都不该打断同步循环
        MODEL_PRICE_SYNC.labels(outcome="fetch_failed", source=name).inc()
        logger.warning("model price fetch failed (%s): %s", name, sanitize_reason(exc, limit=160))
        _report(f"{type(exc).__name__}: {exc}")
        return None
    if not isinstance(payload, dict):
        MODEL_PRICE_SYNC.labels(outcome="bad_payload", source=name).inc()
        _report(f"unexpected payload type: {type(payload).__name__}")
        return None
    try:
        parsed = parser(payload, provider=provider)
    except Exception as exc:  # noqa: BLE001 — 源改了结构 = 解析失败，按"这轮没数据"处理
        MODEL_PRICE_SYNC.labels(outcome="parse_failed", source=name).inc()
        logger.warning("model price parse failed (%s): %s", name, sanitize_reason(exc, limit=160))
        _report(f"parse: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(parsed, dict):
        # 解析函数契约是"返回 {规范化名: ModelPrice}" —— 返回别的东西按坏载荷处理，
        # 而不是把 Any 透传给下游（下游会拿它当 dict 用）。
        MODEL_PRICE_SYNC.labels(outcome="bad_payload", source=name).inc()
        _report(f"parser returned {type(parsed).__name__}, expected dict")
        return None
    return parsed


async def sync_once() -> tuple[int, list[str]]:
    """拉全部源 → 交叉验证 → 写入。返回 ``(写入行数, 分歧描述)``。

    ⚠ **至少要有两个源成功**才做交叉验证：只有一个源时 :func:`cross_validate` 会把所有模型
    都判成 `single_source` ⇒ 一行都不写（这是**正确**的降级：宁可这轮不更新，也不写入未验证值）。

    每个源的结果（成功/失败/条数/耗时/脱敏原因）会写入 `price_source_status`，
    供管理端 `GET /v1/admin/billing/price-sources` 展示。**写诊断失败不影响同步**
    —— 它是旁路观测，不该让"价格没同步上"变成"还多报一个数据库错误"。
    """
    # 键必须是**每个 URL 唯一**的：`portkey` 在源目录里出现 4 次（按厂商分文件），
    # 若直接用解析器名做键，四次拉取会互相覆盖，最后只剩最后一个厂商的文件生效 ——
    # 表象是"价格表里只有 deepseek 的模型"，而代码看不出任何丢弃动作。
    fetched: dict[str, dict[str, ModelPrice]] = {}
    base_sources: set[str] = set()
    statuses: list[SourceStatus] = []
    for name, url, provider in SOURCES:
        reason: list[str] = []
        started = time.monotonic()
        prices = await _fetch_source(
            name, url, provider=provider, record_error=reason.append
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        statuses.append(
            build_source_status(
                parser=name,
                provider=provider,
                url=url,
                rows=len(prices) if prices else 0,
                duration_ms=duration_ms,
                error=reason[0] if reason else "",
            )
        )
        if prices:
            fetched[f"{name}:{provider}" if provider else name] = prices
            base_sources.add(name)

    await _write_source_status(statuses)

    if not fetched:
        MODEL_PRICE_SYNC.labels(outcome="no_source", source="-").inc()
        return 0, []

    agreed, disagreements = cross_validate(fetched)
    if disagreements:
        # 不静默：分歧要么是源过期、要么是我们对齐错了 —— 两种都需要人看一眼。
        logger.warning("model price disagreements (%d): %s", len(disagreements), "; ".join(disagreements[:5]))

    written = await _upsert(agreed, sources=sorted(base_sources))
    return written, disagreements


async def _write_source_status(statuses: list[SourceStatus]) -> None:
    """把本轮每个源的结果写进 `price_source_status`（upsert，一源一行）。

    纯旁路：池未就绪 / 表未迁移 / 写失败都只记 debug，**绝不抛出**。原因很实际 ——
    诊断数据缺失是"少一块面板信息"，而一次未捕获异常会让整轮同步以失败收场，
    那才是用户真正在意的部分。
    """
    if not statuses:
        return
    try:
        from app.db import get_pool

        pool = get_pool()
    except Exception:  # noqa: BLE001 — 池未初始化（RuntimeError）也走这里
        return
    if pool is None:
        return
    keys = [s.source_key for s in statuses]
    try:
        async with pool.acquire() as conn:
            for s in statuses:
                await conn.execute(
                    """
                    INSERT INTO price_source_status
                        (source_key, parser, provider, url_host, status, rows,
                         duration_ms, error, checked_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT (source_key) DO UPDATE SET
                        parser      = EXCLUDED.parser,
                        provider    = EXCLUDED.provider,
                        url_host    = EXCLUDED.url_host,
                        status      = EXCLUDED.status,
                        rows        = EXCLUDED.rows,
                        duration_ms = EXCLUDED.duration_ms,
                        error       = EXCLUDED.error,
                        checked_at  = now()
                    """,
                    s.source_key,
                    s.parser,
                    s.provider,
                    s.url_host,
                    s.status,
                    s.rows,
                    s.duration_ms,
                    s.error,
                )
            # 源目录里删掉的源不该永远留在面板上显示"上次失败"
            await conn.execute(
                "DELETE FROM price_source_status WHERE source_key <> ALL($1::text[])", keys
            )
    except Exception as exc:  # noqa: BLE001 — 诊断写入失败不该影响同步结果
        logger.debug("price source status write skipped: %s", sanitize_reason(exc))



async def _upsert(prices: list[ModelPrice], *, sources: list[str]) -> int:
    """把验证过的价格写进 `model_prices`（表缺失 / 库不可用 ⇒ 记一次告警并放弃）。"""
    if not prices:
        return 0
    from app.db import get_pool  # 与其它 PG 访问同一条连接池入口

    # ⚠ `get_pool()` 是**同步**函数（返回已初始化的 asyncpg.Pool），不能 await。
    #
    # 此前写成 `await get_pool()`：await 一个 Pool 对象必然抛
    # `TypeError: object Pool can't be used in 'await' expression`，被下面的
    # `except Exception` 记成 `write_failed` 并返回 0 —— 于是**每一轮同步都写 0 行**，
    # 价格表永远是空的。而日志把人引向"请执行迁移 0029"，可表本身根本没问题。
    #
    # 池未初始化时它是 `raise RuntimeError`（不是返回 None），所以这里也要接住：
    # 引擎刚启动时同步循环可能比建池更早跑第一轮。
    try:
        pool = get_pool()
    except RuntimeError as exc:
        MODEL_PRICE_SYNC.labels(outcome="db_unavailable", source="-").inc()
        logger.warning("model price upsert skipped (db pool not ready): %s", str(exc)[:160])
        return 0
    if pool is None:
        MODEL_PRICE_SYNC.labels(outcome="db_unavailable", source="-").inc()
        return 0
    src = ",".join(sources)
    written = 0
    try:
        async with pool.acquire() as conn:
            for price in prices:
                await conn.execute(
                    """
                    INSERT INTO model_prices
                        (model, provider, input_per_1k, output_per_1k, sources, source_count,
                         verified_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                    ON CONFLICT (model, provider) DO UPDATE SET
                        input_per_1k = EXCLUDED.input_per_1k,
                        output_per_1k = EXCLUDED.output_per_1k,
                        sources = EXCLUDED.sources,
                        source_count = EXCLUDED.source_count,
                        verified_at = now(),
                        updated_at = now()
                    """,
                    price.model,
                    price.provider,
                    price.input_per_1k,
                    price.output_per_1k,
                    src,
                    len(sources),
                )
                written += 1
    except Exception as exc:  # noqa: BLE001 — 未迁移的库只该丢这张表，不该让任务崩
        MODEL_PRICE_SYNC.labels(outcome="write_failed", source="-").inc()
        logger.warning("model price upsert failed（请执行迁移 0029_model_prices）: %s", str(exc)[:160])
        return 0
    MODEL_PRICE_SYNC.labels(outcome="written", source="-").inc(written)
    return written


def sync_enabled() -> bool:
    """开关：默认**开**；``MODEL_PRICE_SYNC=off``（或 ``0``/``false``/``no``）才关。

    原设计默认关（"新增机制默认什么都不做"），但实际后果是价格表长期为空、
    「价格一览」页面永远是空表 —— 一个默认不产生任何数据的机制等于没接。

    默认开启的风险面很小：价格是慢变量（日更），且这张表**只做参考、不参与计费**
    （见模块头），即使拉到错误价格也不会影响账单。需要彻底禁用的部署显式设
    ``MODEL_PRICE_SYNC=off`` 即可。
    """
    raw = (os.getenv("MODEL_PRICE_SYNC") or "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def sync_interval_seconds() -> int:
    """同步间隔（秒），默认 1800（用户拍板 30min）。取不到或非法值回落默认。"""
    raw = (os.getenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return value if value >= 60 else DEFAULT_INTERVAL_SECONDS


async def start_price_sync() -> None:
    """后台循环：**启动先拉一轮**，之后每 ``interval`` 秒一轮。

    为什么先拉后睡：原实现是先 sleep 再 work，意味着新装/重启后要等整整 30 分钟
    价格表才第一次有数据 —— 用户在此期间看到的是空表，会认为功能坏了。价格表为空
    时"立刻拉一次"的收益远大于那点出站开销。

    与 `main.py` 既有的 lifespan 循环同形。**未启用时直接返回** —— 调用方可以
    无条件 await 它，不必自己判开关。
    """
    if not sync_enabled():
        logger.info("model price sync disabled (MODEL_PRICE_SYNC=off)")
        return
    interval = sync_interval_seconds()
    logger.info("model price sync started (immediate first run, then every %ss)", interval)
    while True:
        try:
            written, disagreements = await sync_once()
            logger.info("model price sync: wrote %d rows, %d disagreements", written, len(disagreements))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 一轮失败不该让循环死掉
            MODEL_PRICE_SYNC.labels(outcome="cycle_failed", source="-").inc()
            logger.warning("model price sync cycle failed: %s", str(exc)[:160])
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
