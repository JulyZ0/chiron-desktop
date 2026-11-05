"""隔离档位必须**真的**改变执行路径 —— 而不是只改一个显示值。

## 这组测试要钉住什么

用户报告「三种方式没有任何区别」，根因是 `shell_exec` 无论选哪一档都走同一行
`create_subprocess_exec`：`wrap_async_process` 只判 `rank < Level.JOB`，于是
`job` / `low_il` / `appcontainer` 三档拿到的隔离措施完全相同。

`child_launch_argv` 把档位变成 **argv 的差异**，让"切换档位"等同于"改变执行
路径"。本文件钉住三件事：

1. `none` / `job` 不额外包一层（只有 Job Object，本来如此）；
2. `low_il` 及更高档位走自降级入口（否则等于没降级）；
3. `appcontainer` 在执行层未接入时**必须如实降级**，不许声称生效。
"""

from __future__ import annotations

import app.isolation as iso
from app.isolation import (
    FLOOR_LEVEL,
    IsolationDecision,
    Level,
    child_launch_argv,
    resolve_level,
)
from app.isolation import _low_il_launcher as launcher


def _decision(effective: Level, requested: Level | None = None) -> IsolationDecision:
    return IsolationDecision(requested or effective, effective, [])


def test_none_and_job_keep_the_command_untouched():
    """`job` 档只挂 Job Object，命令行不该被改写。"""
    for level in (Level.NONE, Level.JOB):
        command = ["cmd.exe", "/c", "echo hi"]
        assert child_launch_argv(_decision(level), command) == command


def test_low_il_wraps_the_command_with_the_level():
    """`low_il` 必须走自降级入口，且把档位带进去。

    没有这一步，父进程侧能做的只有 Job Object —— 与 `job` 档完全一样，
    这正是"三种方式没区别"的直接原因。
    """
    command = ["cmd.exe", "/c", "echo hi"]
    argv = child_launch_argv(_decision(Level.LOW_IL), command)

    assert argv != command
    assert argv[0].endswith(("python.exe", "python"))
    assert argv[1].endswith("_low_il_launcher.py")
    assert "--level=low_il" in argv
    # 分隔符之后必须是原始命令，逐字保留
    separator = argv.index("--")
    assert argv[separator + 1 :] == command


def test_higher_levels_also_route_through_the_launcher():
    """`restricted` / `appcontainer` 若走到这里，同样不能被当成 `job` 处理。"""
    command = ["whoami"]
    for level in (Level.RESTRICTED, Level.APPCONTAINER):
        argv = child_launch_argv(_decision(level), command)
        assert f"--level={level.value}" in argv
        assert argv[argv.index("--") + 1 :] == command


def test_empty_command_is_returned_as_is():
    """空命令没有可执行体，原样返回，不构造出半截 argv。"""
    assert child_launch_argv(_decision(Level.LOW_IL), []) == []


# ── launcher 自身的入口解析 ──


def test_split_argv_separates_own_args_from_command():
    own, command = launcher._split_argv(["--level=low_il", "--", "cmd.exe", "/c", "dir"])
    assert own == ["--level=low_il"]
    assert command == ["cmd.exe", "/c", "dir"]


def test_split_argv_without_separator_has_no_command():
    """缺 `--` 时不能把参数当成命令执行 —— 宁可拒绝。"""
    own, command = launcher._split_argv(["--level=low_il"])
    assert own == ["--level=low_il"]
    assert command == []


def test_split_argv_keeps_dashes_inside_the_command():
    """命令里的 `--`（如 `npm run build -- --watch`）不能被误判成自己的分隔符。"""
    own, command = launcher._split_argv(["--level=low_il", "--", "npm", "run", "build", "--", "--watch"])
    assert own == ["--level=low_il"]
    assert command == ["npm", "run", "build", "--", "--watch"]


def test_level_of_reads_the_flag():
    assert launcher._level_of(["--level=low_il"]) == "low_il"
    assert launcher._level_of([]) == ""


def test_launcher_puts_engine_root_on_sys_path():
    """以文件路径启动时 `sys.path[0]` 是 `app/isolation`，必须补回引擎根。

    沙箱子进程的 `sandboxed_env()` 清空了 `PYTHONPATH`，所以这一步是
    `import app.isolation` 能否成功的唯一保障。
    """
    import sys
    from pathlib import Path

    saved = list(sys.path)
    try:
        sys.path = [p for p in sys.path if "chiron" not in p.lower()]
        launcher._ensure_engine_on_path()
        engine_root = Path(launcher.__file__).resolve().parents[2]
        assert str(engine_root) in sys.path
        assert Path(str(engine_root)).name == "python-engine"
    finally:
        sys.path = saved


# ── appcontainer 不许谎报 ──


def test_appcontainer_never_claims_to_take_effect(monkeypatch):
    """执行层没接入时，`appcontainer` 必须如实降级到 `low_il` 并给出原因。

    原来这里 `return IsolationDecision(want, want, reasons)`，于是状态接口、前端
    面板与审计日志一致声称 appcontainer 生效，而实际执行的仍是 low_il。
    """
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    # 让 AppContainer 机制本身"可用"，才能真正走到执行层这一步
    monkeypatch.setattr(iso.win, "ensure_appcontainer", lambda _n: (True, "S-1-15-2-1", ""))
    monkeypatch.setattr(iso.win, "grant_appcontainer_path", lambda _sid, _p: (True, ""))

    decision = resolve_level(Level.APPCONTAINER)

    assert decision.requested is Level.APPCONTAINER
    assert decision.effective is Level.LOW_IL
    assert decision.degraded is True
    # 降级理由必须说清"执行层尚未接入"，而不是含糊的"不可用"
    assert any("执行层尚未接入" in reason for reason in decision.reasons)
    # 仍在硬底线之上，不是"退回无隔离"
    assert decision.effective.rank >= FLOOR_LEVEL.rank
    assert decision.below_floor is False
