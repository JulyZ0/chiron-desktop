"""RunRegistry — session 归属映射（引擎 run owner lease，Redis 镜像）。

背景：agent 进行中 run 的现场状态（AgentRuntime、工具审批、PersistentTerminal）仍在
本进程内，网关只能用「session 一致性哈希」尽力路由；哈希在扩缩容/实例上下线后会漂移，
审批与取消可能落到错误实例，用户只看到含糊的 "no active agent for this session"。

本模块把「哪个实例持有该 session 的 run」写进 Redis（TTL 续期 + run_token），供两侧使用：

- 网关：把该 session 的请求优先路由到映射中的实例（哈希仅作回退，见
  internal/engine/discovery.go 的 applyRunAffinity）；
- 引擎：审批/取消时校验归属与 run_token——映射指向别的实例时返回明确错误
  （而非静默失败）；实例故障后映射随 TTL 过期，用户重试即在新实例重建 run。

键：``{REDIS_KEY_PREFIX}engine:run:{session_id}``
值：``{"instance_id","run_token","owner_uid","url","started_at","last_seen"}``
TTL 300s（与 Go 侧 session run 锁 5min 对齐 —— 那是**另一个机制**，只是约定相同），心跳 100s 续期。
**键名与 Go 侧 `internal/engine/run_affinity.go` 的 `runRecordPrefix` 逐字一致**（引擎写、网关读）；
该一致性已机械化：`scripts/check_tool_policy_parity.py` 的"共享键前缀"族。

注意：这里只登记「归属」，不做 run 现场状态的持久化/迁移——实例故障时该 run 仍会中断
（用户可重试），本模块消除的是「路由到错误实例 + 静默失败」这一类问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RUN_TTL_SECONDS = 300
RUN_HEARTBEAT_SECONDS = 100


@dataclass(frozen=True, slots=True)
class RunFence:
    run_token: str
    generation: int


_CURRENT_FENCE: ContextVar[RunFence | None] = ContextVar("current_run_fence", default=None)
_FENCES: OrderedDict[str, RunFence] = OrderedDict()
_FENCE_LOCK = threading.Lock()
_MAX_FENCES = 4096


def bind_run_fence(run_token: str, generation: int, *, active: bool = False) -> RunFence | None:
    if not run_token or generation < 1:
        return None
    fence = RunFence(run_token, generation)
    with _FENCE_LOCK:
        _FENCES[run_token] = fence
        _FENCES.move_to_end(run_token)
        while len(_FENCES) > _MAX_FENCES:
            _FENCES.popitem(last=False)
    if active:
        activate_run_fence(fence)
    return fence


def current_run_fence() -> RunFence | None:
    return _CURRENT_FENCE.get()


def resolve_run_fence(run_token: str = "", generation: int = 0) -> RunFence | None:
    if generation < 0 or not run_token:
        return None
    with _FENCE_LOCK:
        fence = _FENCES.get(run_token)
    if generation > 0 and fence is not None and fence.generation != generation:
        return None
    return fence


def activate_run_fence(fence: RunFence | None) -> Token[RunFence | None] | None:
    if fence is None or fence.run_token == "" or fence.generation < 1:
        return None
    return _CURRENT_FENCE.set(fence)


def deactivate_run_fence(token: Token[RunFence | None] | None) -> None:
    if token is not None:
        _CURRENT_FENCE.reset(token)


def run_key(session_id: str) -> str:
    from app.redis_keys import rkey

    return rkey("engine:run:") + session_id


def _payload(
    instance_id: str,
    run_token: str,
    generation: int,
    owner_uid: str,
    url: str,
    started_at: str,
) -> str:
    return json.dumps(
        {
            "instance_id": instance_id,
            "run_token": run_token,
            "generation": generation,
            "owner_uid": owner_uid,
            "url": url,
            "started_at": started_at,
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


async def owner_of(redis: Any, session_id: str) -> dict[str, Any] | None:
    """读取 session 的 run 归属；无映射/解析失败/Redis 不可用均返回 None。"""
    if redis is None or not session_id:
        return None
    try:
        raw = await redis.get(run_key(session_id))
    except Exception as exc:  # noqa: BLE001 - 归属查询失败不阻断审批
        logger.debug("run registry read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


class RunLease:
    """一次 run 的归属租约：启动即登记，周期续期，结束（仅 token 匹配时）删除。"""

    def __init__(
        self,
        redis: Any,
        session_id: str,
        instance_id: str,
        run_token: str,
        owner_uid: str = "",
        url: str = "",
        generation: int = 1,
    ) -> None:
        self._redis = redis
        self._session_id = session_id
        self._instance_id = instance_id
        self._run_token = run_token
        self._generation = generation
        self._owner_uid = owner_uid
        self._url = url
        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._task: asyncio.Task[Any] | None = None
        self._started = False
        self._owned = False
        self.fence = RunFence(run_token, generation)

    @property
    def enabled(self) -> bool:
        return bool(self._redis and self._session_id)

    @property
    def owned(self) -> bool:
        return self._owned

    async def start(self) -> bool:
        if not self._run_token or self._generation < 1:
            return False
        if not self.enabled:
            self._started = True
            self._owned = True
            bind_run_fence(self._run_token, self._generation)
            return True
        self._owned = await self._publish(claim=True)
        if not self._owned:
            return False
        self._started = True
        bind_run_fence(self._run_token, self._generation)
        self._task = asyncio.create_task(self._loop())
        return True

    async def stop(self) -> None:
        """停止续期并注销归属（仅当键仍属于本次 token+generation）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._started = False
        if not self.enabled or not self._owned:
            self._owned = False
            return
        try:
            await self._redis.eval(
                _RELEASE_LUA,
                1,
                run_key(self._session_id),
                self._run_token,
                self._generation,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run registry release failed: %s", exc)
        finally:
            self._owned = False

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(RUN_HEARTBEAT_SECONDS)
                await self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 续期失败不阻断 run
            logger.warning("run registry heartbeat failed: %s", exc)

    async def _publish(self, *, claim: bool = False) -> bool:
        if not self._started and not claim:
            return self._owned
        if not claim and not self._owned:
            return False
        payload = _payload(
            self._instance_id,
            self._run_token,
            self._generation,
            self._owner_uid,
            self._url,
            self._started_at,
        )
        try:
            if claim:
                result = await self._redis.eval(
                    _CLAIM_LUA,
                    1,
                    run_key(self._session_id),
                    payload,
                    RUN_TTL_SECONDS,
                    self._run_token,
                    self._generation,
                )
            else:
                result = await self._redis.eval(
                    _PUBLISH_LUA,
                    1,
                    run_key(self._session_id),
                    payload,
                    RUN_TTL_SECONDS,
                    self._run_token,
                    self._generation,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run registry publish failed: %s", exc)
            return self._owned
        self._owned = int(result) == 1
        return self._owned


# Claim only a missing owner or refresh the identical token+generation.
_CLAIM_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur then
  local ok, data = pcall(cjson.decode, cur)
  if not ok or type(data) ~= 'table' or data['run_token'] ~= ARGV[3]
     or data['generation'] ~= tonumber(ARGV[4]) then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

# Heartbeat must never replace a different owner or generation.
_PUBLISH_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
local ok, data = pcall(cjson.decode, cur)
if not ok or type(data) ~= 'table' or data['run_token'] ~= ARGV[3]
   or data['generation'] ~= tonumber(ARGV[4]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


# 仅当键仍属于本次 token+generation 时删除，避免旧 run 的清理删掉新 run 的登记。
_RELEASE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
local ok, data = pcall(cjson.decode, cur)
if ok and type(data) == 'table' and data['run_token'] == ARGV[1]
   and data['generation'] == tonumber(ARGV[2]) then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
