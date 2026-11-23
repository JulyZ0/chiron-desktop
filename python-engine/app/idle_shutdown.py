"""引擎池副本的空闲自停（S3 空闲回收）。

池里的额外引擎只在有请求时才有价值；没有请求还常驻会白占 ~260MB/进程。
这里在**引擎进程内**做空闲判定：额外副本连续 N 秒没有业务请求就优雅退出
（SIGTERM，退出码 75），启动器看到 75 不再重拉 —— 容量随负载自动缩回 1 个 primary。

只对 `ENGINE_REPLICA=true` 的实例启用；primary 永远不自停。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# 空闲副本的退出码：启动器据此区分"该缩容"与"崩溃需重拉"。
IDLE_EXIT_CODE = 75

# 健康/探活请求不算"业务活动"：启动器与网关每十几秒就会打一次，算进去会永不空闲。
_IGNORED_PREFIXES = ("/healthz", "/readyz", "/metrics", "/info", "/docs", "/openapi.json")

_last_activity = time.monotonic()
_active_requests = 0


def touch() -> None:
    global _last_activity
    _last_activity = time.monotonic()


class IdleTrackingMiddleware:
    """纯 ASGI 中间件：记录业务请求与在途数。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path.startswith(_IGNORED_PREFIXES):
            await self.app(scope, receive, send)
            return
        global _active_requests
        _active_requests += 1
        try:
            await self.app(scope, receive, send)
        finally:
            _active_requests -= 1
            touch()


async def idle_shutdown_watch(threshold_seconds: int, poll_seconds: int = 60) -> None:
    if threshold_seconds <= 0:
        return
    logger.info("idle shutdown enabled: %ss", threshold_seconds)
    while True:
        await asyncio.sleep(poll_seconds)
        idle = time.monotonic() - _last_activity
        if _active_requests == 0 and idle >= threshold_seconds:
            logger.info("engine replica idle for %.0fs; shutting down", idle)
            # 空闲 = 没有在途请求，直接退出即可；用专用退出码让启动器缩容而不是重拉。
            os._exit(IDLE_EXIT_CODE)
            return
