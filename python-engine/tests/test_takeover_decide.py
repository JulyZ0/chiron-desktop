"""L0 接管判定的单元测试。

**为什么这些用例值得写**

`takeover.decide` 决定了"要不要花一次完整 run 的钱"。它有两种代价不对称的错误：

* 该跳过时放行 → 无人值守下动了用户的工作区，还烧掉预算；
* 该放行时跳过 → 接管功能形同虚设（用户以为它在跑，其实每拍都被拦）。

所以这里既钉"必须拦住"的每一条理由，也钉"真的会放行"的正例 —— 只测拦截的测试
会在判定被写成"永远 return False"时全绿。

路由顺序（开关 → 熔断 → 冷却 → 到点 → 预算 → 让步 → 有活）也单独断言：
它决定了多条同时成立时报出哪个原因，而原因会进审计并显示给用户。
"""

from __future__ import annotations

import pytest

from app.agent import takeover as t

NOW = 1_800_000_000.0


def _signal(**kw) -> t.Signal:
    """一个"除让步外全部满足"的基线信号，便于逐项推翻。"""
    base = dict(
        now=NOW,
        user_enabled=True,
        session_enabled=True,
        schedule_kind=t.KIND_INTERVAL,
        interval_seconds=1800,
        last_run_at=NOW - 2000,  # 已过 1800s 间隔
        cooldown_until=None,
        melt_state="",
        consecutive_failures=0,
        tokens_used_today=1000,
        daily_token_budget=120000,
        has_active_run=False,
        presence_at=None,
        last_message_at=NOW - 3600,  # 一小时前，远超 idle 窗口
        idle_minutes=10,
        has_pending_work=True,
    )
    base.update(kw)
    return t.Signal(**base)


# ── 正例：先证明"真的会放行"，否则拦截测试毫无意义 ─────────────────────


def test_baseline_proceeds():
    d = t.decide(_signal())
    assert d.run is True
    assert d.reason is None
    assert d.benign is True


# ── 开关 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kw, detail_part",
    [
        ({"user_enabled": False}, "user-level"),
        ({"session_enabled": False}, "session-level"),
    ],
)
def test_disabled_short_circuits(kw, detail_part):
    d = t.decide(_signal(**kw))
    assert d.run is False
    assert d.reason is t.SkipReason.DISABLED
    assert detail_part in d.detail


def test_switch_beats_melt():
    """用户把开关关掉时，报"关闭"而不是"熔断" —— 否则用户会以为自己要先恢复熔断。"""
    d = t.decide(_signal(session_enabled=False, melt_state=t.MELT_ERRORS))
    assert d.reason is t.SkipReason.DISABLED


# ── 熔断 ────────────────────────────────────────────────────────────


def test_melt_state_blocks():
    d = t.decide(_signal(melt_state=t.MELT_BUDGET))
    assert d.run is False
    assert d.reason is t.SkipReason.MELTED
    assert "manual reset" in d.detail


def test_consecutive_failures_reach_threshold():
    d = t.decide(_signal(consecutive_failures=t.MELT_AFTER_FAILURES))
    assert d.reason is t.SkipReason.MELTED


def test_consecutive_failures_below_threshold_still_runs():
    """差一次就熔断时必须还愿意跑 —— 阈值前就停等于把阈值降到 0。"""
    assert t.decide(_signal(consecutive_failures=t.MELT_AFTER_FAILURES - 1)).run is True


# ── 冷却 ────────────────────────────────────────────────────────────


def test_cooldown_blocks_until_deadline():
    d = t.decide(_signal(cooldown_until=NOW + 60))
    assert d.run is False
    assert d.reason is t.SkipReason.COOLDOWN
    assert d.due_at == NOW + 60


def test_cooldown_expired_no_longer_blocks():
    assert t.decide(_signal(cooldown_until=NOW - 1)).run is True


# ── 到点 ────────────────────────────────────────────────────────────


def test_interval_not_due_yet():
    d = t.decide(_signal(last_run_at=NOW - 100))
    assert d.run is False
    assert d.reason is t.SkipReason.NOT_DUE
    assert d.due_at == NOW - 100 + 1800


def test_interval_never_run_is_due_immediately():
    """刚打开开关时用户期望"马上试一次"，而不是干等一个间隔。"""
    d = t.decide(_signal(last_run_at=None))
    assert d.run is True


def test_interval_below_floor_is_invalid():
    d = t.decide(_signal(interval_seconds=t.MIN_INTERVAL_SECONDS - 1))
    assert d.run is False
    assert d.reason is t.SkipReason.NOT_DUE
    assert "exhausted or invalid" in d.detail


def test_once_before_deadline_waits():
    d = t.decide(_signal(schedule_kind=t.KIND_ONCE, once_at=NOW + 600))
    assert d.run is False
    assert d.reason is t.SkipReason.NOT_DUE
    assert d.due_at == NOW + 600


def test_once_is_due_at_deadline():
    assert t.decide(_signal(schedule_kind=t.KIND_ONCE, once_at=NOW)).run is True


def test_once_fires_only_once():
    """`last_run_at >= once_at` 即视为已执行 —— 一次性任务不能变成周期性任务。"""
    d = t.decide(_signal(schedule_kind=t.KIND_ONCE, once_at=NOW - 60, last_run_at=NOW - 60))
    assert d.run is False
    assert d.reason is t.SkipReason.NOT_DUE


def test_unknown_schedule_kind_never_runs():
    assert t.decide(_signal(schedule_kind="cron")).run is False


# ── 预算 ────────────────────────────────────────────────────────────


def test_budget_exhausted_blocks():
    d = t.decide(_signal(tokens_used_today=120000, daily_token_budget=120000))
    assert d.run is False
    assert d.reason is t.SkipReason.BUDGET


def test_budget_zero_blocks():
    """预算未配置（0）不是"无限"，而是"没批准花钱" —— fail-closed。"""
    d = t.decide(_signal(daily_token_budget=0))
    assert d.reason is t.SkipReason.BUDGET


def test_budget_beats_user_active():
    """顺序断言：预算在让步之前判定。用户正活跃且预算已空时报"预算"，
    因为"等用户停手"这个理由此刻是假的 —— 用户停手了也跑不了。"""
    d = t.decide(_signal(tokens_used_today=999999, presence_at=NOW - 5))
    assert d.reason is t.SkipReason.BUDGET


# ── 让步：三路取或 ──────────────────────────────────────────────────


def test_active_run_is_busy_not_user_active():
    """锁被占用单独报 BUSY：调用方据此**不弹**询问框（用户正在等结果）。"""
    d = t.decide(_signal(has_active_run=True, presence_at=None, last_message_at=NOW - 7200))
    assert d.run is False
    assert d.reason is t.SkipReason.BUSY


def test_recent_presence_blocks():
    d = t.decide(_signal(presence_at=NOW - 60, last_message_at=NOW - 7200))
    assert d.run is False
    assert d.reason is t.SkipReason.USER_ACTIVE


def test_recent_message_blocks():
    d = t.decide(_signal(presence_at=None, last_message_at=NOW - 60))
    assert d.run is False
    assert d.reason is t.SkipReason.USER_ACTIVE


def test_three_signals_are_ored():
    """任一路为真即活跃；三路都为假才放行。"""
    quiet = dict(presence_at=NOW - 7200, last_message_at=NOW - 7200, has_active_run=False)
    assert t.decide(_signal(**quiet)).run is True
    assert t.decide(_signal(**{**quiet, "has_active_run": True})).reason is t.SkipReason.BUSY
    assert t.decide(_signal(**{**quiet, "presence_at": NOW})).reason is t.SkipReason.USER_ACTIVE


def test_no_signals_at_all_proceeds():
    """三路全空（新会话/采集失败）时按"无人活动"放行，而不是无限期跳过。"""
    d = t.decide(_signal(presence_at=None, last_message_at=None, has_active_run=False))
    assert d.run is True


def test_idle_window_boundary():
    """恰好等于窗口不算活跃（`<` 而非 `<=`），差一秒则算活跃。"""
    assert t.decide(_signal(last_message_at=NOW - 600)).run is True
    assert t.decide(_signal(last_message_at=NOW - 599)).reason is t.SkipReason.USER_ACTIVE


# ── 无事可做 ────────────────────────────────────────────────────────


def test_no_pending_work_is_idle():
    d = t.decide(_signal(has_pending_work=False))
    assert d.run is False
    assert d.reason is t.SkipReason.IDLE


def test_user_active_beats_idle():
    """用户活跃时不必去查"有没有活" —— 顺序上让步先于工作量。"""
    d = t.decide(_signal(has_pending_work=False, presence_at=NOW))
    assert d.reason is t.SkipReason.USER_ACTIVE


# ── 辅助函数 ────────────────────────────────────────────────────────


def test_idle_seconds_takes_latest_signal():
    s = _signal(presence_at=NOW - 500, last_message_at=NOW - 100)
    assert t.idle_seconds(s) == 100


def test_idle_seconds_infinite_without_signals():
    s = _signal(presence_at=None, last_message_at=None, has_active_run=False)
    assert t.idle_seconds(s) == float("inf")


def test_idle_seconds_zero_while_run_active():
    assert t.idle_seconds(_signal(has_active_run=True)) == 0.0


def test_benign_skips_exclude_melt_only():
    """熔断不是"正常跳过"（不该被当作可忽略），其余都是。"""
    for reason in t.BENIGN_SKIPS:
        assert t.Decision(reason=reason).benign is True
    assert t.Decision(reason=t.SkipReason.MELTED).benign is False


# ── 授权档位继承 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ask", "ask"),
        ("auto", "auto"),
        # 核心断言：yolo 不允许被无人值守继承。
        ("yolo", "auto"),
        ("", "auto"),
        (None, "auto"),
        (123, "auto"),
        ("  YOLO  ", "auto"),  # 大小写/空白不同也不放行
    ],
)
def test_safe_tools_mode_never_inherits_yolo(raw, expected):
    assert t.safe_tools_mode(raw) == expected


# ── 熔断判定 ────────────────────────────────────────────────────────


def test_should_melt_errors_take_precedence():
    """错误熔断优先：预算次日会自愈，错误不会 —— 报"错误"才能让用户去修。"""
    assert (
        t.should_melt(consecutive_failures=3, tokens_used_today=999999, daily_token_budget=1)
        == t.MELT_ERRORS
    )


def test_should_melt_budget():
    assert (
        t.should_melt(consecutive_failures=0, tokens_used_today=100, daily_token_budget=100)
        == t.MELT_BUDGET
    )


def test_should_not_melt():
    assert t.should_melt(consecutive_failures=2, tokens_used_today=1, daily_token_budget=100) == ""


# ── 打断语义 ────────────────────────────────────────────────────────


def test_interrupt_cancels_once_permanently():
    """once 被用户打断 = 这次就是全部，返回 +inf 表示不再自动启动。"""
    s = _signal(schedule_kind=t.KIND_ONCE, once_at=NOW)
    assert t.apply_interrupt(s, cancelled_at=NOW) == float("inf")


def test_interrupt_on_interval_waits_one_period():
    """interval 被中断只取消"这一次"，下一个间隔照常评估。"""
    s = _signal(interval_seconds=1800)
    assert t.apply_interrupt(s, cancelled_at=NOW) == NOW + 1800


def test_interrupt_with_invalid_interval_disables():
    s = _signal(interval_seconds=0)
    assert t.apply_interrupt(s, cancelled_at=NOW) == float("inf")


# ── 保存期校验 ──────────────────────────────────────────────────────


def test_validate_interval_ok():
    assert t.validate_schedule(
        schedule_kind=t.KIND_INTERVAL, once_at=None, interval_seconds=1800, now=NOW
    ) == []


def test_validate_interval_too_small():
    errs = t.validate_schedule(
        schedule_kind=t.KIND_INTERVAL, once_at=None, interval_seconds=10, now=NOW
    )
    assert errs and "interval_seconds" in errs[0]


def test_validate_once_requires_future_within_horizon():
    assert t.validate_schedule(
        schedule_kind=t.KIND_ONCE, once_at=None, interval_seconds=0, now=NOW
    )
    assert t.validate_schedule(
        schedule_kind=t.KIND_ONCE, once_at=NOW - 1, interval_seconds=0, now=NOW
    )
    assert t.validate_schedule(
        schedule_kind=t.KIND_ONCE,
        once_at=NOW + t.MAX_ONCE_HORIZON_SECONDS + 1,
        interval_seconds=0,
        now=NOW,
    )
    assert (
        t.validate_schedule(
            schedule_kind=t.KIND_ONCE,
            once_at=NOW + t.MAX_ONCE_HORIZON_SECONDS,
            interval_seconds=0,
            now=NOW,
        )
        == []
    )


def test_validate_unknown_kind():
    assert t.validate_schedule(
        schedule_kind="weekly", once_at=None, interval_seconds=1800, now=NOW
    ) != []


# ── next_due_at：到点时刻的纯算术 ────────────────────────────────────
#
# 直接测它而不只经 `decide` 间接覆盖，是因为它是**审计与 UI 展示**的数据源：
# 前端"下次巡检：14:32"这个时间戳就来自这里。算出 None 却仍显示一个时刻，
# 用户会等一个永远不会到的巡检。


def test_next_due_interval_never_run_is_now():
    """从未跑过 ⇒ 立即到点。用户刚打开开关时期望"马上试一次"。"""
    s = _signal(last_run_at=None)
    assert t.next_due_at(s) == NOW


def test_next_due_interval_adds_interval_to_last_run():
    last = NOW - 600
    s = _signal(last_run_at=last, interval_seconds=1800)
    assert t.next_due_at(s) == last + 1800


def test_next_due_interval_below_floor_is_none():
    """间隔非法时算不出下一拍 —— 返回 None 而不是硬算一个数。

    硬算会让 UI 显示一个"下次巡检"时刻，而引擎每拍都判 NOT_DUE 永不执行。
    """
    s = _signal(last_run_at=NOW - 60, interval_seconds=t.MIN_INTERVAL_SECONDS - 1)
    assert t.next_due_at(s) is None


def test_next_due_once_without_timestamp_is_none():
    s = _signal(schedule_kind=t.KIND_ONCE, once_at=None)
    assert t.next_due_at(s) is None


def test_next_due_once_not_yet_fired_returns_deadline():
    s = _signal(schedule_kind=t.KIND_ONCE, once_at=NOW + 600, last_run_at=None)
    assert t.next_due_at(s) == NOW + 600


def test_next_due_once_already_fired_is_none():
    """`last_run_at >= once_at` 即视为已执行 —— once 只跑一次。

    这条也是"一次性任务触发后前端该收起倒计时"的依据。
    """
    deadline = NOW + 600
    s = _signal(schedule_kind=t.KIND_ONCE, once_at=deadline, last_run_at=deadline)
    assert t.next_due_at(s) is None


def test_next_due_once_fired_later_than_deadline_is_none():
    """实际执行可能晚于计划时刻（排到下一拍），仍算已触发。"""
    deadline = NOW + 600
    s = _signal(schedule_kind=t.KIND_ONCE, once_at=deadline, last_run_at=deadline + 120)
    assert t.next_due_at(s) is None


def test_next_due_unknown_kind_is_none():
    s = _signal(schedule_kind="weekly")
    assert t.next_due_at(s) is None


# ── 熔断阈值的边界 ──────────────────────────────────────────────────


def test_melt_threshold_boundary():
    """`MELT_AFTER_FAILURES` 是"达到即熔断"，不是"超过才熔断"。

    off-by-one 的代价：设成 3 却要 4 次才熔断，意味着已经连续失败三次的
    配置还会再花一次 run 的钱。
    """
    n = t.MELT_AFTER_FAILURES
    assert t.should_melt(
        consecutive_failures=n - 1, tokens_used_today=0, daily_token_budget=100
    ) == ""
    assert (
        t.should_melt(consecutive_failures=n, tokens_used_today=0, daily_token_budget=100)
        == t.MELT_ERRORS
    )


def test_budget_boundary_equal_counts_as_exhausted():
    """用到正好等于预算即视为耗尽。

    若写成 `>`，预算 100 的配置还能再花一次 —— 而那一次的额度已经没了。
    """
    assert (
        t.should_melt(consecutive_failures=0, tokens_used_today=100, daily_token_budget=100)
        == t.MELT_BUDGET
    )
    assert (
        t.should_melt(consecutive_failures=0, tokens_used_today=99, daily_token_budget=100)
        == ""
    )


def test_budget_zero_never_melts_on_budget():
    """未配置预算（0）不按预算熔断 —— 该由保存期校验拦住，不是运行时。

    运行时把 0 当作"已耗尽"会让所有老配置一夜之间全停。
    """
    assert (
        t.should_melt(consecutive_failures=0, tokens_used_today=999999, daily_token_budget=0)
        == ""
    )
