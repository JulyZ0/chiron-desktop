"""H36：预算改为**原子预占 + 释放/提交**（消除 check→deduct 之间的竞态）。

缺陷形态（`app/gateway/budget.py`）：
  `check()` 读一次 `used`，判断够不够；真正扣减发生在 LLM 调用**之后**的 `deduct()`。
  中间这段时间里，同一租户的 N 个并发请求都读到"还有额度"，于是全部放行 ——
  月末超支额度可以是 N 倍预估值。这不是"统计不准"，是**判据本身有窗口**。

修法：把"判断 + 占位"放进同一个 Lua（Redis 单线程执行，天然互斥）：
  · `reserve()`：`used + reserved + est <= limit` 才 `HINCRBY reserved est`，否则拒绝；
  · `commit()`：`reserved -= est`、`used += actual`（实际用量与预估的差额在此结算）；
  · `release()`：失败路径把占位退回（否则一次上游故障会永久吃掉额度）。

`check()`/`deduct()` 保留（对外行为不变，其它调用方不受影响）。
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis

from app.gateway.budget import TokenBudget

_RESERVE_LUA = """
local limit = tonumber(redis.call('HGET', KEYS[1], 'limit') or '0')
if limit <= 0 then
    return {'unlimited', 0}
end
local used = tonumber(redis.call('HGET', KEYS[1], 'used') or '0')
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
local est = tonumber(ARGV[2])
if used + reserved + est > limit then
    return {'denied', used + reserved}
end
redis.call('HINCRBY', KEYS[1], 'reserved', est)
redis.call('HSET', KEYS[1], 'res:' .. ARGV[1], est)
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {'reserved', used + reserved + est}
"""

_SETTLE_LUA = """
local est = tonumber(redis.call('HGET', KEYS[1], 'res:' .. ARGV[1]) or '0')
if est >= 0 then
    redis.call('HDEL', KEYS[1], 'res:' .. ARGV[1])
    local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
    local left = reserved - est
    if left < 0 then left = 0 end
    redis.call('HSET', KEYS[1], 'reserved', left)
end
local actual = tonumber(ARGV[2])
if actual > 0 then
    redis.call('HINCRBY', KEYS[1], 'used', actual)
end
return 1
"""


class Reservation:
    """一次预占的凭据：`release()`/`commit()` 都要带上它。"""

    __slots__ = ("tenant_id", "estimated", "token", "unlimited")

    def __init__(self, tenant_id: int, estimated: int, token: str, unlimited: bool) -> None:
        self.tenant_id = tenant_id
        self.estimated = estimated
        self.token = token
        self.unlimited = unlimited

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"Reservation(tenant={self.tenant_id}, est={self.estimated}, "
            f"unlimited={self.unlimited})"
        )


async def reserve(
    budget: TokenBudget, tenant_id: int, estimated_tokens: int, *, ttl_seconds: int = 86400
) -> Reservation | None:
    """原子预占。返回 None 表示**额度不足**（调用方应拒绝本次请求）。"""
    if not tenant_id or estimated_tokens <= 0:
        # 没有租户 / 没有预估值：不做预占（与 `check` 的既有语义一致）。
        return Reservation(tenant_id, 0, "", unlimited=True)

    token = uuid.uuid4().hex
    result = await budget._redis.eval(  # noqa: SLF001 - 与 budget 同包内的受控访问
        _RESERVE_LUA,
        1,
        budget._key(tenant_id),  # noqa: SLF001
        token,
        int(estimated_tokens),
        int(ttl_seconds) * 1000,
    )
    status = result[0].decode() if isinstance(result[0], bytes) else str(result[0])
    if status == "denied":
        return None
    return Reservation(tenant_id, int(estimated_tokens), token, status == "unlimited")


async def release(budget: TokenBudget, reservation: Reservation | None) -> None:
    """失败路径：退回占位（不增加 used）。"""
    if reservation is None or reservation.unlimited or not reservation.token:
        return
    await budget._redis.eval(  # noqa: SLF001
        _SETTLE_LUA, 1, budget._key(reservation.tenant_id), reservation.token, 0
    )


async def commit(
    budget: TokenBudget, reservation: Reservation | None, actual_tokens: int
) -> None:
    """成功路径：把占位换成实际用量（差额在此结算）。"""
    if reservation is None:
        return
    if reservation.unlimited or not reservation.token:
        if actual_tokens > 0:
            await budget.deduct(reservation.tenant_id, actual_tokens)
        return
    await budget._redis.eval(  # noqa: SLF001
        _SETTLE_LUA,
        1,
        budget._key(reservation.tenant_id),
        reservation.token,
        max(0, int(actual_tokens)),
    )


async def reserved_total(budget: TokenBudget, tenant_id: int) -> int:
    """当前未结算的占位总量（可观测性 / 测试断言用）。"""
    raw = await budget._redis.hget(budget._key(tenant_id), "reserved")  # noqa: SLF001
    return int(raw or 0)


def _unused(_: aioredis.Redis) -> None:  # pragma: no cover - 保留类型引用
    return None
