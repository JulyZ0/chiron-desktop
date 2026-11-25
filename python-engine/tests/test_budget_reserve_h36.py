"""H36：预算预占的并发与结算语义（fakeredis 真执行 Lua）。"""

from __future__ import annotations

import asyncio

import pytest

from app.gateway import budget_reserve as br
from app.gateway.budget import TokenBudget

pytest.importorskip("fakeredis")


@pytest.fixture()
def redis():  # type: ignore[no-untyped-def]
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_concurrent_reservations_cannot_overshoot(redis) -> None:  # type: ignore[no-untyped-def]
    """limit=1000、每个请求预占 300：最多 3 个能拿到，第 4 个必须被拒。

    这正是旧 `check()` 挡不住的场景：4 个请求都读到 used=0，"都有额度"。
    """

    async def scenario() -> None:
        budget = TokenBudget(redis)
        await budget.set_limit(7, 1000)
        got = await asyncio.gather(*[br.reserve(budget, 7, 300) for _ in range(6)])
        granted = [r for r in got if r is not None]
        assert len(granted) == 3, f"应只放行 3 个，实际 {len(granted)}"
        assert await br.reserved_total(budget, 7) == 900

    _run(scenario())


def test_release_returns_the_reservation(redis) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        budget = TokenBudget(redis)
        await budget.set_limit(7, 500)
        first = await br.reserve(budget, 7, 400)
        assert first is not None
        assert await br.reserve(budget, 7, 400) is None, "额度已被占满"

        await br.release(budget, first)
        assert await br.reserved_total(budget, 7) == 0
        again = await br.reserve(budget, 7, 400)
        assert again is not None, "释放后应能再次预占（上游故障不该永久吃掉额度）"

    _run(scenario())


def test_commit_settles_actual_usage(redis) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        budget = TokenBudget(redis)
        await budget.set_limit(7, 1000)
        res = await br.reserve(budget, 7, 600)
        assert res is not None
        await br.commit(budget, res, 250)
        assert await br.reserved_total(budget, 7) == 0, "提交后占位必须清零"
        used, limit = await budget.get_usage(7)
        assert (used, limit) == (250, 1000), "used 应按**实际**用量结算"

    _run(scenario())


def test_commit_larger_than_estimate_still_recorded(redis) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        budget = TokenBudget(redis)
        await budget.set_limit(7, 1000)
        res = await br.reserve(budget, 7, 100)
        assert res is not None
        await br.commit(budget, res, 400)
        used, _ = await budget.get_usage(7)
        assert used == 400

    _run(scenario())


def test_no_limit_means_unlimited_reservation(redis) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        budget = TokenBudget(redis)
        # limit=0（或整个 hash 不存在）⇒ 不限制
        await budget.set_limit(7, 0)
        res = await br.reserve(budget, 7, 10**9)
        assert res is not None and res.unlimited is True
        await br.commit(budget, res, 123)
        used, _ = await budget.get_usage(7)
        assert used == 123, "不限制也要记账"

    _run(scenario())


def test_reserve_without_tenant_is_noop(redis) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        budget = TokenBudget(redis)
        res = await br.reserve(budget, 0, 500)
        assert res is not None and res.unlimited is True
        await br.release(budget, res)  # 不应报错

    _run(scenario())


def test_403_quota_is_not_treated_as_key_failure() -> None:
    """403 要先区分"额度用尽"与"这把 key 失效"：前者换 key 只会白烧往返。"""
    from app.gateway.errors import is_quota_exhausted
    from app.gateway.key_retry import is_retryable_key_error

    class _Quota(Exception):
        status_code = 403

        def __str__(self) -> str:
            return "403 insufficient_quota: You exceeded your current quota"

    class _Forbidden(Exception):
        status_code = 403

        def __str__(self) -> str:
            return "403 invalid api key"

    assert is_quota_exhausted(_Quota()) is True
    assert is_retryable_key_error(_Quota()) is False, "额度耗尽换 key 无意义"
    assert is_retryable_key_error(_Forbidden()) is True, "凭证失效应换 key"
