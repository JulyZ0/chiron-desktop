"""G-L3：prompt cache 空转**埋点**的回归测试。

**这里钉住什么**

`app/agent/cache_warm.py` 只做一件事：把"这次空转等了多久 + 上次响应里缓存读写了
多少 token"归一成一条可上报的记录。它**不发任何请求** —— 真发保活要按部署口径算
"保活费 vs 重算费"，那是产品决策；在决策之前先把这个决策需要的数据采出来。

三条约束一旦回退，"先埋点后决策"这件事就失真：

1. **`would_warm` 是判定，不是动作** —— 它只在"缓存里真有东西"且"已经过期"时成立。
   对一件从来没进过缓存的前缀保活是无意义的额外开销；反过来，条件成立也不代表
   这里会发请求（模块内没有网络路径，见最后一条测试）。
2. **纯函数、可直接单测** —— `classify_idle_gap` 不读时钟、不发请求，因此边界
   （负间隔、TTL 恰好相等）可以逐点断言，而不是靠 sleep。
3. **噪声闸** —— 亚秒级间隔不上报。缺了它，"真的空转了五分钟"会被同一次交互内的
   抖动淹掉，采集到的分布反而更没用。
"""

from __future__ import annotations

import inspect

from app.agent import cache_warm
from app.agent.cache_warm import (
    CACHE_TTL_SECONDS,
    IDLE_APPROVAL,
    IDLE_STEER,
    IDLE_SUBAGENT_REPORT,
    IDLE_TURN,
    MIN_REPORTABLE_IDLE_SECONDS,
    CacheUsage,
    IdleGap,
    classify_idle_gap,
)

# ── CacheUsage ────────────────────────────────────────────


def test_usage_total_sums_both_sides():
    """判断保活值不值要看读 + 写两侧：只算命中省下的，算不出"重新写入要花的"。"""
    usage = CacheUsage(read_tokens=900, write_tokens=100)
    assert usage.total == 1000
    assert usage.hit_rate == 0.9


def test_usage_hit_rate_is_zero_without_activity():
    """没有缓存活动时返回 0，而不是除零或 1（1 会把"没缓存"读成"全命中"）。"""
    assert CacheUsage().total == 0
    assert CacheUsage().hit_rate == 0.0


# ── 过期判定 ──────────────────────────────────────────────


def test_expired_is_inclusive_at_ttl():
    """恰好等于 TTL 就算过期：保守取值是故意的，埋点用途是**估上界**。"""
    usage = CacheUsage(read_tokens=1)
    assert not classify_idle_gap(CACHE_TTL_SECONDS - 0.001, usage).expired
    assert classify_idle_gap(CACHE_TTL_SECONDS, usage).expired
    assert classify_idle_gap(CACHE_TTL_SECONDS * 10, usage).expired


# ── would_warm：两个条件缺一不可 ──────────────────────────


def test_would_warm_requires_expiry_and_prior_cache_activity():
    expired = CACHE_TTL_SECONDS + 1
    assert classify_idle_gap(expired, CacheUsage(read_tokens=10)).would_warm
    assert classify_idle_gap(expired, CacheUsage(write_tokens=10)).would_warm
    # 过期但从没进过缓存 ⇒ 保活是无意义的额外开销。
    assert not classify_idle_gap(expired, CacheUsage()).would_warm
    # 有缓存但没过期 ⇒ 不需要保活。
    assert not classify_idle_gap(0.5, CacheUsage(read_tokens=10)).would_warm


# ── 噪声闸 ────────────────────────────────────────────────


def test_worth_reporting_drops_subsecond_noise():
    usage = CacheUsage(read_tokens=1)
    assert not classify_idle_gap(0.0, usage).worth_reporting
    assert not classify_idle_gap(MIN_REPORTABLE_IDLE_SECONDS - 0.001, usage).worth_reporting
    assert classify_idle_gap(MIN_REPORTABLE_IDLE_SECONDS, usage).worth_reporting


# ── 纯函数边界 ────────────────────────────────────────────


def test_negative_seconds_are_clamped():
    """负间隔只可能来自时钟回拨，夹到 0 而不是让 `expired` 拿到负数。"""
    gap = classify_idle_gap(-5.0, CacheUsage(read_tokens=1))
    assert gap.seconds == 0.0
    assert not gap.expired


def test_reason_defaults_and_passes_through():
    usage = CacheUsage(read_tokens=1)
    assert classify_idle_gap(2.0, usage).reason == IDLE_TURN
    for reason in (IDLE_APPROVAL, IDLE_STEER, IDLE_SUBAGENT_REPORT):
        assert classify_idle_gap(2.0, usage, reason=reason).reason == reason


def test_custom_ttl_is_honoured():
    gap = classify_idle_gap(50.0, CacheUsage(read_tokens=1), ttl_seconds=10.0)
    assert gap.ttl_seconds == 10.0
    assert gap.expired


# ── span 元数据 ───────────────────────────────────────────


def test_metadata_carries_both_cache_sides():
    """字段名对齐 `llm_call` span，便于在同一张图里对照。"""
    gap = classify_idle_gap(
        CACHE_TTL_SECONDS + 30,
        CacheUsage(read_tokens=800, write_tokens=200),
        reason=IDLE_APPROVAL,
    )
    meta = gap.as_metadata(model="claude-x")
    assert meta["reason"] == IDLE_APPROVAL
    assert meta["idle_seconds"] == 330.0
    assert meta["expired"] is True
    assert meta["would_warm"] is True
    assert meta["cache_read_tokens"] == 800
    assert meta["cache_write_tokens"] == 200
    assert meta["cache_hit_rate"] == 0.8
    assert meta["model"] == "claude-x"


# ── 硬约束：模块内没有发请求的路径 ────────────────────────


def test_module_has_no_network_imports():
    """埋点阶段最该守的一条：这个模块**不可能**发出保活请求。

    靠"没人调用发送函数"来保证太弱 —— 一次误改就会让埋点变成花钱的路径。这里直接
    对模块源码断言没有网络相关导入，回退时立刻失败并指向原因。
    """
    source = inspect.getsource(cache_warm)
    for forbidden in ("httpx", "aiohttp", "requests", "urllib", "socket"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def test_idle_gap_carries_no_request_payload():
    """测量结果里不该出现可发送的请求体字段。"""
    gap = classify_idle_gap(2.0, CacheUsage(read_tokens=1))
    assert isinstance(gap, IdleGap)
    assert set(gap.__dataclass_fields__) == {
        "seconds",
        "reason",
        "usage",
        "ttl_seconds",
    }
