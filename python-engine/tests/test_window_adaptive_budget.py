"""token 预算必须跟着**模型窗口**走，并在撞线时续段而不是直接截断。

**这里钉住什么**

每轮都会把完整历史重发一次，累计 input 随轮次近似**平方**增长。用一个固定的
400k 额度去管 40 轮的长任务，必然在十几轮就撞线 —— 表现就是用户看到的
"这一轮因预算越界停在这里"，而任务其实完全能跑完。

两条修法各有一个反例要防：

1. 窗口自适应**不能缩小**：窗口表查不到/填了离谱小值时，上限必须仍是默认地板，
   否则大模型捡了便宜、小模型反被压到跑不动。
2. 续段**不能越过显式配置**：`AGENT_RUN_MAX_TOKENS` 是用户要的硬线，
   悄悄放大等于这个配置项没生效（既有单段语义的测试也依赖这一点）。
"""

from __future__ import annotations

from app.agent.task_budget import (
    DEFAULT_MAX_TOKENS,
    TaskBudget,
    from_env,
)


def test_window_scales_default_budget_up() -> None:
    """1M 窗口的模型应当拿到远大于默认地板的额度。"""
    budget = TaskBudget()
    assert budget.tokens == DEFAULT_MAX_TOKENS

    budget.scale_to_window(1_000_000)

    assert budget.tokens == 3_000_000, "窗口 × 3 是自适应上限"
    assert budget.segment_tokens == budget.tokens, "续段步长跟着上限走"


def test_window_never_shrinks_below_floor() -> None:
    """窗口小或查不到时保持默认地板 —— 自适应只放大。"""
    for window in (0, -1, 32_000):
        budget = TaskBudget()
        budget.scale_to_window(window)
        assert budget.tokens == DEFAULT_MAX_TOKENS, f"window={window} 不应压低上限"


def test_segment_grants_room_and_keeps_usage() -> None:
    """续段只抬上限，不动已用量：用量同时是计费口径。"""
    budget = from_env(max_segments=2)
    budget.scale_to_window(1_000_000)
    base = budget.tokens

    assert budget.grant_next_segment() is True
    assert budget.tokens == base + base, "续一段 = 再给一个窗口量级的额度"
    assert budget.segments_left == 1

    assert budget.grant_next_segment() is True
    assert budget.segments_left == 0
    assert budget.grant_next_segment() is False, "段数用尽必须真失败，不能无限续"


def test_explicit_token_cap_is_a_hard_line() -> None:
    """显式配置的额度不被窗口放大，也不续段。"""
    budget = from_env(tokens=50_000, max_segments=3)
    assert budget.tokens_pinned is True

    budget.scale_to_window(1_000_000)
    assert budget.tokens == 50_000, "显式额度不能被窗口悄悄放大"

    assert budget.grant_next_segment() is False, "显式额度是硬线，续段也不得越过"
    assert budget.segments_left == 3, "拒绝续段不应消耗段数"


def test_env_token_cap_is_also_pinned(monkeypatch) -> None:
    """环境变量给的额度同样是硬线（与构造参数一视同仁）。"""
    monkeypatch.setenv("AGENT_RUN_MAX_TOKENS", "120000")

    budget = from_env(max_segments=1)

    assert budget.tokens == 120_000
    assert budget.tokens_pinned is True
    assert budget.grant_next_segment() is False


def test_no_segments_means_overflow_fails_as_before(monkeypatch) -> None:
    """不给段数（子 Agent / 关闭续段）时行为与修复前完全一致。"""
    monkeypatch.delenv("AGENT_RUN_MAX_TOKENS", raising=False)

    budget = from_env()
    assert budget.segments_left == 0
    assert budget.grant_next_segment() is False


def test_zero_token_cap_still_means_unlimited(monkeypatch) -> None:
    """`XXX_RUN_MAX_TOKENS=0` 是"这一轴不限"，不能被当成"没配"回落默认值。

    这是"显式 vs 默认"分组最容易踩的坑：把 0 归进默认，用户就再也关不掉这一轴了。
    """
    monkeypatch.setenv("AGENT_RUN_MAX_TOKENS", "0")

    budget = from_env(max_segments=3)

    assert budget.tokens == 0, "0 表示不限，不该被默认值顶掉"
    assert budget.tokens_pinned is True
    assert budget.exceeded(tokens=10_000_000) == "", "不限时再大的用量也不该越界"


def test_rescaling_each_turn_does_not_compound_the_segment() -> None:
    """`scale_to_window` 每轮开头都会调用 —— 续段不能因此复利膨胀。

    反例：步长取"当前上限"，则续段抬高上限后，下一轮步长跟着抬高，
    八段下来是指数级，等于这段预算没有闸门。步长必须锚定窗口。
    """
    budget = TaskBudget(segments_left=8)
    budget.scale_to_window(1_000_000)
    window_cap = budget.tokens

    for _ in range(8):
        # 模拟每次工具/轮次循环：轮首重新自适应，然后撞线续段。
        budget.scale_to_window(1_000_000)
        assert budget.grant_next_segment() is True

    assert budget.tokens == window_cap * 9, (
        "总上限应当是 1 个基准段 + 8 个窗口量级的续段，而不是复利"
    )
    assert budget.segment_tokens == window_cap, "步长必须锚定窗口，不随上限漂移"


