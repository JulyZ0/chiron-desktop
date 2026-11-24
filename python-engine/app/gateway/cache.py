"""Three-tier response cache with an explicit, fail-closed sharing domain."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

import redis.asyncio as aioredis

from app.gateway.provider import ChatMessage, ChatResponse
from app.redis_keys import rkey

logger = logging.getLogger(__name__)


class SemanticCache:
    """L1 process LRU, L2 Redis exact, and L3 Redis semantic response cache.

    Every tier is disabled unless the caller supplies a user, a non-private scope,
    a policy digest, and model lineage. L3 keys include a digest of every message,
    not a recent-message window. L3 TTL defaults to ``l2_ttl`` and can be overridden
    with ``l3_ttl``.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        embed_fn: Any,
        l1_capacity: int = 2048,
        l2_ttl: int = 3600,
        semantic_threshold: float = 0.95,
        semantic_prefix_dims: int = 64,
        l3_ttl: int | None = None,
    ) -> None:
        if l2_ttl <= 0:
            raise ValueError("l2_ttl must be positive")
        if l3_ttl is not None and l3_ttl <= 0:
            raise ValueError("l3_ttl must be positive")

        self._redis = redis
        self._embed_fn = embed_fn
        self._l2_ttl = l2_ttl
        self._l3_ttl = l2_ttl if l3_ttl is None else l3_ttl
        self._semantic_threshold = semantic_threshold
        self._semantic_prefix_dims = semantic_prefix_dims
        self._l1_capacity = l1_capacity
        self._l1: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

        self._hits_l1 = 0
        self._hits_l2 = 0
        self._hits_l3 = 0
        self._misses = 0

    @property
    def l2_ttl(self) -> int:
        return self._l2_ttl

    @property
    def l3_ttl(self) -> int:
        return self._l3_ttl

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _identity(value: int | str | None) -> str:
        if value is None or isinstance(value, bool):
            return ""
        return str(value).strip()

    @classmethod
    def _lineage(cls, model_lineage: Mapping[str, Any] | str | None) -> str:
        if isinstance(model_lineage, str):
            return model_lineage.strip()
        if isinstance(model_lineage, Mapping) and model_lineage:
            return cls._canonical(model_lineage)
        return ""

    @classmethod
    def _make_domain(
        cls,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        context = [message.to_dict() for message in messages]
        context_raw = cls._canonical(context).encode("utf-8")
        return {
            "version": 2,
            "tenant": cls._identity(tenant_id),
            "user": cls._identity(user_id),
            "private": private is not False,
            "context_digest": hashlib.sha256(context_raw).hexdigest(),
            "tools": cls._canonical(tools),
            "policy_digest": policy_digest.strip() if isinstance(policy_digest, str) else "",
            "model_lineage": cls._lineage(model_lineage),
            "model": model.strip() if isinstance(model, str) else "",
            "temperature": temperature,
            "response_format": cls._canonical(response_format),
        }

    @staticmethod
    def _domain_enabled(domain: Mapping[str, Any]) -> bool:
        return (
            domain["private"] is False
            and domain["tenant"] not in ("", "0")
            and bool(domain["user"])
            and bool(domain["policy_digest"])
            and bool(domain["model_lineage"])
            and bool(domain["model"])
        )

    @classmethod
    def _public_domain(
        cls,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> dict[str, Any] | None:
        try:
            domain = cls._make_domain(
                tenant_id,
                model,
                messages,
                tools,
                temperature,
                response_format,
                user_id=user_id,
                private=private,
                policy_digest=policy_digest,
                model_lineage=model_lineage,
            )
        except (TypeError, ValueError) as exc:
            logger.debug("Cache domain computation failed: %s", exc)
            return None
        return domain if cls._domain_enabled(domain) else None

    @staticmethod
    def _domain_digest(domain: Mapping[str, Any]) -> str:
        raw = SemanticCache._canonical(domain).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def _exact_key_from_domain(cls, domain: Mapping[str, Any]) -> str:
        return cls._domain_digest(domain)[:32]

    @classmethod
    def _exact_key(
        cls,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> str:
        domain = cls._make_domain(
            tenant_id,
            model,
            messages,
            tools,
            temperature,
            response_format,
            user_id=user_id,
            private=private,
            policy_digest=policy_digest,
            model_lineage=model_lineage,
        )
        return cls._exact_key_from_domain(domain)

    @classmethod
    async def _semantic_key_from_domain(
        cls,
        messages: list[ChatMessage],
        domain: Mapping[str, Any],
        embed_fn: Any,
        prefix_dims: int,
    ) -> str | None:
        try:
            if any(not isinstance(message.content, str) for message in messages):
                return None
            text = "\n".join(f"{message.role}:{message.content}" for message in messages if message.content)
            if len(text) < 10:
                return None

            embedding = await embed_fn(text)
            if not embedding:
                return None
            prefix = embedding[:prefix_dims]
            quantized = bytes(max(0, min(255, int((value + 1) * 127.5))) for value in prefix)
            material = quantized + cls._canonical(domain).encode("utf-8")
            return hashlib.sha256(material).hexdigest()[:16]
        except Exception as exc:
            logger.debug("Semantic key computation failed: %s", exc)
            return None

    async def _semantic_key(
        self,
        tenant_id: int | str,
        messages: list[ChatMessage],
        model: str,
        response_format: dict[str, Any] | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> str | None:
        try:
            domain = self._make_domain(
                tenant_id,
                model,
                messages,
                tools,
                temperature,
                response_format,
                user_id=user_id,
                private=private,
                policy_digest=policy_digest,
                model_lineage=model_lineage,
            )
        except (TypeError, ValueError) as exc:
            logger.debug("Semantic domain computation failed: %s", exc)
            return None
        return await self._semantic_key_from_domain(messages, domain, self._embed_fn, self._semantic_prefix_dims)

    async def lookup(
        self,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> ChatResponse | None:
        domain = self._public_domain(
            tenant_id,
            model,
            messages,
            tools,
            temperature,
            response_format,
            user_id=user_id,
            private=private,
            policy_digest=policy_digest,
            model_lineage=model_lineage,
        )
        if domain is None or tools:
            return None

        exact = self._exact_key_from_domain(domain)
        data = self._l1_get(exact)
        if data is not None:
            self._l1.move_to_end(exact)
            self._hits_l1 += 1
            return self._decode(data, model)

        raw = await self._redis.get(rkey(f"llm:cache:{exact}"))
        if raw:
            data = self._json_object(raw)
            if data is not None:
                self._l1_set(exact, data)
                self._hits_l2 += 1
                return self._decode(data, model)

        semantic = await self._semantic_key_from_domain(messages, domain, self._embed_fn, self._semantic_prefix_dims)
        if semantic:
            raw = await self._redis.get(rkey(f"llm:semantic:{semantic}"))
            if raw:
                data = self._json_object(raw)
                if data is not None:
                    self._l1_set(exact, data)
                    self._hits_l3 += 1
                    return self._decode(data, model)

        self._misses += 1
        return None

    async def store(
        self,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response: ChatResponse,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> None:
        domain = self._public_domain(
            tenant_id,
            model,
            messages,
            tools,
            temperature,
            response_format,
            user_id=user_id,
            private=private,
            policy_digest=policy_digest,
            model_lineage=model_lineage,
        )
        if domain is None or tools or response.tool_calls or response.finish_reason == "tool_calls":
            return

        exact = self._exact_key_from_domain(domain)
        semantic = await self._semantic_key_from_domain(messages, domain, self._embed_fn, self._semantic_prefix_dims)
        data = self._encode(response)
        if semantic:
            data["semantic_key"] = semantic

        self._l1_set(exact, data)
        await self._redis.set(
            rkey(f"llm:cache:{exact}"),
            json.dumps(data, ensure_ascii=False),
            ex=self._l2_ttl,
        )
        if semantic:
            await self._redis.set(
                rkey(f"llm:semantic:{semantic}"),
                json.dumps(data, ensure_ascii=False),
                ex=self._l3_ttl,
            )

    async def delete(
        self,
        tenant_id: int | str,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None = None,
        *,
        user_id: int | str | None = "",
        private: bool = True,
        policy_digest: str = "",
        model_lineage: Mapping[str, Any] | str | None = None,
    ) -> int:
        """Remove one complete cache domain and return the number of deleted entries."""
        domain = self._public_domain(
            tenant_id,
            model,
            messages,
            tools,
            temperature,
            response_format,
            user_id=user_id,
            private=private,
            policy_digest=policy_digest,
            model_lineage=model_lineage,
        )
        if domain is None:
            return 0

        exact = self._exact_key_from_domain(domain)
        exact_redis_key = rkey(f"llm:cache:{exact}")
        semantic = await self._semantic_key_from_domain(messages, domain, self._embed_fn, self._semantic_prefix_dims)
        if not semantic:
            raw = await self._redis.get(exact_redis_key)
            data = self._json_object(raw) if raw else None
            candidate = data.get("semantic_key") if data else None
            if isinstance(candidate, str):
                semantic = candidate

        removed = self._l1_delete(exact)
        removed += await self._redis.delete(exact_redis_key)
        if semantic:
            removed += await self._redis.delete(rkey(f"llm:semantic:{semantic}"))
        return removed

    @staticmethod
    def _json_object(raw: Any) -> dict[str, Any] | None:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _l1_get(self, key: str) -> dict[str, Any] | None:
        entry = self._l1.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at <= time.time():
            self._l1.pop(key, None)
            return None
        return data

    def _l1_set(self, key: str, data: dict[str, Any]) -> None:
        try:
            cached_at = float(data.get("cached_at", time.time()))
        except (TypeError, ValueError):
            cached_at = time.time()
        expires_at = cached_at + self._l2_ttl
        if expires_at <= time.time():
            return
        self._l1[key] = (expires_at, data)
        self._l1.move_to_end(key)
        while len(self._l1) > self._l1_capacity:
            self._l1.popitem(last=False)

    def _l1_delete(self, key: str) -> int:
        return int(self._l1.pop(key, None) is not None)

    @staticmethod
    def _encode(response: ChatResponse) -> dict[str, Any]:
        return {
            "content": response.content,
            "tool_calls": [
                {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
                for tool_call in response.tool_calls
            ],
            "finish_reason": response.finish_reason,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cached_at": time.time(),
        }

    @staticmethod
    def _decode(data: dict[str, Any], model: str) -> ChatResponse:
        from app.gateway.provider import ToolCall

        return ChatResponse(
            content=data.get("content", ""),
            tool_calls=[
                ToolCall(id=tool_call["id"], name=tool_call["name"], arguments=tool_call["arguments"])
                for tool_call in data.get("tool_calls", [])
            ],
            finish_reason=data.get("finish_reason", ""),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            model=model,
            provider="cache",
        )

    def stats(self) -> dict[str, Any]:
        total = self._hits_l1 + self._hits_l2 + self._hits_l3 + self._misses
        return {
            "l1_hits": self._hits_l1,
            "l2_hits": self._hits_l2,
            "l3_hits": self._hits_l3,
            "misses": self._misses,
            "hit_rate": (self._hits_l1 + self._hits_l2 + self._hits_l3) / max(total, 1),
        }
