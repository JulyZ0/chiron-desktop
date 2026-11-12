"""压缩预算口径回归（缺陷 1–3）。

背景：chiron 此前把"压缩预算"当成**消息的**预算，三处口径都偏乐观：

1. **工具表不计入**。几十个工具的 JSON schema 轻易上万 token，它实打实进输入，
   却完全不进判定 —— 于是"压缩线还没到、请求先溢出"。溢出一旦发生只能走强制压缩，
   丢得更多、还多烧一次往返。
2. **不扣输出额度**。窗口是输入输出共用的，`max_tokens=32k` 时输入只有约 96k，
   压缩线却按整窗排。
3. **重试判据用消息条数**。snipe 把工具结果从 16000 字符截到 2000 时**一条都没少**，
   按条数判就会认定"没压缩"，于是再砍一刀 `max_messages` —— 已经省下的空间白扔。

本文件锁住三条：`_input_budget` 的算术、`tools_tokens` 参与阈值判定、
`_force_compact` 的降级判据是 token 而不是条数。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import compaction as compaction_mod
from app.agent import runtime as runtime_mod
from app.agent.compaction import (
    CompactionConfig,
    _compact_messages,
    _input_budget,
    _tools_tokens,
)
from app.agent.runtime import AgentRuntime

# ── 1. 输入预算：扣输出额度 + 留估算余量 ─────────────────────────────


def test_input_budget_subtracts_output_reserve():
    """128k 窗口配 32k 输出时，输入只有 89600 —— 不是 128k。"""
    assert _input_budget(128_000, 32_000) == int(128_000 * 0.95) - 32_000


def test_input_budget_keeps_floor_output_reserve():
    """没配输出额度也要留 1024：否则输入塞满窗口就必然溢出。"""
    assert _input_budget(8_192, 0) == int(8_192 * 0.95) - 1024


def test_input_budget_unknown_window_stays_zero():
    """窗口未知时返回 0，调用方据此回落常量而不是瞎猜。"""
    assert _input_budget(0, 4096) == 0


def test_input_budget_never_goes_negative():
    """输出额度大于窗口时保底 1，不能返回 0（那会被读成"窗口未知"）。"""
    assert _input_budget(4_000, 100_000) >= 1


# ── 2. 工具表参与阈值判定 ────────────────────────────────────────────


def test_tools_tokens_counts_schema():
    assert _tools_tokens(None) == 0
    assert _tools_tokens([]) == 0
    tools = [{"type": "function", "function": {"name": "read_file", "description": "x" * 4000}}]
    assert _tools_tokens(tools) > 500


def _snipe_boundary_messages() -> list[dict[str, Any]]:
    """构造一组**本身**压在 SNIP 区间、加上工具表就跨到 PRUNE 的消息。

    总 token ≈ 7500，窗口 10000：
    - 工具表为 0   → 0.75，落在 [0.70, 0.85) → snipe（只截工具结果，条数不变）
    - 工具表 3000 → 1.05，越过 0.85        → prune（真丢消息）
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": "你是助手"}]
    for i in range(10):
        out.append({"role": "user", "content": f"问题 {i} " + "u" * 480})
        out.append({"role": "assistant", "content": f"回答 {i} " + "a" * 480})
    out.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "shell_exec", "arguments": "{}"}}]})
    out.append({"role": "tool", "tool_call_id": "c1", "content": "z" * 20_000})
    return out


def test_tools_tokens_push_compaction_over_the_line(monkeypatch: pytest.MonkeyPatch):
    """同样的消息，只因工具表占 3000 token，判定就从 SNIP 升到 PRUNE。

    断的是**走了哪条分支**而不是"少了几条消息"：prune 的尾部预算在有些令牌总量下
    能装下全部消息，条数照样不变 —— 用条数做断言会把判定正确、结果正确的实现判成失败。
    """
    calls: list[str] = []

    def spy(name: str, impl: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(name)
            return impl(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(compaction_mod, "_snip_tool_results", spy("snip", compaction_mod._snip_tool_results))
    monkeypatch.setattr(compaction_mod, "_prune_messages", spy("prune", compaction_mod._prune_messages))

    msgs = _snipe_boundary_messages()
    base = {"max_context_tokens": 10_000, "threshold_ratio": 0.85, "snipe_ratio": 0.70, "model": ""}

    calls.clear()
    _compact_messages(msgs, CompactionConfig(**base))
    assert calls == ["snip"], "不计工具表时应停在 SNIP（消息一条不丢）"

    calls.clear()
    _compact_messages(msgs, CompactionConfig(**base, tools_tokens=3_000))
    assert calls == ["prune"], "计入工具表后应越过 0.85 线进入 PRUNE"


# ── 3. 强制压缩的降级判据是 token ────────────────────────────────────


def _mode_stub() -> Any:
    """`_resolve_compaction` 只读 `mode_cfg.compaction`。"""
    return SimpleNamespace(compaction=None)


def _few_messages_with_huge_tool_result() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "读一下那个大文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "y" * 30_000},
    ]


def test_force_compact_does_not_downgrade_when_tokens_shrank(monkeypatch: pytest.MonkeyPatch):
    """条数没变但 token 明显变小 → 不应再砍 max_messages。

    这正是 snipe 的形态：一条工具结果从 16000 字符截到 2000，消息数**一条都不少**。
    旧代码按条数判，会认定"没压出空间"，再降级砍半 —— 白扔刚省下的空间。
    """
    calls: list[int] = []
    real_prune = runtime_mod._prune_messages

    def counting_prune(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(1)
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "_prune_messages", counting_prune)
    msgs = _few_messages_with_huge_tool_result()

    out = AgentRuntime(gateway=None)._force_compact(
        msgs, _mode_stub(), {}, window=100_000, max_output=8_000, tools=None, model=""
    )

    assert len(out) == len(msgs), "消息条数本来就没变"
    assert sum(len(m.get("content") or "") for m in out) < sum(
        len(m.get("content") or "") for m in msgs
    ), "工具结果必须真的被截短，否则这条测试没有意义"
    assert calls == [], "token 已经变小，就不该触发降级砍半"


def test_force_compact_downgrades_when_nothing_shrank(monkeypatch: pytest.MonkeyPatch):
    """压缩真的没让出任何空间时，才降级砍 max_messages。"""
    msgs = _few_messages_with_huge_tool_result()
    calls: list[int] = []

    monkeypatch.setattr(
        runtime_mod, "_compact_messages", lambda messages, *a, **kw: list(messages)
    )
    monkeypatch.setattr(
        runtime_mod,
        "_prune_messages",
        lambda messages, *a, **kw: calls.append(1) or messages[:1],
    )

    out = AgentRuntime(gateway=None)._force_compact(
        msgs, _mode_stub(), {}, window=100_000, max_output=8_000, tools=None, model=""
    )

    assert calls == [1], "一个字都没省下来时必须走降级路径"
    assert len(out) < len(msgs)
