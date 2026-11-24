"""Redis-backed exclusive task claims and fenced attempt transitions.

A claim is a lease with a unique token. Every completion, failure, or ACK checks
both the token and PEL consumer, so a reclaimed attempt cannot mutate its successor.
Completed keys suppress duplicate side effects for a bounded retention window.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

RETENTION_SECONDS = max(1, int(os.getenv("QUEUE_IDEMPOTENCY_RETENTION_SECONDS", "2592000")))

_CLAIM_LUA = """
local kind = redis.call('TYPE', KEYS[1])['ok']
if kind ~= 'none' and kind ~= 'hash' then
    return redis.error_reply('WRONGTYPE idempotency state is not a hash')
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local status = redis.call('HGET', KEYS[1], 'status')
local attempt = tonumber(redis.call('HGET', KEYS[1], 'attempt') or '0')
if status == 'completed' then
    return {'completed', attempt}
end
local lease_until = tonumber(redis.call('HGET', KEYS[1], 'lease_until') or '0')
if status == 'running' and lease_until > now then
    return {'busy', attempt}
end
attempt = attempt + 1
redis.call('HSET', KEYS[1],
    'status', 'running',
    'attempt', attempt,
    'token', ARGV[1],
    'task_id', ARGV[2],
    'task_type', ARGV[3],
    'tenant_id', ARGV[4],
    'lease_until', now + tonumber(ARGV[5]),
    'updated_at', now)
redis.call('PEXPIRE', KEYS[1], ARGV[6])
return {'acquired', attempt, ARGV[1]}
"""

_FAIL_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running'
    or redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then
    return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('HSET', KEYS[1], 'status', 'failed', 'lease_until', 0, 'updated_at', now)
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

_ACK_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running'
    or redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then
    return 0
end
local pending = redis.call('XPENDING', KEYS[2], ARGV[2], ARGV[4], ARGV[4], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3] then
    return 0
end
redis.call('XACK', KEYS[2], ARGV[2], ARGV[4])
return 1
"""

_ACK_COMPLETED_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'completed' then
    return 0
end
local pending = redis.call('XPENDING', KEYS[2], ARGV[1], ARGV[3], ARGV[3], 1)
if #pending == 0 or pending[1][2] ~= ARGV[2] then
    return 0
end
redis.call('XACK', KEYS[2], ARGV[1], ARGV[3])
return 1
"""

_RENEW_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running'
    or redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then
    return 0
end
local pending = redis.call('XPENDING', KEYS[2], ARGV[2], ARGV[4], ARGV[4], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3] then
    return 0
end
local claimed = redis.call('XCLAIM', KEYS[2], ARGV[2], ARGV[3], 0, ARGV[4], 'JUSTID')
if #claimed == 0 then
    return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('HSET', KEYS[1], 'lease_until', now + tonumber(ARGV[5]), 'updated_at', now)
redis.call('PEXPIRE', KEYS[1], ARGV[6])
return 1
"""

_TRANSITION_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= 'running'
    or redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then
    return 0
end
local pending = redis.call('XPENDING', KEYS[2], ARGV[2], ARGV[4], ARGV[4], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3] then
    return 0
end
if ARGV[5] == 'xadd' then
    redis.call('XADD', KEYS[3], 'MAXLEN', ARGV[7], '*', unpack(ARGV, 11))
elseif ARGV[5] == 'zadd' then
    redis.call('ZADD', KEYS[3], ARGV[8], ARGV[11])
elseif ARGV[5] ~= 'none' then
    return redis.error_reply('unknown idempotency transition')
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('HSET', KEYS[1],
    'status', ARGV[6],
    'result_ref', ARGV[9],
    'lease_until', 0,
    'updated_at', now)
redis.call('PEXPIRE', KEYS[1], ARGV[10])
redis.call('XACK', KEYS[2], ARGV[2], ARGV[4])
return 1
"""

_PURGE_SQL = """
DELETE FROM task_idempotency
WHERE status <> 'running'
  AND updated_at < NOW() - make_interval(days => $1)
"""


class ClaimStatus(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Attempt:
    namespace: str
    key: str
    state_key: str
    token: str
    attempt: int


@dataclass(frozen=True)
class ClaimDecision:
    status: ClaimStatus
    claim: Attempt | None = None


def key_for(task_type: str, task_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    return f"{task_type}:{task_id}"


def state_key(namespace: str, key: str) -> str:
    from app.redis_keys import rkey

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return rkey(f"queue:idempotency:{namespace}:{digest}")


def _field_args(fields: dict[Any, Any]) -> list[str]:
    return [
        str(part)
        for key, value in fields.items()
        for part in (
            key.decode() if isinstance(key, bytes) else key,
            value.decode() if isinstance(value, bytes) else value,
        )
    ]


async def claim(
    redis: aioredis.Redis,
    *,
    namespace: str,
    key: str,
    task_id: str,
    task_type: str,
    tenant_id: int,
    lease_seconds: int = 600,
) -> ClaimDecision:
    """Acquire an exclusive lease; Redis errors propagate rather than fail open."""
    lease_ms = max(1, lease_seconds) * 1000
    result = await redis.eval(
        _CLAIM_LUA,
        1,
        state_key(namespace, key),
        uuid.uuid4().hex,
        task_id,
        task_type,
        str(tenant_id),
        lease_ms,
        RETENTION_SECONDS * 1000,
    )
    status = ClaimStatus(result[0].decode() if isinstance(result[0], bytes) else result[0])
    attempt = int(result[1])
    if status is not ClaimStatus.ACQUIRED:
        return ClaimDecision(status)
    token = result[2].decode() if isinstance(result[2], bytes) else str(result[2])
    return ClaimDecision(
        ClaimStatus.ACQUIRED,
        Attempt(
            namespace=namespace,
            key=key,
            state_key=state_key(namespace, key),
            token=token,
            attempt=attempt,
        ),
    )


async def fail(redis: aioredis.Redis, claim: Attempt) -> bool:
    """Release a failed current attempt so a retry can acquire the next one."""
    result = await redis.eval(
        _FAIL_LUA, 1, claim.state_key, claim.token, RETENTION_SECONDS * 1000
    )
    return bool(result)


async def ack(
    redis: aioredis.Redis,
    claim: Attempt,
    stream: str,
    group: str,
    consumer: str,
    message_id: str,
) -> bool:
    result = await redis.eval(
        _ACK_LUA,
        2,
        claim.state_key,
        stream,
        claim.token,
        group,
        consumer,
        message_id,
    )
    return bool(result)


async def ack_completed(
    redis: aioredis.Redis,
    *,
    namespace: str,
    key: str,
    stream: str,
    group: str,
    consumer: str,
    message_id: str,
) -> bool:
    result = await redis.eval(
        _ACK_COMPLETED_LUA,
        2,
        state_key(namespace, key),
        stream,
        group,
        consumer,
        message_id,
    )
    return bool(result)


async def renew(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    stream: str,
    group: str,
    consumer: str,
    message_id: str,
    lease_seconds: int,
) -> bool:
    """Renew the lease and PEL idle time only while token and consumer still match."""
    result = await redis.eval(
        _RENEW_LUA,
        2,
        claim.state_key,
        stream,
        claim.token,
        group,
        consumer,
        message_id,
        max(1, lease_seconds) * 1000,
        RETENTION_SECONDS * 1000,
    )
    return bool(result)


async def _transition(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    operation: str,
    status: str,
    fields: dict[Any, Any] | None = None,
    maxlen: int = 0,
    score: float = 0,
    result_ref: str = "",
) -> bool:
    field_args = _field_args(fields or {})
    if operation == "zadd":
        field_args = [str(fields["member"] if fields and "member" in fields else "")]
    result = await redis.eval(
        _TRANSITION_LUA,
        3,
        claim.state_key,
        source,
        target,
        claim.token,
        group,
        consumer,
        message_id,
        operation,
        status,
        maxlen,
        score,
        result_ref,
        RETENTION_SECONDS * 1000,
        *field_args,
    )
    return bool(result)


async def complete(
    redis: aioredis.Redis,
    claim: Attempt,
    stream: str,
    group: str,
    consumer: str,
    message_id: str,
    result_ref: str = "",
) -> bool:
    return await _transition(
        redis,
        claim,
        source=stream,
        target=stream,
        group=group,
        consumer=consumer,
        message_id=message_id,
        operation="none",
        status="completed",
        result_ref=result_ref,
    )


async def retry(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    fields: dict[Any, Any],
    maxlen: int,
) -> bool:
    return await _transition(
        redis,
        claim,
        source=source,
        target=target,
        group=group,
        consumer=consumer,
        message_id=message_id,
        operation="xadd",
        status="failed",
        fields=fields,
        maxlen=maxlen,
    )


async def defer(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    member: str,
    score: float,
) -> bool:
    return await _transition(
        redis,
        claim,
        source=source,
        target=target,
        group=group,
        consumer=consumer,
        message_id=message_id,
        operation="zadd",
        status="failed",
        fields={"member": member},
        score=score,
    )


async def dead_letter(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    fields: dict[Any, Any],
    maxlen: int,
) -> bool:
    return await _transition(
        redis,
        claim,
        source=source,
        target=target,
        group=group,
        consumer=consumer,
        message_id=message_id,
        operation="xadd",
        status="failed",
        fields=fields,
        maxlen=maxlen,
    )


async def forward(
    redis: aioredis.Redis,
    claim: Attempt,
    *,
    source: str,
    target: str,
    group: str,
    consumer: str,
    message_id: str,
    fields: dict[Any, Any],
    maxlen: int,
) -> bool:
    return await _transition(
        redis,
        claim,
        source=source,
        target=target,
        group=group,
        consumer=consumer,
        message_id=message_id,
        operation="xadd",
        status="completed",
        fields=fields,
        maxlen=maxlen,
    )


async def purge_older_than(days: int) -> int:
    """Clean legacy PostgreSQL rows; Redis state expires via its retention TTL."""
    if days <= 0:
        return 0
    try:
        from app.db import get_pool

        pool = get_pool()
    except Exception:  # noqa: BLE001
        return 0
    try:
        result = await pool.execute(_PURGE_SQL, days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy idempotency purge failed: %s", exc)
        return 0
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0
