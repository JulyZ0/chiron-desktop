"""方案 A：审批模式随 run 现场持久化（不恢复服务端存储）。

**背景**（见 `app/agent/guards.py::resolve_tools_mode` 的 docstring）：模式曾是服务端存储
（Redis `session:mode:*`，由 `/v1/mode` 写），于是前端选择、Redis、runtime 三份状态并存 ——
任一处不一致就表现为"设置存了却不生效"，因此被删掉。

删掉的代价是：**无人值守自动续跑时前端不在场**，模式没有来源，只能回落 `auto` ——
用户设的 `ask` / `yolo` 在续跑那一段静默失效。方案 A 不复活全局存储，而是让模式
**随 run 现场一起落盘**（`task_meta.llm_config` 本就整体保存，`tools_mode` 正在其中）。

本文件锁住这条链路上的三个环节。任何一个漏掉字段，模式都会静默回落 `auto`，
而现场表现只是"我明明设了 yolo，它却一直在问我"—— 没有任何报错可循。
"""

from __future__ import annotations

from typing import Any

from app.agent.guards import SESSION_MODE_ASK, SESSION_MODE_AUTO, SESSION_MODE_YOLO, resolve_tools_mode
from app.agent.resume import rebuild_task
from app.agent.runtime import AgentTask, _resume_task_meta


def _task(*, tools_mode: str | None) -> AgentTask:
    llm_config: dict[str, Any] = {"model": "deepseek-chat"}
    if tools_mode is not None:
        llm_config["tools_mode"] = tools_mode
    return AgentTask(
        id="turn-1",
        tenant_id=1,
        user_id="u1",
        session_id="s1",
        content="hi",
        llm_config=llm_config,
        run_token="rt-1",
    )


def _snapshot(meta: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [], "turn_index": 0, "task_meta": meta}


def _resume(meta: dict[str, Any]) -> AgentTask:
    task = rebuild_task(
        tenant_id=1, session_id="s1", snapshot=_snapshot(meta), run_token="rt-2"
    )
    assert task is not None, "快照里带了 task_meta 就必须能重建，否则续跑整段丢失"
    return task


def test_save_step_keeps_tools_mode() -> None:
    """落盘环节：`_resume_task_meta` 必须把 tools_mode 带进 task_meta。"""
    meta = _resume_task_meta(_task(tools_mode=SESSION_MODE_YOLO))
    assert meta["llm_config"]["tools_mode"] == SESSION_MODE_YOLO


def test_rebuild_step_keeps_tools_mode() -> None:
    """重建环节：`rebuild_task` 必须把 tools_mode 原样带回 AgentTask。"""
    task = _resume(_resume_task_meta(_task(tools_mode=SESSION_MODE_ASK)))
    assert task.llm_config["tools_mode"] == SESSION_MODE_ASK


def test_resumed_mode_is_what_run_would_use() -> None:
    """解析环节：`run()` 用 `resolve_tools_mode(llm_config.tools_mode)` 定模式，
    因此端到端等于"续跑那一段仍按用户当初选的模式裁决"。"""
    for mode in (SESSION_MODE_ASK, SESSION_MODE_YOLO):
        task = _resume(_resume_task_meta(_task(tools_mode=mode)))
        assert resolve_tools_mode(task.llm_config.get("tools_mode")) == mode


def test_absent_tools_mode_falls_back_to_auto() -> None:
    """旧快照（提交时就没带 tools_mode）不能被续跑放大成 yolo。"""
    task = _resume(_resume_task_meta(_task(tools_mode=None)))
    assert resolve_tools_mode(task.llm_config.get("tools_mode")) == SESSION_MODE_AUTO


def test_invalid_tools_mode_in_snapshot_is_fail_closed() -> None:
    """被写坏 / 被篡改的快照不能让续跑放开全部工具：非法值一律回落 auto。"""
    meta = _resume_task_meta(_task(tools_mode=SESSION_MODE_YOLO))
    meta["llm_config"]["tools_mode"] = "god-mode"
    task = _resume(meta)
    assert resolve_tools_mode(task.llm_config.get("tools_mode")) == SESSION_MODE_AUTO


def test_persisted_mode_actually_changes_the_resumed_verdict() -> None:
    """持久化的意义：续跑那一段的**裁决结果**跟着变，而不只是字段还在。

    只看"字段是否保留"是无效证据 —— 字段留着却没人用它，现场表现与丢失完全一样。
    这里直接拿重建出的 mode 去问 `requires_confirmation`，断言写操作的处置确实不同。
    """
    from app.agent.tool_policy import WRITE, requires_confirmation

    def _needs_confirm(mode: str) -> bool:
        task = _resume(_resume_task_meta(_task(tools_mode=mode)))
        resumed_mode = resolve_tools_mode(task.llm_config.get("tools_mode"))
        return requires_confirmation(WRITE, resumed_mode)

    # ask：写操作要人点；yolo：不需要。两种选择在续跑段必须仍然可区分。
    assert _needs_confirm(SESSION_MODE_ASK) is True
    assert _needs_confirm(SESSION_MODE_YOLO) is False
    # auto：写操作免确认（只有 delete / external 要）—— 与 ask 分属两侧，
    # 这条同时钉住"回落 auto"不会意外变成 yolo 那种全放行。
    assert _needs_confirm(SESSION_MODE_AUTO) is False


def test_yolo_skips_confirmation_but_keeps_the_second_check() -> None:
    """yolo 的真实语义是**两件事**，不能混为一谈。

    `requires_confirmation` 在 yolo 下对一切返回 False（`tool_policy.py:180`，用户显式
    选择跳过人工确认）；但不可逆 / 对外可见的操作仍被 `requires_second_check` 拉住，
    必须在执行前向 Broker 要一次服务端授权 —— 那一层**与模式无关**，yolo 也不例外。

    模式持久化的边界就在这里：它把"要不要弹窗问人"这件事带过续跑，但带不动、
    也不该带走服务端授权。把两者混同，才会写出"yolo 下还能删任意东西"的错误结论。
    """
    from app.agent.tool_policy import DELETE, EXTERNAL, requires_confirmation, requires_second_check

    # yolo：不弹窗（用户选过了）
    assert requires_confirmation(DELETE, SESSION_MODE_YOLO) is False
    assert requires_confirmation(EXTERNAL, SESSION_MODE_YOLO) is False
    # 但服务端授权照旧：这一层不随模式放宽
    assert requires_second_check(DELETE) is True
    assert requires_second_check(EXTERNAL) is True
    # 对照：write 级可回滚，不需要二次校验
    assert requires_second_check("write") is False
