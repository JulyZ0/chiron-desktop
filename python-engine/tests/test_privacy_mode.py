"""隐私模式（X-Privacy-Mode）测试 — 企业合规：no_retention 时数据绝不落盘.

验证意图：
1. 缺省头时行为与正常留存逐字节一致（Redis 写入被调用）
2. no_retention 时 session 不落 Redis、trace 不写 Redis Stream（内存态仍可用）
3. 未知头值按正常模式处理（fail-open 到正常留存）

通过真实 FastAPI app + PrivacyModeMiddleware 端到端驱动，
用 mock 断言持久化写入函数（redis.setex / redis.xadd）的调用情况。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.privacy_middleware import PrivacyModeMiddleware, is_no_retention, privacy_mode_var
from app.session_store import SessionStore
from app.trace.writer import TraceWriter


def _mock_redis() -> MagicMock:
    """构造可 await 的 Redis mock（覆盖 session_store / trace writer 用到的方法）"""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.expire = AsyncMock()
    redis.xadd = AsyncMock()
    redis.delete = AsyncMock()
    pipeline = MagicMock()
    pipeline.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=pipeline)
    return redis


def _build_app(store: SessionStore, writer: TraceWriter) -> FastAPI:
    """挂载隐私模式中间件的最小 app，端点内执行 session/trace 写入"""
    app = FastAPI()
    app.add_middleware(PrivacyModeMiddleware)

    @app.post("/run")
    async def run():
        await store.get_or_init("sess-1", [{"role": "user", "content": "hi"}])
        await store.append("sess-1", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
        await writer.write_span(trace_id="t1", span_name="llm_call", duration_ms=5, tenant_id="tenant-a")
        await writer.write_batch([{"trace_id": "t1", "span_name": "tool:echo", "duration_ms": 3}])
        return {"no_retention": is_no_retention()}

    return app


def _make_store_and_writer():
    redis = _mock_redis()
    store = SessionStore(redis_client=redis)
    writer = TraceWriter()
    writer._redis = redis  # noqa: SLF001 — 测试直接注入 mock 后端
    return redis, store, writer


async def _run_request(headers: dict | None) -> tuple[MagicMock, dict]:
    redis, store, writer = _make_store_and_writer()
    app = _build_app(store, writer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/run", headers=headers or {})
    assert resp.status_code == 200
    return redis, resp.json()


@pytest.mark.asyncio
async def test_no_header_normal_retention():
    """缺省 X-Privacy-Mode 头：session 落 Redis、trace 写 Stream（写入必须被调用）"""
    redis, body = await _run_request(None)

    assert body["no_retention"] is False
    # session 持久化：get_or_init MISS 写入 + append 写入
    assert redis.setex.await_count >= 2
    # trace 持久化：write_span 单条 xadd + write_batch 走 pipeline
    assert redis.xadd.await_count == 1
    assert redis.pipeline.call_count == 1
    assert redis.pipeline.return_value.xadd.call_count == 1


@pytest.mark.asyncio
async def test_no_retention_skips_all_persistence():
    """X-Privacy-Mode: no_retention —— 隐私模式下数据绝不落盘"""
    redis, body = await _run_request({"X-Privacy-Mode": "no_retention"})

    assert body["no_retention"] is True
    # session：Redis 读写一律跳过（不落地，也不读取历史留存数据）
    assert redis.setex.await_count == 0
    assert redis.get.await_count == 0
    # trace：单条与批量写入均跳过
    assert redis.xadd.await_count == 0
    assert redis.pipeline.call_count == 0


@pytest.mark.asyncio
async def test_no_retention_keeps_in_memory_session_working():
    """no_retention 仅不落盘 —— 内存态会话仍可正常工作（多轮读写一致）"""
    redis = _mock_redis()
    store = SessionStore(redis_client=redis)

    token = privacy_mode_var.set("no_retention")
    try:
        msgs = await store.get_or_init("sess-mem", [{"role": "user", "content": "q1"}])
        await store.append("sess-mem", msgs + [{"role": "assistant", "content": "a1"}])
        again = await store.get_or_init("sess-mem", [])
    finally:
        privacy_mode_var.reset(token)

    # 内存态正常续接：第二轮能读到第一轮累积的消息
    assert [m["content"] for m in again] == ["q1", "a1"]
    # 且全程未触碰 Redis
    assert redis.setex.await_count == 0
    assert redis.get.await_count == 0


@pytest.mark.asyncio
async def test_unknown_value_treated_as_normal():
    """未知 X-Privacy-Mode 值：fail-open 到正常留存（写入必须被调用）"""
    redis, body = await _run_request({"X-Privacy-Mode": "some_future_mode"})

    assert body["no_retention"] is False
    assert redis.setex.await_count >= 2
    assert redis.xadd.await_count == 1


@pytest.mark.asyncio
async def test_is_no_retention_defaults_false_outside_request():
    """无请求上下文（如后台任务）时缺省为正常模式，不得误跳过留存"""
    assert is_no_retention() is False
