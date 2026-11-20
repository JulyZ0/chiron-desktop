"""知识库**检索观测**：把"为什么没检索到"变成可以被回答的数字。

## 为什么必须记录

用户看到的是"问了一句、回答里没有库内容"。这句话背后至少有五种完全不同的现实：

1. 用户根本没选库（前端没发 `kb_ids`）；
2. 选了库、但一次检索都没发生（库停用 / 索引未 ready / provider 不可用）；
3. 检索发生了、服务报错（网络、key、上游 5xx）；
4. 检索成功、但确实 0 命中（库里没有相关内容）；
5. 命中了、但模型没引用。

这五种在界面上长得一样，处置方式却完全不同（去选库 / 去启用 / 去修配置 / 换关键词 /
看回答）。没有计数时，客服与用户只能反复试；有了计数与失败原因 Top5，第一句就能问对：
"`queries=0` 说明根本没检索，先看库是不是停用了"。

## 存储口径（Redis，7 天 TTL，**不动迁移**）

* **小时内桶**：``engine:kb:stats:{kb_id}:{dim}:{YYYYMMDDHH}``（hash，TTL 7 天）。
  一次检索只写一个桶：近 24h 读 24 个桶、近 7d 读 168 个桶 —— 一个窗口一种口径，
  不需要"日桶 + 小时桶"两套写入，也就不会出现两套口径漂移。
* **维度** ``dim``：``all``（库级，任何人检索都累加）与 ``u:{user_id}``（按用户）。
  写两份而不是只写一份：只写库级时"某个用户一直 0 命中"和"全库都 0 命中"分不开；
  只写用户级时库维度的总览要靠 SCAN 拼，代价更大。
* **字段**：``queries`` / ``hits`` / ``zero_hit`` / ``errors`` /
  ``latency_ms_total`` / ``latency_count``。平均延迟由后两项相除得到 —— 存平均值本身
  就没法合并（多小时桶求和时平均的平均是错的）。
* **失败原因**：``engine:kb:stats:{kb_id}:{dim}:errors``（hash，字段是
  ``{分类}|{截断后的消息}``，值是次数，TTL 7 天）。**绝不记录 query 原文**：
  用户的问题可能就是敏感内容，而排障只需要"哪一类、哪一句报错"。

## 硬约束

Redis 故障**绝不影响检索**：所有写入都是 best-effort，异常只记 debug 级日志。
观测是增强，不是前提。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: 计数器与失败原因桶的存活时间（需求：7 天）。
TTL_SECONDS = 7 * 24 * 3600

#: 消息截断长度。够看清"哪一类错"（连接拒绝 / 401 / 超时），又不至于把文档片段存进来。
MAX_ERROR_MESSAGE = 120

#: 失败原因 hash 里最多保留的条目数（防止一个坏上游刷出上千个不同消息把内存吃光）。
MAX_ERROR_FIELDS = 40

#: 归因分类 —— 前端就是按这几个值给出下一步动作的，取值不能随手加。
CATEGORY_DISABLED = "disabled"
CATEGORY_NOT_FOUND = "not_found"
CATEGORY_PROVIDER_UNAVAILABLE = "provider_unavailable"
CATEGORY_SERVICE_ERROR = "service_error"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_UNKNOWN = "unknown"

#: 分类 → 给用户的一句话（诊断用；不含原始异常文本）。
CATEGORY_HINTS: dict[str, str] = {
    CATEGORY_DISABLED: "知识库处于停用状态，检索被直接拦下；去详情页重新启用",
    CATEGORY_NOT_FOUND: "知识库已不存在（可能已被删除）；刷新列表",
    CATEGORY_PROVIDER_UNAVAILABLE: "检索服务不可用（未启动 / 未配 key / 网络不通）；检查知识库设置",
    CATEGORY_SERVICE_ERROR: "检索服务返回了错误；看失败原因 Top5 里的消息",
    CATEGORY_TIMEOUT: "检索超时（推理式检索耗时较长）；可缩小问题范围或重建索引",
    CATEGORY_UNKNOWN: "未归类的检索失败；看失败原因 Top5",
}

_HOUR_FORMAT = "%Y%m%d%H"


# ── 分类 ──


def classify_error(exc: BaseException) -> str:
    """异常 → 稳定分类（**只看类型，不解析文本**）。

    分类必须稳定：它是计数器的 key，也是前端的动作分支。按异常文本做子串匹配，
    上游改一句文案就会让"服务错误"变成"未知"，历史计数也再也合不到一起。
    """
    # 延迟导入：`app.knowledge.errors` 依赖 config，本模块在导入链上尽量靠前。
    from app.knowledge import errors as kb_errors

    # `KnowledgeBaseDisabledError` 定义在 service 里（那里是唯一的抛点），而 service
    # 又依赖本模块 —— 直接 isinstance 会形成循环导入。按类名判定：这个类是内部实现，
    # 改名时这里会一起改（不是外部契约，不存在"第三方同名类"的误判风险）。
    if type(exc).__name__ == "KnowledgeBaseDisabledError":
        return CATEGORY_DISABLED
    if isinstance(exc, kb_errors.ProviderUnavailableError):
        return CATEGORY_PROVIDER_UNAVAILABLE
    if isinstance(exc, kb_errors.ProviderFailedError):
        return CATEGORY_SERVICE_ERROR
    if isinstance(exc, kb_errors.KnowledgeError):
        # service 找不到库时抛的就是裸 KnowledgeError（``knowledge base not found: id``）。
        # 只认自家这句文案：其它 Kit KnowledgeError（不支持的文档类型等）属于 unknown，
        # 把它们算成"库不存在"会让诊断把人带去删库/重建，方向完全错。
        if str(exc).startswith("knowledge base not found"):
            return CATEGORY_NOT_FOUND
        return CATEGORY_UNKNOWN
    if isinstance(exc, TimeoutError):  # 含 asyncio.TimeoutError（3.11+ 是内建 TimeoutError）
        return CATEGORY_TIMEOUT
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return CATEGORY_TIMEOUT
    return CATEGORY_UNKNOWN


def error_message(exc: BaseException | str) -> str:
    """截断后的失败消息（**不存 query 原文**，只存异常自述）。"""
    text = str(exc) if isinstance(exc, BaseException) else str(exc or "")
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_MESSAGE:
        text = text[:MAX_ERROR_MESSAGE] + "…"
    return text or (type(exc).__name__ if isinstance(exc, BaseException) else "")


def describe_failure(exc: BaseException) -> dict[str, str]:
    """失败 → ``{"category", "message"}``（API、事件、Redis 三处共用同一形状）。"""
    return {"category": classify_error(exc), "message": error_message(exc)}


# ── 键 ──


def _prefix() -> str:
    from app.redis_keys import rkey

    return rkey("engine:kb:stats:")


def hour_key(kb_id: str, dim: str, moment: datetime) -> str:
    return f"{_prefix()}{kb_id}:{dim}:{moment.astimezone(UTC).strftime(_HOUR_FORMAT)}"


def errors_key(kb_id: str, dim: str) -> str:
    return f"{_prefix()}{kb_id}:{dim}:errors"


def dim_of(user_id: str) -> str:
    """维度名；空 user_id 归到 ``all``（匿名/系统调用只计库级）。"""
    user = str(user_id or "").strip()
    return f"u:{user}" if user else "all"


def dimensions(user_id: str = "") -> list[str]:
    """一次检索要写的维度集合：库级 + （有身份时）用户级。去重后返回。"""
    dims = ["all"]
    user_dim = dim_of(user_id)
    if user_dim != "all":
        dims.append(user_dim)
    return dims


# ── 写入 ──


async def _redis_or_none() -> Any:
    """取 Redis；不可用时返回 None（**不抛**：观测不能拖垮检索）。"""
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.debug("kb metrics: redis unavailable: %s", exc)
        return None


async def record_retrieval(
    *,
    kb_id: str,
    user_id: str = "",
    hits: int = 0,
    zero_hit: bool = False,
    latency_ms: int = 0,
    error: BaseException | None = None,
    redis: Any = None,
    now: datetime | None = None,
) -> None:
    """记一次检索（成功或失败）。**永不抛**。

    ``redis`` 可注入（测试用假的），缺省时懒取单例。
    """
    if not str(kb_id or ""):
        return
    moment = now or datetime.now(UTC)
    client = redis if redis is not None else await _redis_or_none()
    if client is None:
        return
    try:
        fields: dict[str, int] = {"queries": 1, "latency_ms_total": max(0, int(latency_ms)), "latency_count": 1}
        if hits:
            fields["hits"] = int(hits)
        if zero_hit:
            fields["zero_hit"] = 1
        if error is not None:
            fields["errors"] = 1
        key_suffix = moment.astimezone(UTC).strftime(_HOUR_FORMAT)
        for dim in dimensions(user_id):
            key = f"{_prefix()}{kb_id}:{dim}:{key_suffix}"
            for name, amount in fields.items():
                await client.hincrby(key, name, amount)
            await client.expire(key, TTL_SECONDS)
            if error is not None:
                await _record_error(client, kb_id, dim, error)
    except Exception as exc:  # noqa: BLE001 — 观测失败绝不影响检索
        logger.debug("kb metrics: record failed kb=%s: %s", kb_id, exc)


async def _record_error(client: Any, kb_id: str, dim: str, error: BaseException) -> None:
    detail = describe_failure(error)
    field = f"{detail['category']}|{detail['message']}"
    key = errors_key(kb_id, dim)
    try:
        length = await client.hlen(key)
    except Exception:  # noqa: BLE001 — 不支持 hlen 的客户端（或假实现）就跳过上限保护
        length = 0
    if length and int(length) >= MAX_ERROR_FIELDS:
        # 已经满了就不再新增字段（已有的继续累加）：宁可少一种原因，也不让 hash 无限长
        return
    await client.hincrby(key, field, 1)
    await client.expire(key, TTL_SECONDS)


# ── 读取 ──


def _hour_buckets(hours: int, *, now: datetime) -> list[datetime]:
    """最近 ``hours`` 个小时桶（含当前小时）。"""
    base = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [base - timedelta(hours=i) for i in range(max(0, int(hours)) - 1, -1, -1)]


async def _sum_dim(redis: Any, kb_id: str, dim: str, buckets: list[datetime]) -> dict[str, Any]:
    totals = {
        "queries": 0,
        "hits": 0,
        "zero_hit": 0,
        "errors": 0,
        "latency_ms_total": 0,
        "latency_count": 0,
    }
    for bucket in buckets:
        try:
            raw = await redis.hgetall(hour_key(kb_id, dim, bucket))
        except Exception:  # noqa: BLE001 — 单个桶读失败按 0 处理（不要整个统计 500）
            continue
        if not isinstance(raw, dict):
            continue
        for name in totals:
            key = name.encode() if _bytes_keys(raw) else name
            try:
                totals[name] += int(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
    queries = totals.pop("queries")
    lat_total = totals.pop("latency_ms_total")
    lat_count = totals.pop("latency_count")
    avg = int(round(lat_total / lat_count)) if lat_count else 0
    hits = totals["hits"]
    return {
        "queries": queries,
        "hits": hits,
        "zero_hit": totals["zero_hit"],
        "errors": totals["errors"],
        "avg_latency_ms": avg,
        # 零命中率：分母是**成功**的检索数（queries - errors）。失败本身另有一列，
        # 混进零命中率会让"服务挂了"看起来像"库里没有内容"。
        "zero_hit_rate": round(totals["zero_hit"] / max(0, queries - totals["errors"]), 4)
        if queries - totals["errors"] > 0
        else 0.0,
    }


def _bytes_keys(raw: dict[Any, Any]) -> bool:
    return bool(raw) and all(isinstance(k, bytes) for k in raw)


async def _top_failures(redis: Any, kb_id: str, dim: str, limit: int = 5) -> list[dict[str, Any]]:
    """失败原因 Top5：按次数降序。字段里只会有 ``分类|截断消息``（写入时就切好了）。"""
    try:
        raw = await redis.hgetall(errors_key(kb_id, dim))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, dict):
        return []
    items: list[tuple[int, str]] = []
    for field, count in raw.items():
        name = field.decode() if isinstance(field, bytes) else str(field)
        try:
            number = int(count)
        except (TypeError, ValueError):
            continue
        items.append((number, name))
    items.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for count, name in items[: max(0, limit)]:
        category, _, message = name.partition("|")
        out.append({"category": category, "message": message, "count": count})
    return out


def _diagnosis(*, last_24h: dict[str, Any], last_7d: dict[str, Any], failures: list[dict[str, Any]]) -> list[str]:
    """把数字翻成**可执行**的一句话（前端直接渲染，不再自己解释字段）。"""
    hints: list[str] = []
    if int(last_7d.get("queries") or 0) == 0:
        hints.append("近 7 天没有任何检索记录：先确认对话里是否真的选中了这个库（@ 知识库）")
        return hints
    errors = int(last_7d.get("errors") or 0)
    if errors and failures:
        hints.append(f"近 7 天 {errors} 次检索报错，最常见原因：{failures[0]['category']}")
    if int(last_24h.get("queries") or 0) == 0:
        hints.append("近 24 小时没有检索：这个库可能刚被停用，或最近没人在对话里引用它")
    elif int(last_24h.get("zero_hit") or 0) and not int(last_24h.get("hits") or 0):
        hints.append("近 24 小时检索全部 0 命中：换个关键词，或确认文档已索引完成（状态 ready）")
    if not hints:
        hints.append("近 24 小时检索正常（有命中且无报错）")
    return hints


async def summary(
    *,
    kb_id: str,
    user_id: str = "",
    redis: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """近 24h / 7d 摘要 + 失败原因 Top5（**只读，永不抛**：读不到就当全 0）。

    ``user_id`` 非空时额外返回 ``mine``（该用户在同一窗口内的数字）—— 多租户/团队库里
    "我检索不到"和"整个库检索不到"必须能分开。
    """
    moment = now or datetime.now(UTC)
    client = redis if redis is not None else await _redis_or_none()
    empty = {"queries": 0, "hits": 0, "zero_hit": 0, "errors": 0, "avg_latency_ms": 0, "zero_hit_rate": 0.0}
    if client is None:
        return {
            "kb_id": kb_id,
            "window_hours_24": empty,
            "window_days_7": empty,
            "recent_failures": [],
            "diagnosis": ["Redis 不可用，检索观测暂不可读"],
            "available": False,
        }
    h24 = _hour_buckets(24, now=moment)
    d7 = _hour_buckets(7 * 24, now=moment)
    try:
        last_24h = await _sum_dim(client, kb_id, "all", h24)
        last_7d = await _sum_dim(client, kb_id, "all", d7)
        failures = await _top_failures(client, kb_id, "all")
    except Exception as exc:  # noqa: BLE001 — 统计故障 ≠ 500
        logger.debug("kb metrics: summary failed kb=%s: %s", kb_id, exc)
        return {
            "kb_id": kb_id,
            "window_hours_24": empty,
            "window_days_7": empty,
            "recent_failures": [],
            "diagnosis": [f"检索观测读取失败：{error_message(exc)}"],
            "available": False,
        }
    payload: dict[str, Any] = {
        "kb_id": kb_id,
        "window_hours_24": last_24h,
        "window_days_7": last_7d,
        "recent_failures": failures,
        "diagnosis": _diagnosis(last_24h=last_24h, last_7d=last_7d, failures=failures),
        "available": True,
        "ttl_days": TTL_SECONDS // 86400,
    }
    if str(user_id or "").strip():
        mine_dim = dim_of(user_id)
        payload["mine"] = {
            "window_hours_24": await _sum_dim(client, kb_id, mine_dim, h24),
            "window_days_7": await _sum_dim(client, kb_id, mine_dim, d7),
        }
    return payload


class RetrievalTimer:
    """检索耗时计时器（with 语句；``.elapsed_ms`` 在退出时可用）。"""

    def __init__(self) -> None:
        self._start = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._start) * 1000))

    def __enter__(self) -> "RetrievalTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


__all__ = [
    "CATEGORY_DISABLED",
    "CATEGORY_HINTS",
    "CATEGORY_NOT_FOUND",
    "CATEGORY_PROVIDER_UNAVAILABLE",
    "CATEGORY_SERVICE_ERROR",
    "CATEGORY_TIMEOUT",
    "CATEGORY_UNKNOWN",
    "MAX_ERROR_MESSAGE",
    "TTL_SECONDS",
    "classify_error",
    "describe_failure",
    "dim_of",
    "error_message",
    "record_retrieval",
    "summary",
]
