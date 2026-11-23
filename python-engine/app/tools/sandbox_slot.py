"""按租户的沙箱/代码执行并发槽（R7）。

哪些工具算"沙箱执行"：`run_code`、`execute_python`、`shell_exec`、`persistent_shell`。
限制的是**同时真正跑代码**的数量，而不是文件读写/搜索这类轻工具 —— 后者已有每回合
批内并发上限，再叠一层租户配额只会无谓地拖慢长任务。

计数放 Redis（多引擎副本共享），键 `sem:sandbox:{tenant}`；limit<=0 关闭。
Redis 不可用按放行处理（fail-open）：配额组件故障不该让代码执行全挂。

嵌套调用：`run_code` 程序内部还会调 `shell_exec` 等工具。第一次获取槽位时在
contextvar 里打标记，嵌套的那几次看到标记就不再占槽 —— 否则一个小配额会被
"一个 run_code 里的五次 shell"瞬间打满，语义变成"限制嵌套次数"而不是"限制并发"。
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Iterator

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

#: 计入沙箱并发的工具名（与注册表里的 name 一致）。
SANDBOX_TOOL_NAMES = frozenset(
    {"run_code", "execute_python", "shell_exec", "persistent_shell"}
)

_SLOT_TTL_SECONDS = 300
_slot_key_prefix = "sem:sandbox:"

_held: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "chiron_sandbox_slot_held", default=False
)

_ACQUIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
if v > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

_RELEASE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local n = tonumber(cur)
if not n or n <= 0 then return 0 end
redis.call('DECR', KEYS[1])
return 1
"""


def sandbox_slot_held() -> bool:
    return bool(_held.get())


class SandboxLease:
    """一次槽位持有。`key` 为空表示未启用限额或 Redis 不可用（恒放行）。"""

    def __init__(self, key: str = "", redis: Any = None) -> None:
        self.key = key
        self._redis = redis
        self._released = False

    @contextmanager
    def hold(self) -> Iterator[None]:
        token = _held.set(True)
        try:
            yield
        finally:
            _held.reset(token)

    async def release(self) -> None:
        if self._released or not self.key or self._redis is None:
            return
        self._released = True
        try:
            await self._redis.eval(_RELEASE_LUA, 1, self.key)
        except Exception as exc:  # noqa: BLE001 - 释放失败由 TTL 兜底
            logger.warning("sandbox slot release failed: %s", exc)


async def acquire_sandbox_slot() -> SandboxLease | None:
    """尝试获取租户槽位。返回 None 表示已满；未启用/Redis 故障返回空 key 的租约。"""
    from app.config import settings
    from app.tools.context import get_tenant_id_int

    limit = int(getattr(settings, "sandbox_max_concurrency_per_tenant", 0) or 0)
    tenant = get_tenant_id_int()
    if limit <= 0 or not tenant:
        return SandboxLease()

    key = rkey(f"{_slot_key_prefix}{tenant}")
    try:
        from app.redis_client import get_redis

        redis = await get_redis()
        ok = await redis.eval(_ACQUIRE_LUA, 1, key, limit, _SLOT_TTL_SECONDS)
        if int(ok or 0) == 1:
            return SandboxLease(key=key, redis=redis)
        return None
    except Exception as exc:  # noqa: BLE001 - 配额组件故障时放行
        logger.warning("sandbox slot acquire failed; allowing execution: %s", exc)
        return SandboxLease()
