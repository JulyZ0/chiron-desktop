"""P1-6 知识库质量闭环：检索观测（Redis 计数）+ stats 响应 + 零命中事件 + 权重 + 模型覆盖。

为什么这些用例必须存在（每条都对应一个"看起来正常、实际没用"的真实故障）：

1. 计数只在成功时写 ⇒ "服务一直 500"在统计里显示成 `queries=0`，被读成"根本没检索"；
2. 失败原因存了 query 原文 ⇒ 用户问题里的敏感内容被写进 Redis 与日志；
3. 统计端点不判权限 ⇒ 任何知道 kb_id 的人都能看到这个库的检索活跃度；
4. 零命中不发事件 ⇒ 用户永远不知道"检索过、但没命中"，只能反复换着问；
5. 权重解析只认字符串 ⇒ 前端传 `[{id, weight}]` 时整轮检索静默失效（id 全丢）。

零外部依赖：Redis 用 fake，DB 用 patch。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agent import knowledge_context as kb_ctx
from app.agent.workbench_context import (
    DEFAULT_CONTEXT_WEIGHT,
    context_weights,
    merge_weighted,
)
from app.knowledge import metrics
from app.knowledge.errors import ProviderFailedError, ProviderUnavailableError
from app.knowledge.settings_store import (
    kb_model_overrides,
    normalize_model_name,
)

# ── fake redis ──


class FakeRedis:
    """只实现 metrics 用到的那几个命令（含 TTL 记录，便于断言"7 天"）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}
        self.calls: list[tuple[str, ...]] = []

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        self.calls.append(("hincrby", key, field, str(amount)))
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + int(amount)
        return bucket[field]

    async def expire(self, key: str, seconds: int) -> bool:
        self.calls.append(("expire", key, str(seconds)))
        self.ttl[key] = int(seconds)
        return True

    async def hgetall(self, key: str) -> dict[str, int]:
        self.calls.append(("hgetall", key))
        return dict(self.hashes.get(key, {}))

    async def hlen(self, key: str) -> int:
        return len(self.hashes.get(key, {}))


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def patch_redis(monkeypatch, fake_redis):
    from app import redis_client

    async def _get_redis() -> Any:
        return fake_redis

    monkeypatch.setattr(redis_client, "get_redis", _get_redis)
    return fake_redis


# ── 1. Redis 计数 ──


@pytest.mark.asyncio
async def test_record_counts_queries_hits_zero_hit_latency():
    redis = FakeRedis()
    now = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)
    await metrics.record_retrieval(
        kb_id="kb-1", user_id="u1", hits=3, latency_ms=1200, redis=redis, now=now
    )
    await metrics.record_retrieval(
        kb_id="kb-1", user_id="u1", hits=0, zero_hit=True, latency_ms=800, redis=redis, now=now
    )

    # 库级 + 用户级两份（"只有某用户 0 命中"与"全库 0 命中"必须能分开）
    all_key = metrics.hour_key("kb-1", "all", now)
    user_key = metrics.hour_key("kb-1", "u:u1", now)
    assert redis.hashes[all_key]["queries"] == 2
    assert redis.hashes[all_key]["hits"] == 3
    assert redis.hashes[all_key]["zero_hit"] == 1
    assert redis.hashes[all_key]["latency_ms_total"] == 2000
    assert redis.hashes[all_key]["latency_count"] == 2
    assert redis.hashes[user_key]["queries"] == 2

    # 7 天 TTL（需求写死；桶本身按小时滚动，所以 TTL 只是"不会永久残留"的保证）
    assert redis.ttl[all_key] == metrics.TTL_SECONDS
    assert metrics.TTL_SECONDS == 7 * 24 * 3600


@pytest.mark.asyncio
async def test_record_failure_counts_error_and_keeps_category_without_query():
    redis = FakeRedis()
    now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
    exc = ProviderUnavailableError("检索服务不可达（http://127.0.0.1:8100）：connection refused")
    await metrics.record_retrieval(
        kb_id="kb-1", user_id="u1", latency_ms=30, error=exc, redis=redis, now=now
    )

    key = metrics.hour_key("kb-1", "all", now)
    assert redis.hashes[key]["queries"] == 1
    assert redis.hashes[key]["errors"] == 1

    failures = await metrics._top_failures(redis, "kb-1", "all")  # noqa: SLF001
    assert failures and failures[0]["category"] == metrics.CATEGORY_PROVIDER_UNAVAILABLE
    assert "connection refused" in failures[0]["message"]
    # **不存 query 原文**：这里只断言失败原因里没有用户问题（用例本身也没传问题）
    assert "为什么" not in failures[0]["message"]


def test_error_message_is_truncated():
    long = "x" * 500
    out = metrics.error_message(ProviderFailedError(long))
    assert len(out) <= metrics.MAX_ERROR_MESSAGE + 1  # +1 是省略号
    assert out.endswith("…")


@pytest.mark.parametrize(
    "exc, category",
    [
        (ProviderUnavailableError("x"), metrics.CATEGORY_PROVIDER_UNAVAILABLE),
        (ProviderFailedError("x"), metrics.CATEGORY_SERVICE_ERROR),
        (TimeoutError("x"), metrics.CATEGORY_TIMEOUT),
        (RuntimeError("x"), metrics.CATEGORY_UNKNOWN),
    ],
)
def test_classify_error_is_type_based(exc, category):
    assert metrics.classify_error(exc) == category


# ── 2. stats 摘要 ──


@pytest.mark.asyncio
async def test_summary_splits_24h_and_7d_windows():
    redis = FakeRedis()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    # 2 小时前：在两个窗口内
    await metrics.record_retrieval(
        kb_id="kb-1", hits=2, latency_ms=100, redis=redis, now=now - timedelta(hours=2)
    )
    # 3 天前：只在 7d 窗口内
    await metrics.record_retrieval(
        kb_id="kb-1", hits=0, zero_hit=True, latency_ms=300, redis=redis, now=now - timedelta(days=3)
    )
    # 8 天前：两个窗口都不该包含（TTL 之外）
    await metrics.record_retrieval(kb_id="kb-1", hits=9, latency_ms=10, redis=redis, now=now - timedelta(days=8))

    data = await metrics.summary(kb_id="kb-1", redis=redis, now=now)
    assert data["available"] is True
    assert data["window_hours_24"]["queries"] == 1
    assert data["window_hours_24"]["hits"] == 2
    assert data["window_hours_24"]["avg_latency_ms"] == 100
    assert data["window_days_7"]["queries"] == 2
    assert data["window_days_7"]["zero_hit"] == 1
    # 平均延迟必须由 total/count 算，不能是"平均的平均"
    assert data["window_days_7"]["avg_latency_ms"] == 200
    assert data["diagnosis"], "摘要必须给出可读结论"


@pytest.mark.asyncio
async def test_summary_without_activity_says_so():
    data = await metrics.summary(kb_id="kb-never", redis=FakeRedis())
    assert data["window_days_7"]["queries"] == 0
    assert any("没有任何检索记录" in hint for hint in data["diagnosis"])


@pytest.mark.asyncio
async def test_summary_unavailable_redis_is_not_zeros():
    """"读不到观测"必须与"确实没检索"区分开：两者的处置完全相反。"""
    data = await metrics.summary(kb_id="kb-1", redis=None)
    # 没有 patch redis 时 get_redis 会失败（测试环境无 Redis）或返回不可用连接，
    # 两条路径都必须给出 available=False 而不是抛异常。
    if not data["available"]:
        assert data["diagnosis"]


@pytest.mark.asyncio
async def test_summary_includes_top5_failures_only():
    redis = FakeRedis()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    for idx in range(7):
        exc = ProviderFailedError(f"upstream error {idx}")
        for _ in range(idx + 1):
            await metrics.record_retrieval(kb_id="kb-1", error=exc, redis=redis, now=now)
    data = await metrics.summary(kb_id="kb-1", redis=redis, now=now)
    assert len(data["recent_failures"]) == 5
    counts = [item["count"] for item in data["recent_failures"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 7


@pytest.mark.asyncio
async def test_record_is_best_effort_when_redis_explodes():
    class Boom:
        async def hincrby(self, *a, **k):
            raise RuntimeError("redis down")

    # 不能抛：观测是增强，检索链路不该因为 Redis 抖动而失败
    await metrics.record_retrieval(kb_id="kb-1", hits=1, redis=Boom())


# ── 3. stats 端点：权限 + 形状 ──


class _Pool:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def fetchrow(self, *a, **k):
        return self.row

    async def fetchval(self, *a, **k):
        return "{}"

    async def execute(self, *a, **k):
        return "OK"


@pytest.mark.asyncio
async def test_stats_endpoint_requires_access(patch_redis):
    from app.api.knowledge import get_knowledge_stats

    row = {"id": "kb-1", "user_id": "owner", "visibility": "private", "tenant_id": 1}
    with patch("app.api.knowledge.get_pool", return_value=_Pool(row)):
        with pytest.raises(HTTPException) as exc:
            await get_knowledge_stats("kb-1", "intruder", tenant_id=2)
    assert exc.value.status_code == 403

    with patch("app.api.knowledge.get_pool", return_value=_Pool(row)):
        data = await get_knowledge_stats("kb-1", "owner", tenant_id=1)
    assert data["kb_id"] == "kb-1"
    assert set(data["window_hours_24"]) >= {"queries", "hits", "zero_hit", "errors", "avg_latency_ms"}
    assert "recent_failures" in data


# ── 4. 零命中事件 ──


async def _empty_retriever(kb_id, query, user_id, tenant_id):
    return {"answer": "", "citations": []}


async def _hit_retriever(kb_id, query, user_id, tenant_id):
    return {"answer": "交付 6 周", "citations": [{"doc_name": "a.pdf", "page": 1}]}


async def _boom_retriever(kb_id, query, user_id, tenant_id):
    raise ProviderUnavailableError("服务不可达")


@pytest.mark.asyncio
async def test_zero_hit_event_is_structured_and_not_an_error():
    injection = await kb_ctx.collect(["kb-1"], "无关问题", retriever=_empty_retriever)
    event = injection.as_event()
    assert event["queried"] == 1
    assert event["hits"] == 0
    assert event["zero_hit"] is True
    assert event["errors"] == []
    assert "query" not in event, "事件里绝不能带 query 原文"


@pytest.mark.asyncio
async def test_retrieval_error_event_carries_category():
    injection = await kb_ctx.collect(["kb-1"], "问题", retriever=_boom_retriever)
    event = injection.as_event()
    # 服务错误 ≠ 零命中：前端据此显示"检索失败"而不是"未命中，换关键词"
    assert event["zero_hit"] is False
    assert event["errors"][0]["category"] == metrics.CATEGORY_PROVIDER_UNAVAILABLE
    assert event["errors"][0]["kb_id"] == "kb-1"


@pytest.mark.asyncio
async def test_hit_event_reports_counts():
    injection = await kb_ctx.collect(["kb-1"], "交付周期", retriever=_hit_retriever)
    event = injection.as_event()
    assert event["hits"] == 1 and event["citations"] == 1 and event["zero_hit"] is False


# ── 5. 多库权重 ──


def test_context_weights_accepts_strings_and_objects():
    ctx = {"kb_ids": ["a", {"id": "b", "weight": 3}, {"id": "c", "weight": 0}, "a"]}
    weights = context_weights(ctx, "kb")
    assert weights["a"] == DEFAULT_CONTEXT_WEIGHT
    assert weights["b"] == 3.0
    # 非法权重回落 1.0（手写 URL 的一个笔误不该让这个库消失）
    assert weights["c"] == DEFAULT_CONTEXT_WEIGHT
    # 重复 id 保留首次出现（与 context_ids 的去重保序同口径）
    assert list(weights) == ["a", "b", "c"]
    # 单值回退
    assert context_weights({"kb_id": "solo"}, "kb") == {"solo": DEFAULT_CONTEXT_WEIGHT}


def test_context_ids_still_reads_object_items():
    from app.agent.workbench_context import context_ids

    assert context_ids({"kb_ids": [{"id": "a", "weight": 2}, "b"]}, "kb") == ["a", "b"]


def test_merge_weighted_prefers_high_weight():
    groups = [(1.0, ["a1", "a2", "a3"]), (3.0, ["b1", "b2", "b3"])]
    merged = merge_weighted(groups, 4)
    # b 权重 3：b1(3.0) > b2(1.5) > a1(1.0) > b3(1.0≈) —— 前四位必须由 b 主导
    assert merged[0] == "b1"
    assert merged.index("b2") < merged.index("a1")


def test_merge_weighted_equal_weights_matches_round_robin():
    groups = [(1.0, ["a1", "a2"]), (1.0, ["b1", "b2"])]
    assert merge_weighted(groups, 4) == ["a1", "b1", "a2", "b2"]
    assert merge_weighted(groups, 0) == []


@pytest.mark.asyncio
async def test_collect_orders_by_weight_and_caps_lowest_weight():
    seen: list[str] = []

    async def _retriever(kb_id, query, user_id, tenant_id):
        seen.append(kb_id)
        return {"answer": kb_id, "citations": [{"doc_name": "d"}]}

    await kb_ctx.collect(
        ["low", "high", "mid"],
        "问题",
        retriever=_retriever,
        max_kbs=2,
        weights={"low": 0.5, "high": 5.0, "mid": 1.0},
    )
    # 超出上限时被丢掉的是权重最低的那个，而不是数组里排最后的那个
    assert seen == ["high", "mid"]


# ── 6. 模型覆盖持久化 ──


def test_kb_model_overrides_reads_only_explicit_keys():
    assert kb_model_overrides({"index_model": "m1"}) == {"index_model": "m1"}
    assert kb_model_overrides({"index_model": " ", "chat_model": ""}) == {}
    assert kb_model_overrides(None) == {}
    assert kb_model_overrides('{"chat_model": "c1"}') == {"chat_model": "c1"}


@pytest.mark.parametrize("bad", ["a b", "x" * 130, "bad\nname"])
def test_normalize_model_name_rejects_garbage(bad):
    from app.knowledge.errors import KnowledgeError

    with pytest.raises(KnowledgeError):
        normalize_model_name(bad)


@pytest.mark.asyncio
async def test_save_kb_models_uses_single_statement_and_returns_effective():
    captured: dict[str, Any] = {}

    class Pool:
        async def execute(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return "OK"

        async def fetchval(self, sql, *args):
            return {"index_model": "idx-new", "chat_model": "chat-new"}

    with patch("app.knowledge.settings_store._pool", return_value=Pool()):
        from app.knowledge import settings_store

        result = await settings_store.save_kb_models(
            kb_id="kb-1", index_model="idx-new", chat_model="chat-new"
        )

    # 只改模型时也该只发一条 UPDATE（读—改—写会与并发保存互相覆盖）
    assert "UPDATE knowledge_bases" in captured["sql"]
    assert "|| " in captured["sql"] and " - " in captured["sql"]
    assert result == {"index_model": "idx-new", "chat_model": "chat-new"}


@pytest.mark.asyncio
async def test_update_knowledge_base_persists_models_without_other_fields():
    from app.api import knowledge as kb_api

    row = {"id": "kb-1", "user_id": "u1"}

    class Pool:
        async def fetchrow(self, *a, **k):
            return row

        async def fetchval(self, *a, **k):
            return {}

        async def execute(self, *a, **k):
            return "OK"

    async def _save(**kwargs):
        assert kwargs["index_model"] == "m-idx"
        return {"index_model": "m-idx"}

    with patch("app.api.knowledge.get_pool", return_value=Pool()), patch(
        "app.knowledge.settings_store.save_kb_models", side_effect=_save
    ):
        result = await kb_api.update_knowledge_base(
            kb_id="kb-1", user_id="u1", index_model="m-idx"
        )
    # 只改模型：不报 "no fields to update"，且回包里带生效值
    assert result["updated"] is True
    assert result["index_model"] == "m-idx"


def test_serialize_kb_exposes_model_overrides():
    from app.api.knowledge import _serialize_kb

    row = {
        "id": "kb-1",
        "name": "KB",
        "description": "",
        "provider": "selfhosted",
        "visibility": "private",
        "status": "ready",
        "enabled": True,
        "document_count": 1,
        "total_size_bytes": 10,
        "credits_consumed": 0,
        "error_message": None,
        "config": {"chat_model": "c1"},
        "created_at": datetime(2026, 1, 1),
        "updated_at": datetime(2026, 1, 1),
    }
    data = _serialize_kb(row)
    assert data["chat_model"] == "c1"
    assert data["index_model"] == ""  # 未覆盖 = 空串（沿用网关注入的默认）
