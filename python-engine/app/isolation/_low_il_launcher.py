"""把一条命令放进 Low 完整性级别下执行 —— `low_il` 档的落地入口。

## 为什么必须是一个独立入口

Windows 的完整性级别只能改**当前进程的令牌**，且 ``SetTokenInformation``
没有撤销 API（这是设计，不是缺陷：降级不可逆）。父进程若调用它，被降级的
就是引擎自身 —— 之后所有工具都在 Low IL 下跑，且重启才能恢复。

所以"降级"只能发生在真正执行命令的那个子进程里：父进程负责把它请起来并
带上档位，子进程在执行任何命令之前先把自己降下去。

## 为什么子进程不直接 exec

``os.execv*`` 在 Windows 上是"另起一个进程再退出"，新进程拿到的是父进程
**原始**令牌，Low IL 会丢失 —— 看起来包装了、实际没降级。所以这里用
``subprocess.call``：子进程从本进程**继承**令牌，Low IL 得以保留。

## 用法

父进程侧构造（见 ``child_launch_argv``）：:

    python -m app.isolation._low_il_launcher --level=low_il -- <command> [args...]

降级失败时 ``apply_in_child`` 会 ``os._exit(9)``，本入口**不兜底** ——
「以为自己被隔离的子进程」比没有隔离更危险。
"""

from __future__ import annotations

import os
import subprocess
import sys

#: 与 ``apply_in_child`` 的失败退出码保持一致，便于上层归因。
EXIT_ISOLATION_FAILED = 9

SEPARATOR = "--"


def _ensure_engine_on_path() -> None:
    """把引擎根目录放回 ``sys.path``。

    本入口由父进程以**文件路径**启动（见 ``child_launch_argv``），此时
    ``sys.path[0]`` 是 ``app/isolation/``；而沙箱子进程的 ``sandboxed_env()``
    又刻意清空了 ``PYTHONPATH``（防宿主模块注入）。两者叠加的结果是
    ``import app.isolation`` 直接失败 —— 表现为"命令报 ImportError 而不是被
    隔离执行"，且因为降级没发生就退出，看上去像沙箱坏了。

    ``app/isolation/_low_il_launcher.py`` 的上两级正是引擎根。
    """
    engine_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)


def _split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """拆成「本入口自己的参数」与「要执行的命令」。"""
    for index, item in enumerate(argv):
        if item == SEPARATOR:
            return argv[:index], argv[index + 1 :]
    return argv, []


def _level_of(own_args: list[str]) -> str:
    for item in own_args:
        if item.startswith("--level="):
            return item.split("=", 1)[1]
    return ""


def main(argv: list[str] | None = None) -> int:
    own_args, command = _split_argv(list(sys.argv[1:] if argv is None else argv))

    if not command:
        sys.stderr.write("[isolation] low_il launcher: 缺少要执行的命令\n")
        return 2

    # 必须在 import app.isolation 之前：见 _ensure_engine_on_path 的说明。
    _ensure_engine_on_path()

    from app.isolation import apply_in_child, parse_level

    try:
        level = parse_level(_level_of(own_args))
    except ValueError as exc:
        sys.stderr.write(f"[isolation] FATAL: {exc}\n")
        return 2

    # 顺序关键：降级必须在执行命令**之前**。失败时 apply_in_child 直接 os._exit(9)。
    apply_in_child(level)

    # 子进程继承本进程（已降级）的令牌，因此同为 Low IL。
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        sys.stderr.write(f"[isolation] 无法找到可执行文件: {command[0]}\n")
        return 127
    except OSError as exc:
        sys.stderr.write(f"[isolation] 执行失败: {exc}\n")
        return 126


if __name__ == "__main__":
    sys.exit(main())
