# Token 预算管理 — per-tenant, Redis 原子扣减
from __future__ import annotations

import json
import logging
import time

import redis.asyncio as aioredis

from app.redis_keys import rkey

logger = logging.getLogger(__name__)



def _field(data: dict, name: str, default: int = 0) -> int:
    """从 HGETALL 结果里取值，bytes 与 str 两种键都收。

    客户端是否开 `decode_responses` 决定键的类型；只认一种时另一种会**静默读成 0** ——
    表现为"设了限额却永远放行"或"用了一整月仍显示 0"。
    """
    value = data.get(name)
    if value is None:
        value = data.get(name.encode())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


class TokenBudget:
    """
    存储: Redis Hash
    key: "budget:{tenant_id}:{yyyy-mm}"
    fields: "used" | "limit"

    - limit=0 表示不限制
    - DECRBY 原子扣减
    - 超 80% 发告警事件
    - 超 100% 拒绝请求
    """

    BUDGET_WARN_RATIO = 0.8

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    @staticmethod
    def _key(tenant_id: int) -> str:
        month = time.strftime("%Y-%m")
        return rkey(f"budget:{tenant_id}:{month}")

    async def check(self, tenant_id: int, estimated_tokens: int) -> bool:
        """预检查：是否还有足够额度（**含已预占**）。

        注意：它仍是"读后判断"的粗检。真正防超发的是 `budget_reserve.reserve()`
        （判断与占位在同一段 Lua 里完成）。这里把 `reserved` 也计入，是为了让
        粗检不会把别人已占的额度当成空闲额度。
        """
        if not tenant_id:
            return True

        key = self._key(tenant_id)
        data = await self._redis.hgetall(key)

        if not data:
            return True  # 无预算配置 = 不限制

        limit = _field(data, "limit")
        if limit <= 0:
            return True  # limit=0 不限制

        used = _field(data, "used")
        reserved = _field(data, "reserved")
        return (used + reserved + estimated_tokens) <= limit

    async def deduct(self, tenant_id: int, actual_tokens: int) -> None:
        """实际扣减（调用后不可撤销）"""
        if not tenant_id or actual_tokens <= 0:
            return

        try:
            key = self._key(tenant_id)
            new_used = await self._redis.hincrby(key, "used", actual_tokens)

            # 检查是否需要告警
            limit_raw = await self._redis.hget(key, "limit")
            if limit_raw:
                limit = int(limit_raw)
                if limit > 0 and new_used / limit >= self.BUDGET_WARN_RATIO:
                    await self._redis.publish(
                        rkey("budget:alerts"),
                        json.dumps(
                            {"tenant_id": tenant_id, "used": new_used, "limit": limit}
                        ),
                    )
                    logger.warning(
                        "Budget alert: tenant=%s used=%d limit=%d (%.0f%%)",
                        tenant_id,
                        new_used,
                        limit,
                        new_used / limit * 100,
                    )
        except Exception as e:
            logger.exception("Budget deduct failed for tenant=%s: %s", tenant_id, e)

    async def get_usage(self, tenant_id: int) -> tuple[int, int]:
        """返回 (used, limit)"""
        key = self._key(tenant_id)
        data = await self._redis.hgetall(key)
        if not data:
            return (0, 0)
        return (_field(data, "used"), _field(data, "limit"))

    async def set_limit(self, tenant_id: int, limit: int) -> None:
        """设置月度 token 预算上限"""
        key = self._key(tenant_id)
        await self._redis.hset(key, "limit", str(limit))

    async def reset(self, tenant_id: int) -> None:
        """重置当月用量"""
        key = self._key(tenant_id)
        await self._redis.hset(key, "used", "0")
