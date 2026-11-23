"""按分钟的模型 token 配额（TPM，R7）。

口径：**调用前**看当前分钟已用是否已达上限，**调用后**把实际 input+output 累计进去。
不做 token 预占：并发调用可能在一个窗口内小幅超出上限（最多各自一次调用），换来的是
零额外延迟与更简单的失败语义；真正的硬预算由日费用熔断与模型侧配额共同兜底。

- 键按 UTC 分钟分桶（`ratelimit:llm:tpm:{tenant|user}:<key>:<yyyymmddHHMM>`），TTL 120s。
- 限额 <=0 表示关闭该维度；Redis 异常由调用方按 fail-open 处理。
- 用户身份来自运行时上下文；没有用户上下文的队列任务只受租户级约束。
"""

from __future__ import annotations

import time
from typing import Any

from app.redis_keys import rkey

_KEY_PREFIX = "ratelimit:llm:tpm"
_TTL_SECONDS = 120


def _minute() -> str:
    return time.strftime("%Y%m%d%H%M", time.gmtime())


class TokenQuota:
    def __init__(
        self,
        redis: Any,
        *,
        tenant_tpm: int = 0,
        user_tpm: int = 0,
        key_prefix: str = _KEY_PREFIX,
    ) -> None:
        self._redis = redis
        self.tenant_tpm = max(0, int(tenant_tpm or 0))
        self.user_tpm = max(0, int(user_tpm or 0))
        self._prefix = key_prefix.strip().strip(":") or _KEY_PREFIX

    @property
    def enabled(self) -> bool:
        return self.tenant_tpm > 0 or self.user_tpm > 0

    def _key(self, scope: str, key: int | str) -> str:
        return rkey(f"{self._prefix}:{scope}:{key}:{_minute()}")

    async def _used(self, scope: str, key: int | str) -> int:
        raw = await self._redis.get(self._key(scope, key))
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    async def exceeded(self, tenant_id: int, user_id: str) -> str:
        """返回超限层级（tenant/user）；空串表示放行。"""
        if self.tenant_tpm > 0 and tenant_id:
            if await self._used("tenant", tenant_id) >= self.tenant_tpm:
                return "tenant"
        if self.user_tpm > 0 and user_id:
            if await self._used("user", user_id) >= self.user_tpm:
                return "user"
        return ""

    async def add(self, tenant_id: int, user_id: str, tokens: int) -> None:
        """把一次调用的实际 token 记入当前分钟。任一维度未启用则跳过该键。"""
        if tokens <= 0:
            return
        pairs: list[tuple[str, int | str]] = []
        if self.tenant_tpm > 0 and tenant_id:
            pairs.append(("tenant", tenant_id))
        if self.user_tpm > 0 and user_id:
            pairs.append(("user", user_id))
        for scope, key in pairs:
            name = self._key(scope, key)
            await self._redis.incrby(name, tokens)
            await self._redis.expire(name, _TTL_SECONDS)
