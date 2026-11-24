"""H30: semantic cache keys carry the complete owner and request domain."""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway.cache import SemanticCache
from app.gateway.provider import ChatMessage, ChatResponse
from app.redis_keys import rkey


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.now = 1000.0

    async def get(self, key: str) -> str | None:
        expires_at = self.ttls.get(key)
        if expires_at is not None and self.now >= expires_at:
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            return None
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = self.now + ex
        return True

    async def delete(self, key: str) -> int:
        existed = self.store.pop(key, None) is not None
        self.ttls.pop(key, None)
        return int(existed)


def _scope(
    *,
    user_id: str = "user-a",
    policy_digest: str = "policy-v1",
    model_lineage: Any = None,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "private": False,
        "policy_digest": policy_digest,
        "model_lineage": model_lineage
        if model_lineage is not None
        else {"provider": "provider-a", "model_revision": "revision-1"},
    }


def _cache(
    redis: _FakeRedis | None = None,
    *,
    embed_fn: Any = None,
    **kwargs: Any,
) -> SemanticCache:
    if embed_fn is None:

        async def embed_fn(text: str) -> list[float]:
            return [0.5] * 8

    return SemanticCache(redis=redis or _FakeRedis(), embed_fn=embed_fn, **kwargs)


_MSGS = [ChatMessage(role="user", content="我的邮箱是 a@example.com，帮我看看")]


def test_exact_key_differs_by_tenant() -> None:
    k_a = SemanticCache._exact_key("tenant-a", "gpt-x", _MSGS, None, 0.7)
    k_b = SemanticCache._exact_key("tenant-b", "gpt-x", _MSGS, None, 0.7)
    assert k_a != k_b
    assert k_a == SemanticCache._exact_key("tenant-a", "gpt-x", _MSGS, None, 0.7)


@pytest.mark.asyncio
async def test_semantic_key_differs_by_tenant() -> None:
    cache = _cache()
    k_a = await cache._semantic_key("tenant-a", _MSGS, "gpt-x")
    k_b = await cache._semantic_key("tenant-b", _MSGS, "gpt-x")
    assert k_a and k_b and k_a != k_b
    assert k_a == await cache._semantic_key("tenant-a", _MSGS, "gpt-x")


def test_tenant_id_is_required_on_public_api() -> None:
    import inspect

    for fn in (SemanticCache.lookup, SemanticCache.store, SemanticCache.delete):
        params = inspect.signature(fn).parameters
        assert "tenant_id" in params
        assert params["tenant_id"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_private_sessions_are_disabled_by_default() -> None:
    embedded: list[str] = []

    async def embed(text: str) -> list[float]:
        embedded.append(text)
        return [0.5] * 8

    redis = _FakeRedis()
    cache = _cache(redis, embed_fn=embed)

    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="private"))
    assert await cache.lookup(1, "m", _MSGS, None, 0.0) is None
    assert redis.store == {}
    assert cache._l1 == {}
    assert embedded == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"user_id": ""},
        {"policy_digest": ""},
        {"model_lineage": None},
        {"private": True},
    ],
)
async def test_incomplete_shared_scope_fails_closed(override: dict[str, Any]) -> None:
    redis = _FakeRedis()
    cache = _cache(redis)
    scope = _scope()
    scope.update(override)
    if override.get("model_lineage", "missing") is None:
        scope["model_lineage"] = None

    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="secret"), **scope)
    assert await cache.lookup(1, "m", _MSGS, None, 0.0, **scope) is None
    assert redis.store == {}
    assert cache._l1 == {}


@pytest.mark.asyncio
async def test_same_tenant_different_users_do_not_share_cache() -> None:
    cache = _cache()
    scope_a = _scope(user_id="user-a")
    scope_b = _scope(user_id="user-b")

    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="user-a-private"), **scope_a)
    assert await cache.lookup(1, "m", _MSGS, None, 0.0, **scope_b) is None

    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="user-b-answer"), **scope_b)
    hit_a = await cache.lookup(1, "m", _MSGS, None, 0.0, **scope_a)
    hit_b = await cache.lookup(1, "m", _MSGS, None, 0.0, **scope_b)
    assert hit_a is not None and hit_a.content == "user-a-private"
    assert hit_b is not None and hit_b.content == "user-b-answer"


@pytest.mark.asyncio
async def test_earlier_private_context_changes_semantic_domain() -> None:
    embedded: list[str] = []

    async def embed(text: str) -> list[float]:
        embedded.append(text)
        return [0.5] * 8

    cache = _cache(embed_fn=embed)
    recent = [ChatMessage(role="user", content=f"最近上下文 {index}") for index in range(6)]
    messages_a = [ChatMessage(role="user", content="最早私有上下文：用户 A 的密钥")] + recent
    messages_b = [ChatMessage(role="user", content="最早私有上下文：用户 B 的密钥")] + recent
    scope = _scope()

    key_a = await cache._semantic_key(1, messages_a, "m", **scope)
    key_b = await cache._semantic_key(1, messages_b, "m", **scope)
    assert key_a and key_b and key_a != key_b

    await cache.store(1, "m", messages_a, None, 0.0, ChatResponse(content="answer-for-a"), **scope)
    assert await cache.lookup(1, "m", messages_b, None, 0.0, **scope) is None
    assert any("最早私有上下文" in text for text in embedded)


@pytest.mark.asyncio
async def test_tools_and_policy_are_part_of_semantic_domain() -> None:
    cache = _cache()
    tools = [{"type": "function", "function": {"name": "read_secret"}}]
    base = await cache._semantic_key(1, _MSGS, "m", **_scope())
    with_tools = await cache._semantic_key(1, _MSGS, "m", tools=tools, **_scope())
    other_policy = await cache._semantic_key(1, _MSGS, "m", **_scope(policy_digest="policy-v2"))

    assert base and with_tools and other_policy
    assert len({base, with_tools, other_policy}) == 3


@pytest.mark.asyncio
async def test_model_lineage_change_prevents_cache_hit() -> None:
    cache = _cache()
    lineage_a = _scope(model_lineage={"provider": "p", "base_url": "https://a", "revision": "1"})
    lineage_b = _scope(model_lineage={"provider": "p", "base_url": "https://a", "revision": "2"})

    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="old-lineage"), **lineage_a)
    assert await cache.lookup(1, "m", _MSGS, None, 0.0, **lineage_b) is None


@pytest.mark.asyncio
async def test_l3_has_an_independent_enforced_ttl() -> None:
    redis = _FakeRedis()
    cache = _cache(redis, l2_ttl=100, l3_ttl=5)
    scope = _scope()
    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="short-lived"), **scope)

    assert cache.l2_ttl == 100
    assert cache.l3_ttl == 5
    exact = SemanticCache._exact_key(1, "m", _MSGS, None, 0.0, **scope)
    semantic = await cache._semantic_key(1, _MSGS, "m", temperature=0.0, **scope)
    assert semantic
    assert redis.ttls[rkey(f"llm:cache:{exact}")] == redis.now + 100
    assert redis.ttls[rkey(f"llm:semantic:{semantic}")] == redis.now + 5

    await redis.delete(rkey(f"llm:cache:{exact}"))
    fresh = _cache(redis, l2_ttl=100, l3_ttl=5)
    hit = await fresh.lookup(1, "m", _MSGS, None, 0.0, **scope)
    assert hit is not None and hit.content == "short-lived"
    assert fresh.stats()["l3_hits"] == 1

    redis.now += 6
    expired = _cache(redis, l2_ttl=100, l3_ttl=5)
    assert await expired.lookup(1, "m", _MSGS, None, 0.0, **scope) is None


@pytest.mark.asyncio
async def test_delete_removes_l1_l2_and_l3_for_the_same_domain() -> None:
    redis = _FakeRedis()
    cache = _cache(redis)
    scope = _scope()
    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="delete-me"), **scope)
    assert len(redis.store) == 2

    removed = await cache.delete(1, "m", _MSGS, None, 0.0, **scope)
    assert removed == 3
    assert redis.store == {}
    assert cache._l1 == {}
    assert await _cache(redis).lookup(1, "m", _MSGS, None, 0.0, **scope) is None


@pytest.mark.asyncio
async def test_delete_does_not_cross_user_boundary() -> None:
    cache = _cache()
    scope_a = _scope(user_id="user-a")
    scope_b = _scope(user_id="user-b")
    await cache.store(1, "m", _MSGS, None, 0.0, ChatResponse(content="keep-me"), **scope_a)

    assert await cache.delete(1, "m", _MSGS, None, 0.0, **scope_b) == 0
    hit = await cache.lookup(1, "m", _MSGS, None, 0.0, **scope_a)
    assert hit is not None and hit.content == "keep-me"
