"""G2 + G3：子 Agent 轮数上限单一真源、生命周期 7 阶段齐全。"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ENGINE / rel).read_text(encoding="utf-8")


# ── G2：轮数上限只有一个真源 ────────────────────────────────────────────────
def test_max_turns_cap_is_single_source() -> None:
    """工具 schema、工具侧夹断、runner 侧夹断都引用同一个常量。"""
    tool_src = _src("app/tools/subagent.py")
    runner_src = _src("app/agent/subagent_runner.py")

    assert re.search(r"MAX_TURNS_CAP\s*=\s*\d+", tool_src), "常量定义不见了"
    assert "from app.tools.subagent import MAX_TURNS_CAP" in runner_src, (
        "runner 必须引用同一常量（否则又会出现 40 vs 10 的漂移）"
    )
    assert not re.search(r"min\([^)]*,\s*10\s*\)", runner_src), (
        "runner 里不该再出现写死的 10"
    )


def test_schema_maximum_matches_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型看到的 `maximum` 必须等于实际夹断值。"""
    from app.tools import subagent as tool_mod

    schema_src = inspect.getsource(tool_mod)
    assert '"maximum": MAX_TURNS_CAP' in schema_src, (
        "schema 的 maximum 必须引用常量，否则模型会被许诺一个跑不到的轮数"
    )
    # 夹断函数本身
    assert tool_mod.MAX_TURNS_CAP > 0
    for line in schema_src.splitlines():
        if "max_turns=max(1, min(int(max_turns" in line:
            assert "MAX_TURNS_CAP" in line


def test_cap_comment_explains_the_choice() -> None:
    """10 是**有意**的收敛值 —— 理由必须在代码里，否则后人会顺手对齐到 40。"""
    src = _src("app/tools/subagent.py")
    head = src[src.index("MAX_TURNS_CAP") - 600 : src.index("MAX_TURNS_CAP") + 40]
    assert "有意" in head or "deliberate" in head.lower()


# ── G3：生命周期 7 个阶段都真的会被发出来 ──────────────────────────────────
def test_all_phases_declared() -> None:
    from app.subagent.lifecycle import PHASES

    assert len(PHASES) == 7


def test_running_phase_is_emitted() -> None:
    """修前 `PHASE_RUNNING` 只有定义、从不 emit —— 看板看不到"子真的跑起来了"。"""
    runner_src = _src("app/agent/subagent_runner.py")
    assert "PHASE_RUNNING" in runner_src, "runner 未导入 PHASE_RUNNING"
    assert re.search(r"phase=PHASE_RUNNING", runner_src), "runner 未发出 child_running"


def test_running_phase_emitted_before_the_loop() -> None:
    """落点必须在进入主循环**之前**：之前失败=没跑起来，之后失败=跑中失败。"""
    runner_src = _src("app/agent/subagent_runner.py")
    running_at = runner_src.index("phase=PHASE_RUNNING")
    loop_at = runner_src.index("async for evt in runtime.run(child)")
    assert running_at < loop_at, "child_running 必须在运行主循环之前发出"
