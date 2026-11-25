"""G9：工具结果状态可判定（unknown vs not_started），恢复时不再混为一谈。

修前会怎样：恢复逻辑把"历史里缺一条 tool 结果"的调用**一律**注入同一句
`UNKNOWN_RESULT` 文案。于是两种性质完全不同的情况拿到相同待遇：
· 已开始（数据库有 pending 行）→ 可能已产生副作用，**不能重试**；
· 从未派发 → 没有任何副作用，**可以安全重试**。
而且只有自然语言，机器（前端/评测/审计）无法判定。
"""

from __future__ import annotations

import pytest

from app.agent import resume, tool_outcome as to


# ── 1. 词表与分类器 ───────────────────────────────────────────────────────
def test_outcome_vocabulary_is_closed() -> None:
    assert to.OUTCOMES == {"ok", "error", "cancelled", "unknown", "not_started"}


def test_tag_round_trip() -> None:
    for outcome in to.OUTCOMES:
        text = to.with_tag("正文", outcome)
        assert to.classify(text) == outcome
        assert to.strip_tag(text) == "正文"


def test_with_tag_is_idempotent() -> None:
    once = to.with_tag("正文", to.OUTCOME_UNKNOWN)
    twice = to.with_tag(once, to.OUTCOME_UNKNOWN)
    assert once == twice


def test_unknown_status_would_be_a_bug() -> None:
    with pytest.raises(ValueError):
        to.tag("maybe")


def test_classify_defaults_to_unknown_not_ok() -> None:
    """没有标签的历史消息一律判 `unknown` —— 宁可"不知道"，也别当成成功。"""
    assert to.classify("工具返回了一段没有标签的文本") == to.OUTCOME_UNKNOWN
    assert to.classify(None) == to.OUTCOME_UNKNOWN


def test_classify_result_prefers_structured_outcome() -> None:
    assert to.classify_result({"outcome": "unknown"}) == to.OUTCOME_UNKNOWN
    assert to.classify_result({"cancelled": True}) == to.OUTCOME_CANCELLED
    assert to.classify_result({"error": "boom"}) == to.OUTCOME_ERROR
    assert to.classify_result({"output": "ok"}) == to.OUTCOME_OK


# ── 2. 重试与复核语义 ────────────────────────────────────────────────────
def test_unknown_is_not_retryable_and_needs_review() -> None:
    assert to.retryable(to.OUTCOME_UNKNOWN) is False
    assert to.needs_human_review(to.OUTCOME_UNKNOWN) is True


def test_cancelled_and_not_started_are_retryable() -> None:
    assert to.retryable(to.OUTCOME_CANCELLED) is True
    assert to.retryable(to.OUTCOME_NOT_STARTED) is True
    assert to.needs_human_review(to.OUTCOME_NOT_STARTED) is False


# ── 3. 恢复注入：两类分开 ────────────────────────────────────────────────
def test_resume_constants_carry_distinct_tags() -> None:
    assert to.classify(resume.UNKNOWN_RESULT) == to.OUTCOME_UNKNOWN
    assert to.classify(resume.NOT_STARTED_RESULT) == to.OUTCOME_NOT_STARTED


def test_replay_pending_distinguishes_started_and_never_started() -> None:
    messages = [{"role": "user", "content": "hi"}]
    out = resume.with_replay_pending(
        messages,
        ("call_started",),
        all_call_ids=("call_started", "call_never_started"),
    )
    by_id = {m.get("tool_call_id"): m["content"] for m in out if m.get("role") == "tool"}
    assert to.classify(by_id["call_started"]) == to.OUTCOME_UNKNOWN
    assert to.classify(by_id["call_never_started"]) == to.OUTCOME_NOT_STARTED


def test_replay_pending_skips_calls_that_already_have_results() -> None:
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    out = resume.with_replay_pending(messages, ("c1",), all_call_ids=("c1",))
    assert out == messages, "已有结果的调用不该被再补一条占位"


def test_replay_pending_noop_when_nothing_pending() -> None:
    messages = [{"role": "user", "content": "hi"}]
    assert resume.with_replay_pending(messages, (), all_call_ids=()) == messages


# ── 4. 文案仍然可读（模型是主要读者）─────────────────────────────────────
def test_bodies_stay_human_readable() -> None:
    unknown_body = to.strip_tag(resume.UNKNOWN_RESULT)
    not_started_body = to.strip_tag(resume.NOT_STARTED_RESULT)
    assert unknown_body.startswith("本次调用")
    assert not_started_body.startswith("本次调用")
    assert "副作用" in unknown_body and "副作用" in not_started_body
    assert unknown_body != not_started_body
