"""S3：池副本空闲自停的统计口径（健康探针不算活动）。"""

from __future__ import annotations

import asyncio

import app.main  # noqa: F401 — 初始化 app 包
from app import idle_shutdown
from app.idle_shutdown import IdleTrackingMiddleware


async def test_middleware_counts_business_requests_only():
    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["path"])

    mw = IdleTrackingMiddleware(app)
    idle_shutdown._active_requests = 0

    await mw({"type": "http", "path": "/healthz"}, None, None)
    assert idle_shutdown._active_requests == 0, "健康探针不应算业务活动"

    await mw({"type": "http", "path": "/v1/chat/submit"}, None, None)
    assert seen == ["/healthz", "/v1/chat/submit"]
    assert idle_shutdown._active_requests == 0, "请求结束后在途数必须归零"


def test_touch_updates_activity(monkeypatch):
    before = idle_shutdown._last_activity
    monkeypatch.setattr(idle_shutdown.time, "monotonic", lambda: before + 10)
    idle_shutdown.touch()
    assert idle_shutdown._last_activity == before + 10


def test_watch_disabled_when_threshold_zero():
    # 阈值 0 = 关闭：任务应立刻返回，而不是空转
    asyncio.run(asyncio.wait_for(idle_shutdown.idle_shutdown_watch(0), timeout=1))
