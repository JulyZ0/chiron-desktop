"""折叠摘要：提取式 → 模型摘要的升级链与形状校验。

背景：`_prune_messages` 是同步纯函数（还被当 `on_overflow` 同步回调传入），算不了模型
摘要，只能先写一条提取式摘要（每条取首行）。而这条 `digest` 会**顶替原文**参与此后
每一次上下文重建 —— 提取式说得清"这段在谈什么"，说不出"为什么这么决定、还差什么"，
这正是"压缩后像换了个人"的主因。

升级放在 async 层（`AgentRuntime._upgrade_fold_digests`），**写库之前**就地改写；
模型输出必须过形状校验（长度 / 占位套话 / 技术锚点），不合格一律保留提取式：
一份敷衍或幻觉的摘要比没有摘要危害大得多（模型会当真，且无从发现原文已被丢掉）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.compaction import (
    DIGEST_MAX_CHARS,
    CompactionConfig,
    _apply_folds,
    _digest_anchors,
    _digest_prompt,
    _digest_source,
    _prune_messages,
    _validate_digest,
    covered_prefix_digest,
    install_fold_projection,
    make_fold_lineage,
    projection_source_digest,
)
from app.agent.runtime import AgentRuntime
from app.gateway.provider import ChatResponse

#: 一段合格的摘要：够长，且点了区间里真实存在的技术标记。
GOOD_DIGEST = (
    "用户在修沙箱路径判定：workspace 内的绝对路径被 safe_join 误拒，"
    "已改为 commonpath 包含判定（app/tools/sandbox.py）。"
    "已跑 `python -m pytest tests/test_sandbox_isolation.py` 并通过；"
    "下一步：确认 Windows 与 Linux 两条路径分支都有覆盖。"
)


def _big_source() -> list[dict[str, Any]]:
    """一个**跨过折叠经济性门槛**的区间（`DIGEST_MIN_FOLD_TOKENS`）。

    `_range()` 只够验证"序列化保留了角色"，却小到不值得发一次摘要调用 ——
    而 `_upgrade_fold_digests` 现在会拒绝这种区间（Reasonix `foldEconomics` 同款）。
    升级路径的测试必须用够大的区间，否则测的是门槛而不是升级。
    """
    return [{**m, "content": (m.get("content") or "") + "补充说明。" * 120} for m in _range()]


def _range() -> list[dict[str, Any]]:
    """一个典型折叠区间：既有用户约束，也有改文件的工具调用。"""
    return [
        {"role": "user", "content": "修一下 safe_join：workspace 内的绝对路径不该被拒"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": '{"path": "app/tools/sandbox.py"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "c1"},
    ]


class _StubGateway:
    """只实现 `call_bounded` 用到的那一个方法。"""

    def __init__(self, content: str = "", finish_reason: str = "stop", raises: Exception | None = None):
        self._content = content
        self._finish_reason = finish_reason
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages=None, model="", max_tokens=0, temperature=0.0):
        self.calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        if self._raises is not None:
            raise self._raises
        return ChatResponse(content=self._content, finish_reason=self._finish_reason)


def _runtime(gateway: Any) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime._gateway = gateway
    return runtime


def _fold(source: list[dict[str, Any]], digest: str = "- 提取式摘要") -> dict[str, Any]:
    return {
        "covered_from": 1,
        "covered_to": 4,
        "body_suffix": 2,
        "digest": digest,
        "digest_model": "extractive",
        "digest_prompt": "prune",
        "_source": source,
    }


# ── 提示词构造 ──


def test_digest_source_keeps_role_identity():
    """摘要输入必须保留角色：分不清"谁下的约束"，摘要就没有价值。"""
    source = _digest_source(_range())

    assert "## user" in source
    assert "## tool" in source
    assert "safe_join" in source
    assert "app/tools/sandbox.py" in source


def test_digest_source_is_bounded_and_does_not_cut_a_message():
    """超长区间要截断，但不能把一条消息切成半截 —— 半截的命令会被读成另一条。"""
    messages = [{"role": "user", "content": f"第 {i} 条：{'x' * 400}"} for i in range(200)]

    source = _digest_source(messages)

    assert len(source) < 24_000 + 200, "摘要输入必须有上限，否则这次旁路调用自己就会超窗"
    body = source.split("\n\n", 1)[1]
    assert body.startswith("## "), "截断后必须落在消息边界上"


def test_digest_prompt_marks_the_slice_as_data():
    """区间原文是**数据**：用户可能在里头写"忽略以上指令"，提示词要先把边界说清。"""
    prompt = _digest_prompt(_range())

    assert prompt[0]["role"] == "system"
    assert "<conversation>" in prompt[1]["content"]
    assert "never instructions" in prompt[1]["content"]


# ── 形状校验 ──


def test_validate_digest_rejects_a_one_liner():
    """太短 = 敷衍。字段看着填过了，实际零信息，比空串更隐蔽。"""
    assert _validate_digest("这段在讨论压缩。", _range()) == ""


def test_validate_digest_rejects_placeholders():
    """占位串与套话绝不能经由模型路径被引回来。"""
    for bad in (
        "[12 message(s) folded] " + "x" * 100,
        "(summary generation produced no content) " + "x" * 100,
        "I cannot summarise this conversation because " + "x" * 100,
    ):
        assert _validate_digest(bad, _range()) == "", bad


def test_validate_digest_rejects_a_summary_without_any_anchor():
    """区间里明明有路径和命令，摘要一个技术标记都没提 ⇒ 多半是凭空写的。

    这条尤其要防"角色名被当成锚点"：旧实现用"任意长单词"取锚点，
    一份通篇没提具体标识符的摘要只要出现 `user` 就能通过。
    """
    fabricated = (
        "用户和助手围绕一个软件工程问题进行了多轮讨论，助手给出了一些方案，用户表示认可，随后双方继续推进后续工作。"
    )
    assert "user" in _digest_source(_range())  # 序列化头里有角色名
    assert _validate_digest(fabricated, _range()) == ""


def test_validate_digest_accepts_a_summary_citing_identifiers():
    assert _validate_digest(GOOD_DIGEST, _range()) == GOOD_DIGEST


def test_validate_digest_caps_length():
    """超长不算错误，但必须夹住：它是转交单，不是第二份记录。"""
    long_digest = GOOD_DIGEST + "补" * (DIGEST_MAX_CHARS * 2)

    assert len(_validate_digest(long_digest, _range())) == DIGEST_MAX_CHARS


def test_anchors_ignore_role_headers_but_keep_identifiers():
    anchors = _digest_anchors(_range())

    assert "safe_join" in anchors
    assert "app/tools/sandbox.py" in anchors
    assert "user" not in anchors
    assert "assistant" not in anchors


# ── 升级链（async 层） ──


@pytest.mark.asyncio
async def test_upgrade_replaces_extractive_with_model_summary():
    gateway = _StubGateway(content=GOOD_DIGEST)
    folds = [_fold(_big_source())]

    await _runtime(gateway)._upgrade_fold_digests(folds, model="deepseek-chat")

    assert folds[0]["digest"] == GOOD_DIGEST
    assert folds[0]["digest_model"] == "deepseek-chat", "来源标注必须如实，读方据此判断可信度"
    assert folds[0]["digest_prompt"] == "prune-model"
    assert "_source" not in folds[0], "区间原文不能留在 fold 里（日志/回溯会带上整段历史）"
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_upgrade_keeps_extractive_when_the_call_fails():
    """旁路失败绝不能拖垮本轮，也不能把摘要清空。"""
    gateway = _StubGateway(raises=RuntimeError("provider 503"))
    folds = [_fold(_big_source())]

    await _runtime(gateway)._upgrade_fold_digests(folds)

    assert folds[0]["digest"] == "- 提取式摘要"
    assert folds[0]["digest_model"] == "extractive"
    assert "_source" not in folds[0]


@pytest.mark.asyncio
async def test_upgrade_keeps_extractive_when_the_summary_is_fabricated():
    gateway = _StubGateway(content="用户和助手围绕一个软件工程问题进行了多轮讨论，助手给出了一些方案，用户表示认可。")
    folds = [_fold(_big_source())]

    await _runtime(gateway)._upgrade_fold_digests(folds)

    assert folds[0]["digest_model"] == "extractive", "未通过形状校验的摘要不得写库"


@pytest.mark.asyncio
async def test_upgrade_skips_calling_the_model_without_a_gateway():
    folds = [_fold(_big_source())]

    await _runtime(None)._upgrade_fold_digests(folds)

    assert folds[0]["digest_model"] == "extractive"
    assert "_source" not in folds[0], "没能力升级也要清掉原文"


@pytest.mark.asyncio
async def test_upgrade_only_summarises_the_newest_fold():
    """多条 fold 时不串行烧钱：旧区间保留提取式，但原文同样要清掉。"""
    gateway = _StubGateway(content=GOOD_DIGEST)
    folds = [_fold(_big_source()), _fold(_big_source())]

    await _runtime(gateway)._upgrade_fold_digests(folds)

    assert len(gateway.calls) == 1
    assert folds[0]["digest_model"] == "extractive"
    assert folds[1]["digest_model"] != "extractive"
    assert all("_source" not in f for f in folds)


# ── 与裁剪路径的衔接 ──


def test_prune_fold_carries_the_range_source_for_upgrade():
    """fold 记录必须把区间原文带出同步层，否则 async 层再也取不回来。"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for i in range(8):
        messages.append({"role": "user", "content": f"问题 {i} 关于 safe_join", "seq": i * 2 + 1})
        messages.append({"role": "assistant", "content": f"回答 {i}", "seq": i * 2 + 2})
    folds: list[dict[str, Any]] = []

    _prune_messages(messages, CompactionConfig(max_messages=4), folds=folds)

    assert len(folds) == 1
    source = folds[0]["_source"]
    assert source, "同步层没带出原文，模型摘要就无从生成"
    assert "问题 0" in _digest_source(source)
    assert folds[0]["digest_model"] == "extractive", "同步层只能写提取式，标注要如实"


# ── H15：fold lineage、covered-prefix digest 与投影安装 CAS ──

LINEAGE_PROVIDER = "openai"
LINEAGE_MODEL = "gpt-test"
LINEAGE_WINDOW = 128_000


def _lineage_messages(count: int = 6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for seq in range(1, count + 1):
        out.append(
            {
                "id": f"m{seq}",
                "role": "user" if seq % 2 else "assistant",
                "content": f"内容 {seq}",
                "seq": seq,
            }
        )
    return out


def _trusted_fold(
    messages: list[dict[str, Any]],
    frm: int = 1,
    to: int = 4,
    body_suffix: int = 3,
) -> dict[str, Any]:
    return {
        "covered_from": frm,
        "covered_to": to,
        "body_suffix": body_suffix,
        "digest": "- [user] 早前要点",
        "digest_model": "extractive",
        "lineage": make_fold_lineage(
            messages,
            frm,
            to,
            provider=LINEAGE_PROVIDER,
            model=LINEAGE_MODEL,
            context_window=LINEAGE_WINDOW,
        ),
    }


def _apply_trusted(messages: list[dict[str, Any]], folds: list[dict[str, Any]]):
    return _apply_folds(
        messages,
        folds,
        provider=LINEAGE_PROVIDER,
        lineage_model=LINEAGE_MODEL,
        context_window=LINEAGE_WINDOW,
    )


def test_prune_records_complete_fold_lineage_and_prefix_digest():
    messages = _lineage_messages(8)
    folds: list[dict[str, Any]] = []

    _prune_messages(
        messages,
        CompactionConfig(
            max_messages=3,
            provider=LINEAGE_PROVIDER,
            model=LINEAGE_MODEL,
            context_window=LINEAGE_WINDOW,
        ),
        folds=folds,
    )

    assert len(folds) == 1
    lineage = folds[0]["lineage"]
    assert lineage["version"] == 1
    assert lineage["provider"] == LINEAGE_PROVIDER
    assert lineage["model"] == LINEAGE_MODEL
    assert lineage["context_window"] == LINEAGE_WINDOW
    assert lineage["covered_from"] == 1
    assert lineage["covered_to"] == 7
    assert lineage["covered_prefix_digest"] == covered_prefix_digest(messages, 1, 7)
    assert _apply_trusted(messages, folds) is not None


@pytest.mark.parametrize("force_entry", [False, True])
def test_runtime_compaction_records_effective_lineage(monkeypatch: pytest.MonkeyPatch, force_entry: bool):
    from app.agent import runtime as runtime_mod

    captured: dict[str, CompactionConfig] = {}
    real_compact = runtime_mod._compact_messages

    def recording_compact(messages, cfg=None, **kwargs):
        captured["cfg"] = cfg
        return real_compact(messages, cfg, **kwargs)

    monkeypatch.setattr(runtime_mod, "_compact_messages", recording_compact)
    runtime = AgentRuntime(gateway=None)
    mode = SimpleNamespace(
        compaction={
            "strategy": "prune",
            "max_messages": 3,
            "provider": "stale-provider",
            "context_window": 1_000,
        }
    )
    messages = _lineage_messages(8)
    folds: list[dict[str, Any]] = []

    if force_entry:
        runtime._force_compact(
            messages,
            mode,
            {"provider": LINEAGE_PROVIDER},
            window=256_000,
            max_output=8_000,
            model=LINEAGE_MODEL,
            folds=folds,
        )
    else:
        runtime._compact_with_notice(
            messages,
            mode,
            {"provider": LINEAGE_PROVIDER},
            window=256_000,
            max_output=8_000,
            model=LINEAGE_MODEL,
            folds=folds,
        )

    cfg = captured["cfg"]
    assert cfg.provider == LINEAGE_PROVIDER
    assert cfg.context_window == 256_000
    assert folds[0]["lineage"]["provider"] == LINEAGE_PROVIDER
    assert folds[0]["lineage"]["model"] == LINEAGE_MODEL
    assert folds[0]["lineage"]["context_window"] == 256_000


def test_prefix_digest_is_canonical_and_covers_message_identity():
    messages = _lineage_messages(4)
    reordered_keys = [dict(reversed(list(message.items()))) for message in messages]

    assert covered_prefix_digest(messages, 1, 4) == covered_prefix_digest(reordered_keys, 1, 4)
    changed = [dict(message) for message in messages]
    changed[1]["id"] = "another-message"
    assert covered_prefix_digest(changed, 1, 4) != covered_prefix_digest(messages, 1, 4)


@pytest.mark.parametrize("field", ["provider", "model", "context_window"])
def test_projection_rejects_lineage_mismatch(field: str):
    messages = _lineage_messages()
    fold = _trusted_fold(messages)
    original = fold["lineage"][field]
    fold["lineage"][field] = original + 1 if isinstance(original, int) else f"{original}-other"

    assert _apply_trusted(messages, [fold]) is None


@pytest.mark.parametrize("field", ["content", "id", "tool_calls"])
def test_projection_rejects_changed_covered_prefix(field: str):
    messages = _lineage_messages()
    fold = _trusted_fold(messages)
    messages[1][field] = "changed"

    assert _apply_trusted(messages, [fold]) is None


def test_legacy_fold_without_lineage_is_readable_but_not_trusted():
    messages = _lineage_messages()
    fold = _trusted_fold(messages)
    fold.pop("lineage")

    assert fold["digest"] == "- [user] 早前要点"
    assert _apply_trusted(messages, [fold]) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("version", 999), ("covered_prefix_digest", "0" * 64), ("provider", "")],
)
def test_projection_rejects_incomplete_or_unknown_lineage(field: str, value: Any):
    messages = _lineage_messages()
    fold = _trusted_fold(messages)
    fold["lineage"][field] = value

    assert _apply_trusted(messages, [fold]) is None


def test_projection_install_cas_rejects_stale_source_without_mutating_it():
    messages = _lineage_messages()
    folds = [_trusted_fold(messages)]
    expected = projection_source_digest(messages)
    messages.append({"id": "m7", "role": "user", "content": "new", "seq": 7})
    before = list(messages)

    assert (
        install_fold_projection(
            messages,
            folds,
            expected_source_digest=expected,
            provider=LINEAGE_PROVIDER,
            model=LINEAGE_MODEL,
            context_window=LINEAGE_WINDOW,
        )
        is None
    )
    assert messages == before


def test_projection_install_cas_swaps_only_a_matching_snapshot():
    messages = _lineage_messages()
    folds = [_trusted_fold(messages)]
    expected = projection_source_digest(messages)
    original_id = id(messages)

    installed = install_fold_projection(
        messages,
        folds,
        expected_source_digest=expected,
        provider=LINEAGE_PROVIDER,
        model=LINEAGE_MODEL,
        context_window=LINEAGE_WINDOW,
    )

    assert installed is messages
    assert id(messages) == original_id
    assert "早前要点" in messages[1]["content"]
    assert [m["seq"] for m in messages[2:]] == [4, 5, 6]


def test_concurrent_projection_install_has_exactly_one_cas_winner():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    messages = _lineage_messages()
    folds = [_trusted_fold(messages)]
    expected = projection_source_digest(messages)
    barrier = Barrier(2)

    def _install():
        barrier.wait()
        return install_fold_projection(
            messages,
            folds,
            expected_source_digest=expected,
            provider=LINEAGE_PROVIDER,
            model=LINEAGE_MODEL,
            context_window=LINEAGE_WINDOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _install(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert "早前要点" in messages[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate_during_claim", [False, True])
async def test_runtime_projection_install_uses_database_cas(monkeypatch: pytest.MonkeyPatch, mutate_during_claim: bool):
    import app.agent.runtime as runtime_module
    from app.agent import history_writer

    monkeypatch.setattr(runtime_module, "MAX_CONTEXT_TOKENS", 0)
    messages = _lineage_messages()
    folds = [_trusted_fold(messages)]
    claim = SimpleNamespace(
        claim_id="claim-1",
        session_id="s1",
        tenant_id=1,
        source_digest=projection_source_digest(messages),
        message_revision=7,
        folds=folds,
    )
    completed: list[str] = []
    aborted: list[str] = []

    async def _claim(session_id, tenant_id, source, *, expected_source_digest):
        assert session_id == "s1" and tenant_id == 1
        assert expected_source_digest == projection_source_digest(source)
        if mutate_during_claim:
            source.append({"id": "m7", "role": "user", "content": "new", "seq": 7})
            return None
        return claim

    async def _complete(c, *, projection_digest):
        completed.append(c.claim_id)
        return True

    async def _abort(c):
        aborted.append(c.claim_id)

    monkeypatch.setattr(history_writer, "claim_fold_projection", _claim)
    monkeypatch.setattr(history_writer, "complete_fold_projection", _complete)
    monkeypatch.setattr(history_writer, "abort_fold_projection", _abort)
    runtime = object.__new__(AgentRuntime)
    runtime._context_window = LINEAGE_WINDOW
    task = SimpleNamespace(
        session_id="s1",
        tenant_id=1,
        llm_config={"provider": LINEAGE_PROVIDER, "model": LINEAGE_MODEL},
    )

    projected = await runtime._project_over_budget(messages, task, LINEAGE_MODEL)

    assert projected is messages
    if mutate_during_claim:
        assert len(messages) == 8
        assert "早前要点" not in messages[1]["content"]
        assert completed == []
    else:
        assert len(messages) == 5
        assert "早前要点" in messages[1]["content"]
        assert completed == ["claim-1"]
    assert aborted == []


@pytest.mark.asyncio
async def test_runtime_projection_without_provider_aborts_database_claim(
    monkeypatch: pytest.MonkeyPatch,
):
    import app.agent.runtime as runtime_module
    from app.agent import history_writer
    from app.tools import context as tool_context

    monkeypatch.setattr(runtime_module, "MAX_CONTEXT_TOKENS", 0)
    monkeypatch.setattr(tool_context, "get_tool_context", lambda *_args: "")
    messages = _lineage_messages()
    folds = [_trusted_fold(messages)]
    claim = SimpleNamespace(
        claim_id="claim-1",
        session_id="s1",
        tenant_id=1,
        source_digest=projection_source_digest(messages),
        message_revision=7,
        folds=folds,
    )
    completed: list[str] = []
    aborted: list[str] = []

    async def _claim(*_args, **_kwargs):
        return claim

    async def _complete(c, *, projection_digest):
        completed.append(c.claim_id)
        return True

    async def _abort(c):
        aborted.append(c.claim_id)

    monkeypatch.setattr(history_writer, "claim_fold_projection", _claim)
    monkeypatch.setattr(history_writer, "complete_fold_projection", _complete)
    monkeypatch.setattr(history_writer, "abort_fold_projection", _abort)
    runtime = object.__new__(AgentRuntime)
    runtime._context_window = LINEAGE_WINDOW
    task = SimpleNamespace(
        session_id="s1",
        tenant_id=1,
        llm_config={"model": LINEAGE_MODEL},
    )
    before = list(messages)

    projected = await runtime._project_over_budget(messages, task, LINEAGE_MODEL)

    assert projected is messages
    assert messages == before
    assert completed == []
    assert aborted == ["claim-1"]
