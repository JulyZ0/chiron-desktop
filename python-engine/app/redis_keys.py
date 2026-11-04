"""Redis 键统一前缀 helper —— 与 Go 网关 internal/db/redis_key.go 的 RedisKey() 语义一致。

多套环境(dev/prod/隔离租户)共用同一 Redis 时，以 REDIS_KEY_PREFIX（如 "dev:" / "prod:"）
隔离键空间；默认空 = 存量行为（兼容单环境部署）。
所有 Redis key / channel / stream 名必须经 rkey() 组装，禁止散落裸键字面量。
"""
from __future__ import annotations

from app.config import settings


def rkey(name: str) -> str:
    """返回带统一前缀的完整键名：{REDIS_KEY_PREFIX}{name}。"""
    return f"{settings.redis_key_prefix}{name}"
