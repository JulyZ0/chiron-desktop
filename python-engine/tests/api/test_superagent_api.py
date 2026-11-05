"""`/v1/superagent/*` 运行时端点测试。

**为什么直接调端点函数而不走 ASGI**

`tests/conftest.py` 有 autouse fixture 往每个请求注入 `X-Internal-Token`，于是走
`AsyncClient` 时"缺凭据"这条分支永远到不了 handler —— 那测的是中间件，不是端点
是否 fail-closed。鉴权判定已由 `test_isolation_api.py` 覆盖（同一个
`_verify_gateway_key`），这里只管端点的**业务语义**。

**要钉住的三件事**

1. `segment` 必须区分"确认没有接管"与"查不到" —— 否则一次 Redis 抖动就会让
   覆盖层在 run 改文件的中途消失。
2. `interrupt` 必须**双拍齐发**：停 run + 写冷却。只有其一时前端会说"打断了"
   但几十秒后又起来，或者 run 继续在跑。
3. `once` 模式的打断落库形式是**关开关**，不是写一个 `+inf` 时间戳（后者写不进
   `timestamp` 列）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.agent import takeover as l0
from app.api import superagent as sa
from app.config import settings


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def set(self, key, value, *, nx=False, ex=None):  # noqa: ANN001
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):  # noqa: ANN001
        return self.store.get(key)

    async def delete(self, key):  # noqa: ANN001
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1


class FakePool:
    """`fetchrow` 返回预置行，`execute` 只记录。"""

    def __init__(self, row=None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):  # noqa: ANN001
        return self.row

    async def execute(self, sql, *args):  # noqa: ANN001
        self.executed.append((sql, args))
        return "UPDATE 1"


class _Req:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}
        self.client = None


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造一份能过 `_verify_gateway_key` 的头；两凭据都没配则跳过。"""
    token = settings.internal_token
    if not token:
        pytest.skip("本环境未配置 internal_token")
    headers = {"X-Internal-Token": token}
    headers.update(extra or {})
    return headers


@pytest.fixture
def wired(monkeypatch):
    """把端点的两个 IO 依赖换成桩，并返回它们供断言。"""
    redis = FakeRedis()
    pool = FakePool()

    async def _redis():
        return redis

    async def _pool():
        return pool

    monkeypatch.setattr(sa, "_redis", _redis)
    monkeypatch.setattr(sa, "_pool", _pool)
    # 审计落盘指到空操作：这些用例不该在仓库里留 logs/takeover_audit.jsonl
    monkeypatch.setattr(sa.job, "_audit", lambda _entry: None)
    return redis, pool


# ── presence ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presence_requires_user_id(wired):
    with pytest.raises(HTTPException) as exc:
        await sa.post_presence(_Req(_auth_headers()), None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_presence_written_from_gateway_header(wired):
    """身份取网关注入的头 —— 请求体里的 user_id 不作为跨用户操作的依据。"""
    redis, _ = wired
    res = await sa.post_presence(
        _Req(_auth_headers({"X-User-Id": "u-from-gateway"})), None
    )
    assert res["ok"] is True
    assert res["user_id"] == "u-from-gateway"
    assert sa.job.presence_key("u-from-gateway") in redis.store


@pytest.mark.asyncio
async def test_presence_header_beats_body(wired):
    redis, _ = wired
    res = await sa.post_presence(
        _Req(_auth_headers({"X-User-Id": "from-header"})),
        sa.PresenceBody(user_id="from-body"),
    )
    assert res["user_id"] == "from-header"
    assert sa.job.presence_key("from-body") not in redis.store


@pytest.mark.asyncio
async def test_presence_degrades_without_redis(monkeypatch):
    """Redis 不可用返回 200 + ok:false，而不是 5xx。

    心跳失败不该让前端弹错误框 —— 判定侧在"读不到 presence"时本来就保守。
    """

    async def _none():
        return None

    monkeypatch.setattr(sa, "_redis", _none)
    res = await sa.post_presence(_Req(_auth_headers({"X-User-Id": "u1"})), None)
    assert res["ok"] is False
    assert res["ttl_seconds"] == 0


# ── segment ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_segment_absent_is_known_and_inactive(wired):
    res = await sa.get_segment(_Req(_auth_headers()), "s1")
    assert res["active"] is False
    # Redis 可用 ⇒ 这是"确认没有"，不是"查不到"
    assert res["known"] is True


@pytest.mark.asyncio
async def test_segment_present_reports_takeover_id(wired):
    redis, _ = wired
    await sa.job.begin_segment(redis, session_id="s1", takeover_id="tk-9")
    res = await sa.get_segment(_Req(_auth_headers()), "s1")
    assert res["active"] is True
    assert res["takeover_id"] == "tk-9"


@pytest.mark.asyncio
async def test_segment_without_redis_is_unknown(monkeypatch):
    """Redis 挂了必须报 `known: false`。

    若也说 `active: false`，覆盖层会在接管进行中消失 —— 用户正好在 run
    改文件的时刻开始输入。前端据 `known` 决定"保持现状"而不是"解锁"。
    """

    async def _none():
        return None

    monkeypatch.setattr(sa, "_redis", _none)
    res = await sa.get_segment(_Req(_auth_headers()), "s1")
    assert res["active"] is False
    assert res["known"] is False


# ── interrupt ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_requires_session_id(wired):
    with pytest.raises(HTTPException) as exc:
        await sa.post_interrupt(_Req(_auth_headers()), sa.InterruptBody(session_id="  "))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_interrupt_clears_segment_and_sets_cooldown(wired, monkeypatch):
    """双拍齐发：停 run + 写冷却。缺任一半都会出现"打断后又起来"。"""
    redis, pool = wired
    await sa.job.begin_segment(redis, session_id="s1", takeover_id="tk-1")
    pool.row = {
        "id": "tk-1",
        "schedule_kind": l0.KIND_INTERVAL,
        "interval_seconds": 1800,
        "enabled": True,
    }

    stopped: list[str] = []

    async def _fake_interrupt(session_id, *, redis=None):  # noqa: ANN001
        stopped.append(session_id)
        return True

    import app.agent.interrupt as interrupt_mod

    monkeypatch.setattr(interrupt_mod, "request_interrupt", _fake_interrupt)

    res = await sa.post_interrupt(_Req(_auth_headers()), sa.InterruptBody(session_id="s1"))

    assert stopped == ["s1"]  # 第一拍：run 被叫停
    assert sa.job.segment_key("s1") not in redis.store  # 覆盖层立刻消失
    assert res["cooldown_until"] is not None  # 第二拍：下一拍不再启动
    assert res["disabled"] is False
    assert pool.executed  # 冷却已落库


@pytest.mark.asyncio
async def test_interrupt_once_disables_instead_of_inf_timestamp(wired, monkeypatch):
    """`once` 模式打断 ⇒ 关闭开关。

    `apply_interrupt` 对 once 返回 `+inf`，而 `+inf` 写不进 timestamp 列。
    落库形式必须是 `enabled=false`，否则要么报错要么被静默截断成某个巨大日期。
    """
    redis, pool = wired
    pool.row = {
        "id": "tk-1",
        "schedule_kind": l0.KIND_ONCE,
        "interval_seconds": 0,
        "enabled": True,
    }

    import app.agent.interrupt as interrupt_mod

    async def _noop(session_id, *, redis=None):  # noqa: ANN001
        return True

    monkeypatch.setattr(interrupt_mod, "request_interrupt", _noop)

    res = await sa.post_interrupt(_Req(_auth_headers()), sa.InterruptBody(session_id="s1"))

    assert res["disabled"] is True
    assert res["cooldown_until"] is None
    sql, args = pool.executed[-1]
    assert "enabled = false" in sql


@pytest.mark.asyncio
async def test_interrupt_survives_db_failure(wired, monkeypatch):
    """DB 不可用时仍要停 run —— 让用户拿回控制权优先于记账完整。"""
    redis, _ = wired

    async def _no_pool():
        return None

    monkeypatch.setattr(sa, "_pool", _no_pool)

    stopped: list[str] = []

    import app.agent.interrupt as interrupt_mod

    async def _fake(session_id, *, redis=None):  # noqa: ANN001
        stopped.append(session_id)
        return True

    monkeypatch.setattr(interrupt_mod, "request_interrupt", _fake)

    res = await sa.post_interrupt(_Req(_auth_headers()), sa.InterruptBody(session_id="s1"))

    assert stopped == ["s1"]
    assert res["ok"] is True
    # 无法确定冷却 ⇒ 如实返回 None，前端可提示"定时未取消"
    assert res["cooldown_until"] is None
    assert sa.job.segment_key("s1") not in redis.store


# ── status ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_reports_engine_switch(wired, monkeypatch):
    monkeypatch.setattr(settings, "superagent_takeover_enabled", False)
    res = await sa.get_status(_Req(_auth_headers()))
    assert res["engine_enabled"] is False
    assert res["max_sessions"] == l0.MAX_TAKEOVER_SESSIONS


@pytest.mark.asyncio
async def test_status_exposes_unattended_tools_mode(wired):
    """前端要能把"yolo 不被继承"显示出来，否则用户以为接管会自动执行。"""
    res = await sa.get_status(_Req(_auth_headers()))
    assert res["unattended_tools_mode"] == "auto"
    assert res["unsafe_tools_mode"] == "yolo"


@pytest.mark.asyncio
async def test_status_tick_floor(wired, monkeypatch):
    """节拍下限 30s：配成 1 秒会把 Redis 和 PG 打成筛子。"""
    monkeypatch.setattr(settings, "superagent_takeover_tick_seconds", 1)
    res = await sa.get_status(_Req(_auth_headers()))
    assert res["tick_seconds"] == 30
