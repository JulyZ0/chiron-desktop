"""P1-4：压缩"过快重复"熔断 + 压缩后回读最近读过的文件。

修前有两类不报错、只烧钱的形态：
1. 上下文刚压完又被同一批工具结果填满 —— 于是每一步都触发压缩，摘要调用无限循环；
2. 压缩把文件正文摘要掉，而长任务下一步还要改那个文件，模型只能重新 read_file。

判据：熔断在第 3 次"间隔 < 3 工具步"时暂停自动压缩（手动 /compact 可重置）；
read_file 的结果有界保留（5 个文件 / 每个 1500 字），压缩后以稳定标记注入，重压替换。
"""

from __future__ import annotations

import app.main  # noqa: F401 — 初始化 app 包
from app.agent.runtime import (
    _RECENT_FILES_MARKER,
    AgentRuntime,
    _strip_recent_files,
)


def _runtime() -> AgentRuntime:
    rt = AgentRuntime.__new__(AgentRuntime)
    rt._tool_steps = 0
    rt._compaction_rapid = 0
    rt._compaction_last_step = -1
    rt._compaction_disabled_reason = ""
    rt._recent_reads = {}
    return rt


def test_compaction_breaker_trips_after_three_rapid_refills():
    rt = _runtime()
    rt._tool_steps = 1
    rt._note_compaction(force=False)
    rt._tool_steps = 2
    rt._note_compaction(force=False)
    assert rt._compaction_disabled_reason == "", "两次还不该停"

    rt._tool_steps = 3
    rt._note_compaction(force=False)
    assert rt._compaction_rapid == 3
    assert "暂停自动压缩" in rt._compaction_disabled_reason


def test_slow_compaction_resets_the_counter():
    rt = _runtime()
    rt._tool_steps = 1
    rt._note_compaction(force=False)
    rt._tool_steps = 10  # 中间跑了 9 步：不是"刚压完又满"
    rt._note_compaction(force=False)
    assert rt._compaction_rapid == 1
    assert rt._compaction_disabled_reason == ""


def test_manual_compaction_rearms_auto():
    rt = _runtime()
    for step in (1, 2, 3):
        rt._tool_steps = step
        rt._note_compaction(force=False)
    assert rt._compaction_disabled_reason

    rt._tool_steps = 4
    rt._note_compaction(force=True)
    assert rt._compaction_rapid == 0
    assert rt._compaction_disabled_reason == ""


def test_disabled_auto_compaction_returns_unchanged(monkeypatch):
    rt = _runtime()
    rt._compaction_disabled_reason = "paused for test"
    messages = [{"role": "user", "content": "hi"}]
    out, event = rt._compact_with_notice(messages, None, {}, force=False)  # type: ignore[arg-type]
    assert out is messages
    assert event is None


def test_remember_read_is_bounded_deduped_and_ignores_failures():
    rt = _runtime()
    call = {"name": "read_file", "arguments": '{"path": "a.py"}'}
    rt._remember_read(call, {"content": "x" * 2000})
    assert len(rt._recent_reads["a.py"]) == 1500

    # 同名路径后写覆盖先写，并移到最新
    rt._remember_read(call, {"content": "new"})
    assert rt._recent_reads["a.py"] == "new"

    # 失败/未执行/非 read_file 都不记
    rt._remember_read(call, {"error": "boom"})
    rt._remember_read(call, {"not_executed": True})
    rt._remember_read(
        {"name": "grep_files", "arguments": '{"path": "b.py"}'}, {"content": "hits"}
    )
    assert rt._recent_reads["a.py"] == "new"

    # 最多 5 个，先进先出
    for i in range(7):
        rt._remember_read(
            {"name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'},
            {"content": f"v{i}"},
        )
    assert len(rt._recent_reads) == 5
    assert "f0.py" not in rt._recent_reads and "f6.py" in rt._recent_reads


def test_recent_files_message_uses_stable_marker_and_strip_replaces():
    rt = _runtime()
    assert rt._recent_files_message() is None
    rt._recent_reads = {"a.py": "aaa", "b.py": "bbb"}
    msg = rt._recent_files_message()
    assert msg is not None
    assert msg["role"] == "system"
    assert str(msg["content"]).startswith(_RECENT_FILES_MARKER)
    assert "--- a.py ---\naaa" in str(msg["content"])

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "system", "content": f"{_RECENT_FILES_MARKER} old"},
        {"role": "system", "content": "other system"},
    ]
    stripped = _strip_recent_files(messages)
    assert len(stripped) == 2
    assert all(_RECENT_FILES_MARKER not in str(m.get("content")) for m in stripped)
