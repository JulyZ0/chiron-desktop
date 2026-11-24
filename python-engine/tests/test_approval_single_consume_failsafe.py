"""审批单次消费 + 安全评审 fail-safe + steer 本地副本 TTL/代数（H37 / PRD 9.7）。

三处缺陷（同一条主线：**别把"没证明"当成"放行"**）：

1. `remote_wait` 用 `GET` + `DEL` 两步消费 —— 两个等待者（两个副本 / 同一副本的两个协程）
   都会先读到同一个值，于是**同一条审批被消费两次**：一次点击批准了两个动作。
2. Guardian 把「理由说没有风险 + 结论 deny」这种**自相矛盾**的判词归一为 `allow`，
   等于让一条不可信的判词换来免人工执行。
3. `steer` 的本地副本没有 TTL / 代数：Redis 不可用时那条文本永久留在进程里，下一次同
   session 的 run 会把它当新指令注入；而 `push_steer` 在 Redis 写失败时仍返回"成功"，
   等于承诺了一个并不存在的跨副本投递。

本文件的攻击探针：并发双消费、Lua 回退、矛盾/失败判词、过期与旧代数的本地副本。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import guardian, remote_wait, steer

# ── 1. 审批键的单次消费 ────────────────────────────────────────────────────


class _PubSub:
    async def subscribe(self, channel: str) -> None:
        return None

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0):
        await asyncio.sleep(0.001)
        return None

    async def aclose(self) -> None:
        return None


class _AtomicRedis:
    """真实语义的近似：`GETDEL` 是**一次**操作（中途不让出事件循环）。"""

    def __init__(self, values: dict[str, bytes]) -> None:
        self.kv = dict(values)
        self.getdel_calls = 0
        self.delete_calls = 0

    async def getdel(self, key: str):
        self.getdel_calls += 1
        return self.kv.pop(key, None)

    async def get(self, key: str):
        self.getdel_calls += 1000  # 不该被用到
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        self.delete_calls += 1
        self.kv.pop(key, None)
        return 1

    def pubsub(self) -> _PubSub:
        return _PubSub()


class _LuaRedis:
    """老服务端：没有 GETDEL，但有 EVAL（Lua 里读+删是原子的）。"""

    def __init__(self, values: dict[str, bytes]) -> None:
        self.kv = dict(values)
        self.eval_calls = 0

    async def eval(self, script: str, numkeys: int, key: str):
        assert numkeys == 1 and "redis.call('GET'" in script
        self.eval_calls += 1
        return self.kv.pop(key, None)

    def pubsub(self) -> _PubSub:
        return _PubSub()


class _LegacyRedis:
    """只有 GET + DEL 的桩：走降级路径（不保证单次消费），必须告警。"""

    def __init__(self, values: dict[str, bytes]) -> None:
        self.kv = dict(values)

    async def get(self, key: str):
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        self.kv.pop(key, None)
        return 1

    def pubsub(self) -> _PubSub:
        return _PubSub()


def _inject(monkeypatch, redis) -> None:
    async def fake_get_redis():
        return redis

    monkeypatch.setattr("app.redis_client.get_redis", fake_get_redis)


async def test_concurrent_waiters_cannot_double_consume(monkeypatch) -> None:
    """攻击探针：两个等待者并发抢同一个审批键 —— 只能有一个拿到。"""
    redis = _AtomicRedis({"approval:tc1": b'{"approved":true}'})
    _inject(monkeypatch, redis)

    results = await asyncio.gather(
        remote_wait.take_key_with_wake("approval:tc1", "wake:tc1", timeout=0.2, interval=0.01),
        remote_wait.take_key_with_wake("approval:tc1", "wake:tc1", timeout=0.2, interval=0.01),
    )
    claimed = [r for r in results if r is not None]
    assert claimed == [b'{"approved":true}'], f"同一条审批被消费了 {len(claimed)} 次"
    assert redis.getdel_calls >= 2, "两次尝试都应走 GETDEL（原子取走）"
    assert redis.delete_calls == 0, "GETDEL 之后不需要再 DEL（两步就是旧的双消费洞）"


async def test_lua_fallback_is_atomic_too(monkeypatch) -> None:
    redis = _LuaRedis({"approval:tc2": b"1"})
    _inject(monkeypatch, redis)

    results = await asyncio.gather(
        remote_wait.take_key_with_wake("approval:tc2", "w", timeout=0.2, interval=0.01),
        remote_wait.take_key_with_wake("approval:tc2", "w", timeout=0.2, interval=0.01),
    )
    assert [r for r in results if r is not None] == [b"1"]
    assert redis.eval_calls >= 2


async def test_legacy_client_still_works_but_is_not_silent(monkeypatch, caplog) -> None:
    """降级路径要留痕：没有 GETDEL/EVAL 时单次消费不再被保证（这是必须告警的事实）。"""
    redis = _LegacyRedis({"approval:tc3": b"0"})
    _inject(monkeypatch, redis)

    with caplog.at_level("WARNING"):
        got = await remote_wait.take_key_with_wake("approval:tc3", "w", timeout=0.2, interval=0.01)
    assert got == b"0"
    assert any("single-consumption" in rec.message for rec in caplog.records)


# ── 2. Guardian 矛盾 / 失败不得归一为 allow ───────────────────────────────


def _contradictory() -> guardian.GuardianAssessment:
    return guardian.parse_assessment(
        '{"outcome":"deny","risk_level":"low","danger_found":false,'
        '"rationale":"only prints a greeting"}'
    )


def test_contradictory_verdict_is_not_upgraded_to_allow() -> None:
    a = _contradictory()
    assert a.contradictory is True
    assert a.outcome == "unknown", "矛盾判词不得归一为 allow（旧实现的洞）"
    assert a.failed is True, "矛盾 = 没评成 ⇒ 回退人工审批"
    assert guardian.may_release("write", a) is False, "矛盾判词绝不能免人工执行"


def test_failed_review_never_releases() -> None:
    failed = guardian.GuardianAssessment("unknown", "unknown", "gateway timeout", failed=True)
    assert guardian.may_release("write", failed) is False


def test_consistent_allow_still_releases_and_deny_still_denies() -> None:
    allow = guardian.parse_assessment(
        '{"outcome":"allow","risk_level":"low","danger_found":false,"rationale":"reads a file"}'
    )
    assert (allow.outcome, allow.failed) == ("allow", False)
    assert guardian.may_release("write", allow) is True

    deny = guardian.parse_assessment(
        '{"outcome":"deny","risk_level":"critical","danger_found":true,"rationale":"rm -rf /"}'
    )
    assert (deny.outcome, deny.failed) == ("deny", False)


# ── 3. steer 本地副本的 TTL / 代数 / 交付承诺 ─────────────────────────────


class _BreakingRedis:
    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def lpush(self, key: str, value: str) -> None:
        raise RuntimeError("redis write failed")

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_steer():
    steer._LOCAL.clear()
    steer._GENERATION.clear()
    yield
    steer._LOCAL.clear()
    steer._GENERATION.clear()


@pytest.mark.asyncio
async def test_local_steer_expires_by_ttl(monkeypatch) -> None:
    """TTL：过了 STEER_TTL_SECONDS 的本地副本绝不能再被注入到别的 run 里。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(steer, "_now", lambda: clock["now"])

    await steer.push_steer("s-ttl", "旧指令", redis=_AtomicRedis({}))
    clock["now"] += steer.STEER_TTL_SECONDS + 1
    assert await steer.drain_steer("s-ttl", redis=None) == []


@pytest.mark.asyncio
async def test_clear_invalidates_local_copy_by_generation(monkeypatch) -> None:
    """代数：run 收尾 clear 之后，旧代数的本地副本一律作废（不会被下一次 drain 取走）。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(steer, "_now", lambda: clock["now"])

    await steer.push_steer("s-gen", "第一轮指令", redis=_AtomicRedis({}))
    steer.clear_steer_local("s-gen")

    # 模拟并发：clear 之后仍有一条旧代数的副本落进队列
    steer._LOCAL.setdefault("s-gen", []).append((clock["now"], 0, "旧代数残留", "watchdog"))
    assert await steer.drain_steer("s-gen", redis=None) == [], "旧代数副本不能被注入"

    await steer.push_steer("s-gen", "第二轮指令", redis=_AtomicRedis({}))
    assert await steer.drain_steer("s-gen", redis=None) == [("第二轮指令", "user")]


@pytest.mark.asyncio
async def test_redis_failure_does_not_promise_cross_replica_delivery() -> None:
    ok, reason = await steer.push_steer("s-fail", "帮我看下日志", redis=_BreakingRedis())
    assert ok is True, "本副本已经入队，不能报成彻底失败（否则用户会重试、产生重复指令）"
    assert reason.startswith(steer.LOCAL_ONLY_PREFIX), "必须显式声明只投递到本副本"
    assert await steer.drain_steer("s-fail", redis=None) == [("帮我看下日志", "user")]


@pytest.mark.asyncio
async def test_missing_redis_also_reports_local_only() -> None:
    ok, reason = await steer.push_steer("s-no-redis", "继续", redis=None)
    assert ok is True
    assert reason.startswith(steer.LOCAL_ONLY_PREFIX)


@pytest.mark.asyncio
async def test_successful_redis_push_has_empty_reason() -> None:
    class _Ok:
        def __init__(self) -> None:
            self.lists: dict[str, list[str]] = {}

        async def incr(self, key: str) -> int:
            return 1

        async def expire(self, key: str, seconds: int) -> bool:
            return True

        async def lpush(self, key: str, value: str) -> None:
            self.lists.setdefault(key, []).insert(0, value)

        async def ltrim(self, key: str, start: int, stop: int) -> None:
            return None

    ok, reason = await steer.push_steer("s-ok", "继续", redis=_Ok())
    assert (ok, reason) == (True, ""), "Redis 写入成功才算跨副本投递已落实"
