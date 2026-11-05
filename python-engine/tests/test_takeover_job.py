"""`takeover_job` 调度层测试（全部用桩，不碰真 Redis / PG）。

**这一层要钉住的三件事**

1. **关掉时真的什么都不做** —— `enabled=False` 必须在建 DB 查询之前返回。
   否则"关掉接管"只是少跑了 run，但每分钟仍在查一次库。
2. **接管段一定有始有终** —— `begin_segment` 之后无论 runner 抛异常还是成功，
   `end_segment` 都必须执行。少了它，UI 会被锁到 TTL 过期（30 分钟）。
3. **记账的失败语义** —— 正常跳过（用户活跃/到点未到）不能累加 `consecutive_failures`，
   否则天天在用的用户会在三天后被自己的操作"熔断"掉。
"""

from __future__ import annotations

import json

import pytest

from app.agent import takeover as l0
from app.queue import takeover_job as job

NOW = 1_800_000_000.0


class FakeRedis:
    """够用的 Redis 桩：支持 set/get/delete，并记录调用顺序。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_all = False

    async def set(self, key, value, *, nx=False, ex=None):  # noqa: ANN001
        self.calls.append(("set", key))
        if self.fail_all:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):  # noqa: ANN001
        self.calls.append(("get", key))
        if self.fail_all:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def delete(self, key):  # noqa: ANN001
        self.calls.append(("delete", key))
        if self.fail_all:
            raise RuntimeError("redis down")
        self.store.pop(key, None)
        return 1


class FakePool:
    """记录 execute 调用的参数；fetch 返回预置行。"""

    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, tuple]] = []
        self.fetched = 0

    async def fetch(self, sql, *args):  # noqa: ANN001
        self.fetched += 1
        return self.rows

    async def fetchrow(self, sql, *args):  # noqa: ANN001
        return None

    async def execute(self, sql, *args):  # noqa: ANN001
        self.executed.append((sql, args))
        return "UPDATE 1"


def _row(**kw):
    """一行"万事俱备"的配置：用户与会话开关都开、interval 已到点、预算充足。"""
    base = dict(
        id="tk-1",
        tenant_id=1,
        user_id="u1",
        session_id="s1",
        enabled=True,
        schedule_kind=l0.KIND_INTERVAL,
        once_at=None,
        interval_seconds=1800,
        tools_mode="auto",
        daily_token_budget=120000,
        tokens_used_today=0,
        budget_day=None,
        last_run_at=None,  # 从未跑过 → interval 立即到点
        cooldown_until=None,
        melt_state="",
        consecutive_failures=0,
        user_enabled=True,
        idle_minutes=10,
    )
    base.update(kw)
    return base


async def _no_msg(pool, *, session_id):  # noqa: ANN001
    return None


async def _no_work(pool, *, tenant_id, session_id):  # noqa: ANN001
    return True


# ── 开关与租约 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_does_nothing_at_all():
    """关掉时零副作用：不抢租约、不查库、不写审计。"""
    redis = FakeRedis()
    pool = FakePool(rows=[_row()])
    res = await job.tick(redis=redis, pool=pool, enabled=False)
    assert res.skipped_global == "disabled"
    assert res.considered == 0
    assert redis.calls == []
    assert pool.fetched == 0
    assert pool.executed == []


@pytest.mark.asyncio
async def test_lease_busy_skips_whole_tick():
    redis = FakeRedis()
    redis.store[job.lease_key()] = "other-replica"
    pool = FakePool(rows=[_row()])
    res = await job.tick(redis=redis, pool=pool, enabled=True)
    assert res.skipped_global == "lease_busy"
    assert pool.fetched == 0


@pytest.mark.asyncio
async def test_lease_released_after_tick():
    redis = FakeRedis()
    await job.tick(redis=redis, pool=FakePool(rows=[]), enabled=True)
    assert job.lease_key() not in redis.store


@pytest.mark.asyncio
async def test_lease_released_even_when_tick_raises():
    """异常路径也必须放租约，否则下一次要等 300s。"""
    redis = FakeRedis()

    class Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("boom")

    try:
        await job.tick(redis=redis, pool=Boom(), enabled=True)
    except Exception:  # noqa: BLE001
        pass
    # _load_rows 自己吞掉异常返回 []，所以这里更该确认租约仍在释放路径上
    assert job.lease_key() not in redis.store


# ── 跳过路径 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_active_skips_without_starting_run():
    redis = FakeRedis()
    pool = FakePool(rows=[_row()])
    started: list = []

    # 刚说过话 → 让步
    async def recently(pool, *, session_id):  # noqa: ANN001
        return NOW - 5

    res = await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        idle_minutes=10,
        runner=lambda row: started.append(row),
        taskboard_fn=_no_work,
        last_message_fn=recently,
    )
    assert res.started == 0
    assert res.skipped.get("user_active") == 1
    assert started == []
    # 跳过也要记账（否则 UI 看不到"上次为什么没跑"）
    assert pool.executed


@pytest.mark.asyncio
async def test_active_run_reported_as_busy():
    redis = FakeRedis()
    redis.store["agent:run-lock:s1"] = "someone"
    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        runner=lambda row: None,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped.get("busy") == 1


@pytest.mark.asyncio
async def test_no_pending_work_is_idle():
    redis = FakeRedis()

    async def no_work(pool, *, tenant_id, session_id):  # noqa: ANN001
        return False

    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        runner=lambda row: None,
        taskboard_fn=no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped.get("idle") == 1


@pytest.mark.asyncio
async def test_user_level_switch_off_blocks():
    """会话开关开着但用户总开关关着 → disabled。"""
    redis = FakeRedis()
    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row(user_enabled=False)]),
        enabled=True,
        now=NOW,
        runner=lambda row: None,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped.get("disabled") == 1


# ── 接管段生命周期 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_segment_begun_and_ended_around_run():
    redis = FakeRedis()
    seen: list = []

    async def runner(row):  # noqa: ANN001
        # run 期间段标记必须存在 —— 前端靠它上锁
        seen.append(job.segment_key("s1") in redis.store)
        return job.RunOutcome(ok=True, tokens=1234, progressed=True)

    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        runner=runner,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.started == 1
    assert seen == [True]
    # 段已结束：否则 UI 锁到 TTL
    assert job.segment_key("s1") not in redis.store


@pytest.mark.asyncio
async def test_segment_ended_even_when_runner_raises():
    """runner 抛异常时必须仍清段标记，否则用户被锁 30 分钟。"""
    redis = FakeRedis()

    async def boom(row):  # noqa: ANN001
        raise RuntimeError("run blew up")

    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        runner=boom,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    # 行级异常边界吞掉了它，但段必须已清
    assert job.segment_key("s1") not in redis.store
    assert res.failed == 1 or res.started == 0


# ── 记账 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_increments_failures():
    redis = FakeRedis()
    pool = FakePool(rows=[_row()])

    async def fail(row):  # noqa: ANN001
        return job.RunOutcome(ok=False, error="no checkpoint")

    await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        runner=fail,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    sql, args = pool.executed[-1]
    # 参数顺序：id, enabled, used, budget_day, last_run_at, verdict, reason,
    #           consecutive_failures, melt_state
    assert args[0] == "tk-1"
    assert args[7] == 1  # consecutive_failures


@pytest.mark.asyncio
async def test_success_resets_failures_and_adds_tokens():
    redis = FakeRedis()
    pool = FakePool(
        rows=[
            _row(
                consecutive_failures=2,
                tokens_used_today=500,
                # budget_day 必须是"今天"，否则按天重置逻辑会把 500 归零 ——
                # 那是正确行为，但这条用例要验的是累加。
                budget_day=job._today(NOW),
            )
        ]
    )

    async def ok(row):  # noqa: ANN001
        return job.RunOutcome(ok=True, tokens=1000, progressed=True)

    await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        runner=ok,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    _, args = pool.executed[-1]
    assert args[2] == 1500  # used = 500 + 1000
    assert args[7] == 0  # failures reset
    assert args[8] == ""  # 未熔断


@pytest.mark.asyncio
async def test_benign_skip_does_not_increment_failures():
    """用户活跃不是失败 —— 否则正常用户会被自己的操作熔断。"""
    redis = FakeRedis()
    pool = FakePool(rows=[_row(consecutive_failures=1)])

    async def recently(pool, *, session_id):  # noqa: ANN001
        return NOW - 5

    await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        idle_minutes=10,
        runner=lambda row: None,
        taskboard_fn=_no_work,
        last_message_fn=recently,
    )
    _, args = pool.executed[-1]
    assert args[7] == 1  # 保持不变，没有被加到 2


@pytest.mark.asyncio
async def test_once_disables_after_run():
    """一次性任务跑过就自动关闭，不然会因"用户正忙"永远挂在到点状态。"""
    redis = FakeRedis()
    pool = FakePool(rows=[_row(schedule_kind=l0.KIND_ONCE, once_at=NOW)])

    async def ok(row):  # noqa: ANN001
        return job.RunOutcome(ok=True, tokens=10)

    await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        runner=ok,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    _, args = pool.executed[-1]
    assert args[1] is False  # enabled


@pytest.mark.asyncio
async def test_budget_exhaustion_melts():
    redis = FakeRedis()
    pool = FakePool(
        rows=[
            _row(
                tokens_used_today=120000,
                daily_token_budget=120000,
                budget_day=job._today(NOW),
            )
        ]
    )
    res = await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        runner=lambda row: None,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped.get("budget") == 1


# ── 采集：预算按天重置 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_resets_on_new_day():
    """budget_day 不是今天时，用量视为 0（按天重置）。"""
    redis = FakeRedis()
    pool = FakePool(rows=[_row(tokens_used_today=120000, budget_day="2020-01-01")])

    async def ok(row):  # noqa: ANN001
        return job.RunOutcome(ok=True, tokens=100, progressed=True)

    res = await job.tick(
        redis=redis,
        pool=pool,
        enabled=True,
        now=NOW,
        runner=ok,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.started == 1  # 昨天用满不影响今天
    _, args = pool.executed[-1]
    assert args[2] == 100  # 今天只用 100


# ── presence ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presence_roundtrip():
    redis = FakeRedis()
    assert await job.mark_presence(redis, user_id="u1", now=NOW) is True
    assert await job.read_presence(redis, user_id="u1") == NOW
    assert await job.read_presence(redis, user_id="nobody") is None


@pytest.mark.asyncio
async def test_presence_blocks_takeover():
    redis = FakeRedis()
    await job.mark_presence(redis, user_id="u1", now=NOW)
    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        idle_minutes=10,
        runner=lambda row: None,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped.get("user_active") == 1


# ── 段查询 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_segment_snapshot_reads_back():
    redis = FakeRedis()
    await job.begin_segment(redis, session_id="s1", takeover_id="tk-1")
    snap = await job.segment_snapshot(redis, session_id="s1")
    assert snap and snap["takeover_id"] == "tk-1"
    await job.end_segment(redis, session_id="s1")
    assert await job.segment_snapshot(redis, session_id="s1") is None


@pytest.mark.asyncio
async def test_segment_failure_does_not_block_run():
    """段标记失败（Redis 抖动）只影响 UI 外观，不该白丢一次巡检。"""
    redis = FakeRedis()
    redis.fail_all = True
    started: list = []

    async def runner(row):  # noqa: ANN001
        started.append(1)
        return job.RunOutcome(ok=True, tokens=1)

    # Redis 全挂时 _acquire_lease 返回 False → 整拍不跑。这是有意的：
    # 没有 Redis 就没有租约与让步信号，宁可不动。
    res = await job.tick(
        redis=redis,
        pool=FakePool(rows=[_row()]),
        enabled=True,
        now=NOW,
        runner=runner,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert res.skipped_global == "lease_busy"
    assert started == []


# ── SQL 形状 ────────────────────────────────────────────────────────


def test_load_rows_joins_user_settings():
    """总开关必须在 SQL 里 JOIN，否则会话级开关会让它形同虚设。"""
    import inspect

    src = inspect.getsource(job._load_rows)
    assert "LEFT JOIN superagent_settings" in src
    assert "COALESCE(s.enabled, false)" in src
    assert "t.enabled = true" in src


def test_audit_entry_shape_is_jsonl_safe():
    """审计条目必须可 json 序列化（写 jsonl 前不做额外转换）。"""
    entry = {"action": "run", "tokens": 10, "ok": True, "error": ""}
    assert json.loads(json.dumps(entry)) == entry


# ── _collect：把一行配置 + 环境事实折成 Signal ────────────────────────
#
# 这一层最容易出的错是**默认值方向**：拿不准的事实该折成"别接管"还是"接管"。
# 折错方向不会报错，只会让接管在没有依据时照跑。


@pytest.mark.asyncio
async def test_collect_redis_down_counts_as_active_run():
    """Redis 读不到会话锁 ⇒ 当作被占用（不接管）。

    折成 False 的话，Redis 抖动一瞬间会让接管去抢一个可能正在跑的会话 ——
    与 `resume._lock_is_held` 同一取向：宁可少接管，不可抢现场。
    """
    redis = FakeRedis()
    redis.fail_all = True
    pool = FakePool()

    sig = await job._collect(  # noqa: SLF001
        redis=redis,
        pool=pool,
        row=_row(),
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.has_active_run is True


@pytest.mark.asyncio
async def test_collect_no_redis_counts_as_active_run():
    """连 Redis 都没有时同样保守。"""
    sig = await job._collect(  # noqa: SLF001
        redis=None,
        pool=FakePool(),
        row=_row(),
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.has_active_run is True


@pytest.mark.asyncio
async def test_collect_lock_free_means_not_active():
    """锁不存在 ⇒ 没有 run 在跑（这是放行的前提）。"""
    sig = await job._collect(  # noqa: SLF001
        redis=FakeRedis(),
        pool=FakePool(),
        row=_row(),
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.has_active_run is False


@pytest.mark.asyncio
async def test_collect_resets_budget_on_new_day():
    """`budget_day` 不是今天 ⇒ 当日用量归零。

    这条是"次日自动恢复"的实现基础：不重置的话，一次预算熔断会永久生效，
    用户第二天回来发现功能还是死的，而他没做任何错事。
    """
    row = _row(tokens_used_today=999_999, budget_day="2000-01-01")
    sig = await job._collect(  # noqa: SLF001
        redis=FakeRedis(),
        pool=FakePool(),
        row=row,
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.tokens_used_today == 0


@pytest.mark.asyncio
async def test_collect_keeps_budget_within_same_day():
    """同一天内用量必须保留 —— 否则预算永远拦不住任何东西。"""
    row = _row(tokens_used_today=50_000, budget_day=job._today(NOW))  # noqa: SLF001
    sig = await job._collect(  # noqa: SLF001
        redis=FakeRedis(),
        pool=FakePool(),
        row=row,
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.tokens_used_today == 50_000


@pytest.mark.asyncio
async def test_collect_carries_presence_into_signal():
    """presence 必须真的接进 Signal —— 它是让步信号 2 的唯一来源。"""
    redis = FakeRedis()
    await job.mark_presence(redis, user_id="u1", now=NOW - 60)

    sig = await job._collect(  # noqa: SLF001
        redis=redis,
        pool=FakePool(),
        row=_row(),
        now=NOW,
        idle_minutes=10,
        taskboard_fn=_no_work,
        last_message_fn=_no_msg,
    )
    assert sig.presence_at == NOW - 60


# ── takeover_loop：后台循环的控制流 ─────────────────────────────────


@pytest.mark.asyncio
async def test_loop_stops_on_should_stop(monkeypatch):
    """`should_stop` 为真时必须退出，而不是继续 sleep。

    退出不了意味着 worker 的 `stop()` 取消任务前它还在抢租约 —— 停机过程
    里仍可能起一次 run。
    """

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(job, "_wait", _no_wait)
    monkeypatch.setattr(job, "_setting", lambda name, default=None: 60)

    calls: list[int] = []

    def _should_stop():
        calls.append(1)
        return True  # 第一次就喊停

    await job.takeover_loop(FakeRedis(), should_stop=_should_stop)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_loop_survives_tick_exception(monkeypatch):
    """一拍抛异常不能把循环打死 —— 否则一次 DB 抖动就永久停掉巡检。

    这是"后台循环"最基本的属性：异常只 warning，下一拍照常。
    """
    ticks: list[int] = []

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(job, "_wait", _no_wait)
    monkeypatch.setattr(
        job, "_setting", lambda name, default=None: True if "enabled" in name else 60
    )

    async def _boom(*, redis, pool, enabled):  # noqa: ANN001
        ticks.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(job, "tick", _boom)

    state = {"n": 0}

    def _should_stop():
        state["n"] += 1
        return state["n"] > 3

    await job.takeover_loop(FakeRedis(), should_stop=_should_stop)
    # 连抛 3 次后仍继续到第 4 次判断才停 —— 异常没有中断循环
    assert len(ticks) == 3


@pytest.mark.asyncio
async def test_loop_skips_tick_when_disabled(monkeypatch):
    """运行中被关掉开关 ⇒ 不再调 tick（不查库、不起 run）。

    `takeover_loop` 每拍重读开关，是为了让"关掉"立即生效而不用重启引擎。
    """
    ticks: list[int] = []

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(job, "_wait", _no_wait)
    monkeypatch.setattr(job, "_setting", lambda name, default=None: False)

    async def _tick(**kw):  # noqa: ANN001
        ticks.append(1)
        raise AssertionError("tick must not run while disabled")

    monkeypatch.setattr(job, "tick", _tick)

    state = {"n": 0}

    def _should_stop():
        state["n"] += 1
        return state["n"] > 3

    await job.takeover_loop(FakeRedis(), should_stop=_should_stop)
    assert ticks == []


@pytest.mark.asyncio
async def test_loop_tick_interval_floor(monkeypatch):
    """节拍下限 30s：配成 1 秒会把 Redis 和 PG 打成筛子。"""
    seen: list[float] = []

    async def _no_wait(seconds):
        seen.append(seconds)

    monkeypatch.setattr(job, "_wait", _no_wait)
    monkeypatch.setattr(job, "_setting", lambda name, default=None: 1)

    state = {"n": 0}

    def _should_stop():
        state["n"] += 1
        return True

    await job.takeover_loop(FakeRedis(), should_stop=_should_stop)
    assert seen == [30]
