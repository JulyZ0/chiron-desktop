"""压缩触发线「预算 - 固定提前量」（G-L2）的回归测试。

背景：chiron 此前用**纯比例**
（`0.70` / `0.85`）决定何时压缩。比例在小窗口模型上会放大成灾难：`8K` 窗口的 85%
只剩 1.2K 留给"摘要输入 + 摘要输出 + 下一轮提问"，压缩动作本身就可能把请求顶出窗口；
而同一条比例在 `200K` 上留 30K，绰绰有余。

这里守住五条契约：
1. **小窗口靠提前量更早触发** —— 固定 reserve 才是小端的正确表达
2. **大窗口仍由比例说了算** —— 提前量相对微不足道，不该无谓提前压缩
3. **提前量本身有上限** —— 不能超过窗口 25%，否则 8K 窗口减 16K 得到负数，
   触发线被压到 0 就是"每轮都压缩"，比触发太晚更糟
4. **窗口未知 / 阈值压平保持原语义** —— `force`、手动压缩、溢出恢复都靠这个
5. **上报给前端的比例与判定同源** —— 环上写的数字必须就是真实触发点
"""

from __future__ import annotations

from typing import Any

import pytest

import app.agent.compaction as compaction_mod
from app.agent.compaction import (
    RESERVE_MAX_WINDOW_RATIO,
    RESERVE_TOKENS,
    CompactionConfig,
    _compact_messages,
    _trigger_tokens,
)

SMALL_WINDOW = 8_192
LARGE_WINDOW = 200_000


# ── 触发线本身 ───────────────────────────────────────────────────────


def test_small_window_triggers_earlier_than_the_ratio():
    """8K 窗口上，85% 名义比例必须被提前量拉到更早 —— 否则留不出摘要空间。"""
    line = _trigger_tokens(SMALL_WINDOW, 0.85)

    assert line < int(SMALL_WINDOW * 0.85), "小窗口上提前量没有生效"
    # 具体值：min(6963, 8192 - 2048) = 6144
    assert line == SMALL_WINDOW - min(RESERVE_TOKENS, int(SMALL_WINDOW * RESERVE_MAX_WINDOW_RATIO))
    # 留出的空间至少够一次摘要往返（这里断言"明显多于纯比例剩下的 1.2K"）
    assert SMALL_WINDOW - line >= 2048


def test_large_window_still_follows_the_ratio():
    """200K 窗口上 16K 提前量只占 8%，比例线仍应胜出 —— 不必无谓提前压缩。"""
    line = _trigger_tokens(LARGE_WINDOW, 0.85)

    assert line == int(LARGE_WINDOW * 0.85)


def test_reserve_never_exceeds_its_window_share():
    """提前量封顶到窗口的 25%：无上限时小窗口会减成负数。"""
    # 一个比 reserve 还小的窗口
    tiny = 8_000
    line = _trigger_tokens(tiny, 0.99)

    assert line > 0, "触发线被压到 0 就是每轮都压缩"
    assert line == tiny - int(tiny * RESERVE_MAX_WINDOW_RATIO)


@pytest.mark.parametrize("window", [1_000, 4_096, 8_192, 32_768, 128_000, 1_000_000])
@pytest.mark.parametrize("ratio", [0.5, 0.70, 0.85, 0.95, 0.99])
def test_trigger_is_never_a_no_op_or_the_whole_window(window, ratio):
    """任何窗口 × 任何比例都必须落在 (0, window) 开区间内。

    越界的两端各自是灾难：≤0 ⇒ 每轮无条件压缩；≥window ⇒ 永远压不动（溢出才救）。
    """
    line = _trigger_tokens(window, ratio)

    assert 0 < line < window


def test_trigger_never_later_than_the_ratio_line():
    """「取更早者」是这条差距的核心：提前量只能让触发更早，不能更晚。"""
    for window in (1_000, 8_192, 32_768, 200_000):
        for ratio in (0.5, 0.70, 0.85):
            assert _trigger_tokens(window, ratio) <= int(window * ratio)


def test_zero_ratio_means_unconditional():
    """阈值被压平（`force` / 手动 / 溢出恢复）= 无条件触发。

    这些路径把 `threshold_ratio` 设成 0 来表达"现在就压"，返回 0 才能让
    `ratio >= trigger_ratio` 恒成立。
    """
    assert _trigger_tokens(LARGE_WINDOW, 0.0) == 0
    assert _trigger_tokens(LARGE_WINDOW, -1.0) == 0


def test_unknown_window_returns_zero():
    """窗口未知时没有预算可减 —— 返回 0 让调用方先判 `window`，而不是瞎猜。"""
    assert _trigger_tokens(0, 0.85) == 0


# ── _compact_messages 集成 ───────────────────────────────────────────


def _messages_at_tokens(target: int) -> list[dict[str, Any]]:
    """构造大致 `target` token 的平淡对话（4 chars ≈ 1 token）。

    每条塞满 `CHARS_PER_MESSAGE` 字符，而不是切成一堆小消息：`_compact_messages` 在
    `len(messages) > cfg.max_messages`（默认 200）时会**先**走条数上限分支，那样就测不到
    token 阈值了 —— 200K 窗口按 400 字符一条要 1600 条，直接撞上限。
    """
    chars_per_message = 8_000
    out: list[dict[str, Any]] = [{"role": "system", "content": "你是助手"}]
    remaining = target * 4
    i = 0
    while remaining > 0:
        size = min(chars_per_message, remaining)
        out.append(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i} " + "x" * size}
        )
        remaining -= size
        i += 1
    assert len(out) <= 200, f"构造出 {len(out)} 条消息，会撞上 max_messages 条数上限"
    return out


def _branch_calls(monkeypatch, msgs, cfg) -> list[str]:
    calls: list[str] = []

    def spy(name: str, impl: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(name)
            return impl(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        compaction_mod, "_snip_tool_results", spy("snip", compaction_mod._snip_tool_results)
    )
    monkeypatch.setattr(
        compaction_mod, "_prune_messages", spy("prune", compaction_mod._prune_messages)
    )
    _compact_messages(msgs, cfg)
    return calls


def test_small_window_prunes_below_the_nominal_ratio(monkeypatch):
    """8K 窗口上，占用仅 78% 就该 PRUNE —— 旧实现要等到 85%。"""
    msgs = _messages_at_tokens(int(SMALL_WINDOW * 0.78))
    actual = compaction_mod._estimate_tokens(msgs, "")
    # 前提校验：确实落在 [新线, 名义线) 之间，否则这个用例没测到差别
    new_line = _trigger_tokens(SMALL_WINDOW, 0.85)
    assert new_line <= actual < int(SMALL_WINDOW * 0.85), (
        f"构造占用 {actual} 应落在 [{new_line}, {int(SMALL_WINDOW * 0.85)})"
    )

    calls = _branch_calls(
        monkeypatch, msgs, CompactionConfig(max_context_tokens=SMALL_WINDOW, model="")
    )

    assert calls == ["prune"]


def test_large_window_still_waits_for_the_ratio(monkeypatch):
    """200K 窗口上，占用 80% 仍停在 SNIP —— PRUNE 线不该被提前量拉早。

    200K × 0.85 = 170K，而 200K − 16K = 184K，取更早者仍是 170K：大窗口上 16K 提前量
    只占 8%，相对微不足道，行为与纯比例完全一致。若实现错误地把提前量当绝对阈值、
    或取成"更晚者"，80%（160K）就会被判成 PRUNE —— 那是无谓丢上下文。
    """
    # 先钉住大窗口上提前量不生效这一前提
    assert _trigger_tokens(LARGE_WINDOW, 0.85) == int(LARGE_WINDOW * 0.85)

    msgs = _messages_at_tokens(int(LARGE_WINDOW * 0.80))

    calls = _branch_calls(
        monkeypatch, msgs, CompactionConfig(max_context_tokens=LARGE_WINDOW, model="")
    )

    assert calls == ["snip"], "80% 应停在 SNIP（PRUNE 线是 85%）"


def test_force_still_compacts_at_any_occupancy(monkeypatch):
    """阈值压平（`force` / 手动 / 溢出恢复）必须无视占用直接压。"""
    msgs = _messages_at_tokens(200)  # 远低于任何触发线

    calls = _branch_calls(
        monkeypatch,
        msgs,
        CompactionConfig(
            max_context_tokens=SMALL_WINDOW, threshold_ratio=0.0, snipe_ratio=0.0, model=""
        ),
    )

    assert calls == ["prune"]


def test_unknown_window_falls_back_to_the_bare_ratio(monkeypatch):
    """`max_context_tokens == 0` 时没有预算可减，退回纯配置比例（既有行为）。"""
    msgs = _messages_at_tokens(200)

    calls = _branch_calls(
        monkeypatch, msgs, CompactionConfig(max_context_tokens=0, threshold_ratio=0.85, model="")
    )

    assert calls == [], "窗口未知时不应触发压缩"


def test_tail_ceiling_uses_the_same_trigger_line():
    """尾部预算的上限必须与判定同源，否则小窗口上会"反复触发、每次只压几条"。

    这里直接比较两类窗口下 `_recent_tail_start` 给出的起点：小窗口的尾部预算应当
    受新的（更早的）触发线约束，而不是 85%。
    """
    from app.agent.compaction import _recent_tail_start

    msgs = _messages_at_tokens(int(SMALL_WINDOW * 0.9))
    cfg = CompactionConfig(max_context_tokens=SMALL_WINDOW, model="")

    start = _recent_tail_start(msgs, cfg)
    trigger = _trigger_tokens(SMALL_WINDOW, cfg.threshold_ratio)
    # 尾部最多占触发线的一半 —— 否则触发时已经装不下任何东西可折。
    kept_tokens = sum(
        compaction_mod._fixed_tokens(m) for m in msgs[start:]
    )
    assert kept_tokens <= max(1, trigger // 2)
