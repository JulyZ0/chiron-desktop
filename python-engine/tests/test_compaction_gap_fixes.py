"""D1–D9：与 deepagents / Reasonix 对齐后的压缩行为契约。

每一条都对应一个**真实会伤到用户**的失败模式，不是"别人有所以我也要有"：

- **D1** 压缩没腾出空间还重发一次 ⇒ 必然再溢出，用户多等一倍、还多付一次钱。
- **D3** 切点越过本轮请求 ⇒ 压缩完模型忘了自己在干什么。
- **D4** 折叠边界停在工具组中间 ⇒ 交给 provider 一段自相矛盾的序列。
- **D5** 摘要链无限累积 ⇒ 折叠十几次后光摘要就占掉大半预算。
- **D6** 手动压缩没有准入门槛 ⇒ 短对话里模型一喊就丢掉一半历史。
- **D7** 小到不值得的区间也发摘要调用 ⇒ 纯烧钱 + 拖慢"用户正在等"的路径。
- **D8** 缺 `seq` 就整段放弃投影 ⇒ 回退全量、只能走丢得更多的实时裁剪。
- **D9** 指纹不含模型名 ⇒ 换模型导致前缀缓存全失效，却查不出原因。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.compaction import (
    DIGEST_CHAIN_MAX_CHARS,
    CompactionConfig,
    _apply_folds,
    _prune_messages,
)
from app.agent.runtime import AgentRuntime

# ── 工具 ───────────────────────────────────────────────────────────────


def _with_seq(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给非 system 消息补 seq（1 起），模拟从库里读出的历史。"""
    out: list[dict[str, Any]] = []
    seq = 0
    for m in messages:
        if m["role"] == "system":
            out.append(m)
            continue
        seq += 1
        out.append({**m, "seq": seq})
    return out


def _turn_messages() -> list[dict[str, Any]]:
    """既定形状：一条早前交换 + 一条**本轮请求** + 它引发的工具调用。"""
    return _with_seq(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "很早的请求"},
            {"role": "assistant", "content": "很早的回答"},
            {"role": "user", "content": "本轮请求：修一个 bug"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
            {"role": "assistant", "content": "改好了"},
        ]
    )


def _all_content(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def _assert_no_orphan_tool(messages: list[dict[str, Any]]) -> None:
    """kept 里的 tool 消息前面必须有配对的 assistant(tool_calls)（D4 不变量）。"""
    open_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            open_ids = {tc.get("id") for tc in (m.get("tool_calls") or [])}
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in open_ids, f"孤立工具结果：{m.get('tool_call_id')}"
            open_ids.discard(m.get("tool_call_id"))


# ── D3：本轮请求不能被折进摘要 ────────────────────────────────────────


def test_current_turn_request_survives_pruning():
    """切点会越过本轮请求时，必须把它留在上下文里。

    长回合（几十次工具调用）里按条数/token 算出的切点很容易越过去，
    压缩完模型就"忘了自己在干什么"—— 表现为重复确认需求、跑到别的方向上去。
    """
    messages = _turn_messages()

    out = _prune_messages(messages, CompactionConfig(max_messages=4))

    assert "本轮请求：修一个 bug" in _all_content(out), "本轮请求被折进了摘要"
    _assert_no_orphan_tool([m for m in out if m["role"] != "system"])


def test_turn_start_is_ignored_when_it_is_the_very_first_message():
    """整个区间都是本轮时必须照常压缩 —— 否则窗口照样爆，只是推给溢出路径。"""
    messages = _with_seq(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "唯一的请求"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "回答二"},
        ]
    )

    out = _prune_messages(messages, CompactionConfig(max_messages=3))

    # 注意判据不是"条数变少"：裁剪会**追加**一条计数提示和一条续接简报，
    # 条数可能持平。真正的判据是"有内容被裁掉了"。
    assert "were pruned" in _all_content(out), "起点就是第一条时不应禁止压缩"


def test_compaction_own_notices_are_not_mistaken_for_the_turn_request():
    """裁剪通知 / 续接简报是压缩自己写的，不能当"本轮请求"钉住。"""
    messages = _with_seq(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "真请求"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "[17 earlier message(s) were pruned to fit the context window]"},
            {"role": "assistant", "content": "回答二"},
            {"role": "user", "content": "更多"},
            {"role": "assistant", "content": "回答三"},
        ]
    )

    out = _prune_messages(messages, CompactionConfig(max_messages=3))

    # 提示串本身不是用户请求，不该把它当成本轮起点而阻止压缩。
    assert len(out) < len(messages)


# ── D5：摘要链有上限，最新的完整保留 ─────────────────────────────────


def _folds_with_long_digests(per_digest: int) -> list[dict[str, Any]]:
    return [
        {"covered_from": 1, "covered_to": 4, "digest": "OLD" + "o" * per_digest, "covered_ids": []},
        {"covered_from": 4, "covered_to": 8, "digest": "MID" + "m" * per_digest, "covered_ids": []},
        {"covered_from": 8, "covered_to": 12, "digest": "NEW" + "n" * per_digest, "covered_ids": []},
    ]


def _twelve_messages() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": "你是助手"}]
    for i in range(1, 13):
        out.append({"role": "user" if i % 2 else "assistant", "content": f"内容 {i}", "seq": i})
    return out


def test_digest_chain_is_bounded_and_keeps_the_newest():
    """折叠十几次后摘要本身会线性累积 —— 超限时从旧到新截，最新一条永远完整。"""
    per = DIGEST_CHAIN_MAX_CHARS // 2 - 10  # 两条装得下、三条装不下
    folds = _folds_with_long_digests(per)

    projected = _apply_folds(_twelve_messages(), folds)

    assert projected is not None
    summary = projected[1]["content"]
    assert "NEW" in summary, "最新摘要必须完整保留（它覆盖刚刚发生的事）"
    assert "OLD" not in summary, f"总长超过 {DIGEST_CHAIN_MAX_CHARS} 时旧摘要应被截掉"


def test_digest_chain_keeps_everything_when_it_fits():
    folds = _folds_with_long_digests(100)

    projected = _apply_folds(_twelve_messages(), folds)

    assert projected is not None
    summary = projected[1]["content"]
    assert {"OLD", "MID", "NEW"} <= {w for w in ("OLD", "MID", "NEW") if w in summary}


def test_single_oversized_digest_is_truncated_not_dropped():
    """唯一一条摘要就超限时也要保住它的开头 —— 一条不装等于历史归零。"""
    folds = [
        {
            "covered_from": 1,
            "covered_to": 12,
            "digest": "NEW" + "n" * (DIGEST_CHAIN_MAX_CHARS * 2),
            "body_suffix": 0,
        }
    ]

    projected = _apply_folds(_twelve_messages(), folds)

    assert projected is not None
    assert "NEW" in projected[1]["content"]


# ── D8：缺 seq 时用 covered_ids 重建坐标 ──────────────────────────────


def _no_seq_messages() -> list[dict[str, Any]]:
    """有 id、没有 seq 的历史（导入的旧会话就是这种形状）。"""
    out: list[dict[str, Any]] = [{"role": "system", "content": "你是助手"}]
    for i in range(1, 7):
        out.append({"id": f"m{i}", "role": "user" if i % 2 else "assistant", "content": f"内容 {i}"})
    return out


def test_missing_seq_is_rebuilt_from_covered_ids():
    folds = [
        {
            "covered_from": 1,
            "covered_to": 4,
            "digest": "- [user] 早前要点",
            "covered_ids": ["m1", "m2", "m3"],
            "body_suffix": 3,
        }
    ]

    projected = _apply_folds(_no_seq_messages(), folds)

    assert projected is not None, "有 id 就足以重建坐标，不该整段放弃"
    assert "早前要点" in projected[1]["content"]
    assert len(projected) == 1 + 1 + 3, "system + 摘要 + 3 条原样尾部"


def test_projection_still_refuses_when_ids_are_unusable():
    """连 id 都没有时没有可靠依据 —— 宁可回退全量，也不拼一份会漏内容的投影。"""
    messages = _no_seq_messages()
    messages[1] = {"role": "user", "content": "没有 id 也没有 seq"}  # type: ignore[assignment]
    folds = [
        {
            "covered_from": 1,
            "covered_to": 4,
            "digest": "- 要点",
            "covered_ids": ["m1", "m2"],
            "body_suffix": 3,
        }
    ]

    assert _apply_folds(messages, folds) is None


# ── D6：手动压缩准入门槛 ─────────────────────────────────────────────


def _mode_stub() -> Any:
    return SimpleNamespace(compaction=None)


def _short_conversation() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "帮我看一下这个函数"},
        {"role": "assistant", "content": "看起来是空指针"},
    ]


def test_manual_compaction_is_declined_below_half_the_auto_trigger():
    """短对话里模型喊一次压缩不该丢掉一半历史。

    `compact_conversation` 是模型可自主调用的工具，而模型对"该整理了吗"的判断
    常常偏早（阶段边界、刚读完几个文件）。自动路径从不会这样 —— 它是阈值驱动的。
    """
    runtime = AgentRuntime(gateway=None)
    messages = _short_conversation()

    out, notice = runtime._compact_with_notice(
        messages,
        _mode_stub(),
        {"compaction": {"max_context_tokens": 200_000, "threshold_ratio": 0.85}},
        force=True,
    )

    assert out == messages, "未达自动触发线一半时不应压缩"
    assert notice is None


def test_manual_compaction_proceeds_once_past_the_gate():
    """越过门槛后照常压缩 —— 门槛不能变成"手动入口永远无效"。"""
    runtime = AgentRuntime(gateway=None)
    messages = [
        {"role": "system", "content": "你是助手"},
        *[
            {"role": "user" if i % 2 else "assistant", "content": f"第 {i} 段：" + "很长的描述。" * 200}
            for i in range(40)
        ],
    ]

    out, notice = runtime._compact_with_notice(
        messages,
        _mode_stub(),
        {"compaction": {"max_context_tokens": 20_000, "threshold_ratio": 0.85}},
        force=True,
    )

    assert len(out) < len(messages)
    assert notice is not None, "真的压缩了就要让前端看得见"


# ── D7：折叠经济性 ───────────────────────────────────────────────────


class _StubGateway:
    def __init__(self, content: str = "") -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages=None, model="", max_tokens=0, temperature=0.0):
        self.calls.append({"model": model})
        return SimpleNamespace(content=self._content, finish_reason="stop")


@pytest.mark.asyncio
async def test_tiny_folds_do_not_get_a_model_summary():
    """几十 token 的区间省下的量抵不过一次往返 —— 保留提取式摘要即可。"""
    gateway = _StubGateway(content="- 无关内容")
    runtime = object.__new__(AgentRuntime)
    runtime._gateway = gateway
    folds = [
        {
            "covered_from": 1,
            "covered_to": 2,
            "digest": "- 提取式",
            "digest_model": "extractive",
            "_source": [{"role": "user", "content": "嗯"}],
        }
    ]

    await runtime._upgrade_fold_digests(folds, model="m")

    assert gateway.calls == [], "小到不值得就不该发调用"
    assert folds[0]["digest_model"] == "extractive"
    assert "_source" not in folds[0], "原文无论如何都要清掉"


# ── D9：指纹含模型名 ─────────────────────────────────────────────────


def test_prefix_change_reports_model_switch():
    """换模型会让整段前缀缓存失效，代价与改 system 段相同，必须能被看见。"""
    runtime = object.__new__(AgentRuntime)
    runtime._last_prefix_fp = None
    messages = [{"role": "system", "content": "你是助手"}]

    first = runtime._prefix_change(messages, [], "model-a")
    second = runtime._prefix_change(messages, [], "model-b")

    assert first["first_turn"] is True
    assert second["prefix_changed"] == ["model"]


def test_prefix_change_is_silent_when_nothing_changed():
    runtime = object.__new__(AgentRuntime)
    runtime._last_prefix_fp = None
    messages = [{"role": "system", "content": "你是助手"}]

    runtime._prefix_change(messages, [], "model-a")
    again = runtime._prefix_change(messages, [], "model-a")

    assert again["prefix_changed"] == []


# ── D1：压缩没腾出空间就不重发 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_overflow_is_not_retried_when_compaction_freed_nothing():
    """重发一个与刚被拒请求同样大的请求，只会再失败一次 —— 纯亏延迟与计费。"""
    from app.gateway.provider import ChatResponse

    calls = {"n": 0}

    async def always_overflow(**kwargs: Any):
        calls["n"] += 1
        raise RuntimeError("context_length_exceeded")
        yield ChatResponse(content="unreachable", finish_reason="stop")  # pragma: no cover

    runtime = AgentRuntime(gateway=SimpleNamespace(chat_stream=always_overflow))
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "hi"},
    ]
    flags: list[dict[str, Any]] = []

    with pytest.raises(RuntimeError):
        async for _ in runtime._chat_stream_with_retry(
            messages,
            model="m",
            provider_hint="openai",
            tenant_id=0,
            max_tokens=1024,
            temperature=0.0,
            tools=None,
            # 压缩"跑了但没效果"（只剩 system 可压缩时的真实形态）
            on_overflow=lambda: list(messages),
            overflow_flags=flags,
        ):
            pass

    assert calls["n"] == 1, "没腾出空间就不该重发"
    assert flags and flags[0]["messages_after"] == flags[0]["messages_before"]
