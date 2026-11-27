"""隔离档位必须**真的**改变执行路径 —— 而不是只改一个显示值。

## 这组测试要钉住什么

用户报告「三种方式没有任何区别」，根因是 `shell_exec` 无论选哪一档都走同一行
`create_subprocess_exec`：`wrap_async_process` 只判 `rank < Level.JOB`，于是
`job` / `low_il` 两档拿到的隔离措施完全相同。

`child_launch_argv` 把档位变成 **argv 的差异**，让"切换档位"等同于"改变执行
路径"。本文件钉住三件事：

1. `none` / `job` 不额外包一层（只有 Job Object，本来如此）；
2. `low_il` 及更高档位走自降级入口（否则等于没降级）；
3. `container` 不走 argv 改装路径（由容器后端直接起一次性容器）。
"""

from __future__ import annotations

from app.isolation import IsolationDecision, Level, child_launch_argv
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


def test_spawn_kwargs_never_leak_an_unconsumed_key():
    """`spawn_kwargs` 必须恒为空。

    历史上它塞过无人消费的 `_appcontainer=True`：真展开进
    `create_subprocess_exec(...)` 会 `TypeError`，被 `**kwargs` 吞掉则
    "档位显示在隔离里、进程其实不在"。这里不该再返回值。
    """
    from app.isolation import IsolationDecision, spawn_kwargs

    for level in (Level.NONE, Level.JOB, Level.LOW_IL):
        decision = IsolationDecision(level, level, [], False)
        assert spawn_kwargs(decision) == {}, f"{level.value} 不该返回额外 kwargs"

