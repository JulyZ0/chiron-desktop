"""G7：子 Agent 过程事件丢弃必须可见（指标 + 终态载荷 + 面板提示）。

修前会怎样：`EventSink.dropped` / `delivery_dropped` 只在内存里加一 —— 既不进
`/metrics`，也不出前端。面板上"过程少了几行"没有任何解释，排障时只能猜；而丢的恰好是
"过程"，最容易被误读成"模型没做这件事"。
"""

from __future__ import annotations

import inspect
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]


def _engine_src(rel: str) -> str:
    return (ENGINE / rel).read_text(encoding="utf-8")


# ── 1. 指标：两类丢弃分开计数 ──────────────────────────────────────────────
def test_metric_counter_exists_with_kind_label() -> None:
    src = _engine_src("app/observability/metrics.py")
    assert "SUBAGENT_EVENTS_DROPPED" in src
    assert "subagent_events_dropped_total" in src
    assert '"kind"' in src, "预览丢弃与投递丢弃必须分开（合成一个数字会掩盖性质差别）"


def test_drops_increment_the_counter() -> None:
    src = _engine_src("app/agent/event_sink.py")
    # 预算/容量丢弃 → preview；投递队列满 → delivery
    assert src.count('_count_drop("preview")') >= 2
    assert src.count('_count_drop("delivery")') >= 1


def test_counter_helper_never_breaks_the_stream() -> None:
    """观测是旁路：指标模块不可用时不能影响事件流。"""
    src = _engine_src("app/agent/event_sink.py")
    start = src.index("def _count_drop(")
    body = src[start : start + 400]
    assert "except Exception" in body


# ── 2. stats() 与终态载荷 ─────────────────────────────────────────────────
def test_stats_reports_both_counters() -> None:
    from app.agent.event_sink import EventSink

    sink = EventSink(session_id="s1")
    stats = sink.stats()
    assert set(stats) == {"dropped", "delivery_dropped", "pending"}
    assert all(isinstance(v, int) for v in stats.values())


def test_terminal_event_carries_dropped_stats() -> None:
    from app.agent.event_sink import EV_DONE, EventSink

    sink = EventSink(session_id="s1")
    sink.emit_started(run_id="r1")
    sink.emit_done(run_id="r1", status="completed", summary="ok")
    done = [e for e in sink.drain() if e.type == EV_DONE][0]
    payload = done.to_payload()
    assert "dropped" in payload, "丢弃统计必须挂在**必达**的终态上"
    assert payload["dropped"]["dropped"] == 0


def test_dropped_survives_as_a_real_count() -> None:
    """真的丢一条预览事件后，终态上的数字要跟着涨。"""
    from app.agent.event_sink import EV_DONE, EventSink

    # 容量 = 1 且合并窗口足够长（不同频道不会被合并）：
    # 第二条预览到达时队列已满 ⇒ 走"丢最旧 + 计数"那条路径。
    sink = EventSink(session_id="s1", maxsize=1, merge_window=60.0)
    sink.emit_started(run_id="r1")
    sink.emit_progress(run_id="r1", channel="subagent.text", content="a")
    sink.emit_progress(run_id="r1", channel="subagent.reasoning", content="b")
    sink.emit_done(run_id="r1", status="completed")
    done = [e for e in sink.drain() if e.type == EV_DONE][0]
    assert done.to_payload()["dropped"]["dropped"] > 0
    # 终态必须还在（丢弃只能发生在预览上）
    assert done.status == "completed"


# ── 3. 前端：类型与展示 ───────────────────────────────────────────────────
def test_frontend_type_has_dropped_field() -> None:
    src = _engine_src("../frontend-vue/src/api/subagent.ts")
    assert "dropped?:" in src


def test_panel_renders_dropped_note() -> None:
    src = _engine_src("../frontend-vue/src/components/chat/SubAgentPanel.vue")
    assert "droppedNote" in src
    assert "chat.subagent_events_dropped" in src
    # 两条来源都要看：实时事件 + 回放历史
    assert "props.liveEvents" in src and "historyEvents.value" in src
