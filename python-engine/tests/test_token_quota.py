"""R7 TPM：分钟桶的判定与累计口径。"""
from __future__ import annotations

import pytest

from app.gateway.token_quota import TokenQuota


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def incrby(self, key: str, amount: int) -> int:
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttl[key] = seconds
        return True


@pytest.mark.asyncio
async def test_tenant_and_user_buckets() -> None:
    redis = _FakeRedis()
    quota = TokenQuota(redis, tenant_tpm=1000, user_tpm=400)

    assert await quota.exceeded(1, "u1") == ""

    await quota.add(1, "u1", 300)
    assert await quota.exceeded(1, "u1") == ""
    assert redis.store
    # 两个维度各自累计，而不是二选一。
    assert len(redis.store) == 2
    assert all(ttl == 120 for ttl in redis.ttl.values())

    await quota.add(1, "u1", 150)
    assert await quota.exceeded(1, "u1") == "user"
    # 换一个用户仍只看自己的桶。
    assert await quota.exceeded(1, "u2") == ""

    await quota.add(1, "u1", 600)
    assert await quota.exceeded(1, "u2") == "tenant"


@pytest.mark.asyncio
async def test_disabled_dimensions_skip() -> None:
    redis = _FakeRedis()
    quota = TokenQuota(redis, tenant_tpm=0, user_tpm=0)
    assert quota.enabled is False
    await quota.add(1, "u1", 10_000)
    assert redis.store == {}
    assert await quota.exceeded(1, "u1") == ""

    only_user = TokenQuota(redis, tenant_tpm=0, user_tpm=100)
    await only_user.add(1, "u1", 120)
    assert len(redis.store) == 1, "租户维度关闭时不应写租户键"
    assert await only_user.exceeded(1, "u1") == "user"
