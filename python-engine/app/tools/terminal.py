"""persistent_shell 工具 — 持久终端会话（对应 deepseek-harness tool-bash-persistent）

每个 session 一个长驻 shell 进程：cwd、导出变量、激活环境、函数、后台任务
跨调用持久。

实现要点：非交互 shell 经管道 stdout 是全缓冲（4KB，readline 收不到小输出，
deepseek 用 PTY 解决），故命令输出重定向到临时文件、用哨兵文件标记完成，
规避缓冲问题且保持 shell 状态持久。超时/崩溃时关闭 shell，下次调用重建
（与 deepseek 的 reset 语义一致）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.tools.context import get_session_id
from app.tools.registry import registry
from app.tools.sandbox import (
    _rlimit_kwargs,
    check_command_text,
    sandboxed_env,
    workspace_dir,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120


def _shell_command() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/bash")


class PersistentTerminal:
    """进程内持久 shell 管理器，按 session key 隔离。"""

    def __init__(self) -> None:
        self._procs: dict[str, Any] = {}
        #: 与 `_procs` 同键的 Job 句柄。**必须持有到进程结束**：句柄一关
        #: `KILL_ON_JOB_CLOSE` 立刻杀光整棵树，提前 GC 会让 shell 莫名消失。
        self._jobs: dict[str, Any] = {}
        #: `restricted` 档下的受限会话（持有进程/线程/管道句柄，必须显式 close）。
        self._sessions: dict[str, Any] = {}
        #: 每个 shell 启动时**实际**生效的档位。`execute()` 要据此决定哨兵文件落在
        #: 哪里：`low_il` 及以上写不了 Medium 目录（含系统 `%TEMP%`），哨兵写不出来
        #: 就永远等不到完成标记 ⇒ 每条命令都跑满 timeout，看起来像 shell 卡死。
        self._levels: dict[str, Any] = {}

    def _needs_workspace_scratch(self, key: str) -> bool:
        """该 key 下的 shell 是否只能写在 workspace 内。

        判据取自**上次启动该 shell 时实际生效的档位**，而不是当前配置值 ——
        配置可以在 shell 存活期间被热切（`PUT /isolation`），但已经起来的进程
        令牌不会变。按配置值判断会在切换后让老 shell 的哨兵落到它写不了的目录。

        `low_il` 及以上都返回 True：完整性检查是 no-write-up，Low IL 进程写不了
        Medium 目录（系统 `%TEMP%` 即是）；`restricted` 的受限令牌更是只授权了
        工作区。无记录（未启动过 / 走了异常分支）时按 False 处理，保持原行为。
        """
        level = self._levels.get(key)
        if level is None:
            return key in self._sessions
        try:
            from app.isolation import Level

            return level.rank >= Level.LOW_IL.rank
        except Exception:  # noqa: BLE001 — 判不出来时退回旧判据，不阻断命令执行
            return key in self._sessions

    def _prune_procs(self) -> None:
        """回收已退出的进程条目，防止 _procs 无限累积（S 资源修复）。"""
        dead = [
            k
            for k, p in self._procs.items()
            if p is not None and p.returncode is not None
        ]
        for k in dead:
            self._procs.pop(k, None)
            self._levels.pop(k, None)
            session = self._sessions.pop(k, None)
            if session is not None:
                # 受限会话持有进程/管道句柄和两个排空线程，必须显式释放，
                # 否则每轮 `npm test` 都会漏掉一组内核句柄。
                session.close()
            job = self._jobs.pop(k, None)
            if job is not None:
                job.close()

    async def _get_proc(self, key: str) -> Any:
        proc = self._procs.get(key)
        if proc is not None and proc.returncode is None:
            return proc
        # 回收已退出进程，再创建新的（避免累积完成/崩溃的 shell）
        self._prune_procs()
        # `restricted` 档走**受限常驻会话**（`win_launch.open_restricted_session`）：
        # 受限令牌 + 独立桌面 + 自建可继承管道。此前的 fail-closed 是因为一次性
        # `launch_restricted` 无法"常驻 + 增量写 stdin"，那个缺口已由常驻模式补上。
        #
        # 注意**不能**在这里退回普通 `create_subprocess_exec`：那会让 persistent_shell
        # 成为绕过受限令牌的后门 —— "配置声称挡得住写、实际有一条路绕过去"。
        decision = None
        try:
            from app.isolation import Level, resolve_level

            decision = resolve_level()
        except Exception as exc:  # noqa: BLE001 — 隔离模块异常时按既有路径走
            logger.warning("persistent_shell: resolve_level failed: %s", exc)
        if decision is not None and decision.effective in (
            Level.RESTRICTED,
            Level.APPCONTAINER,
        ):
            return await self._open_self_built_session(key, decision.effective)
        # S 修复：ws 用局部变量，避免写成实例属性后被并发请求覆盖导致跨会话串 cwd/cd
        ws = str(workspace_dir())
        shell_exe = _shell_command()
        # **按档位改变执行路径**：`low_il` 及以上必须由子进程自己降级（完整性级别
        # 改的是当前进程令牌且不可撤销，父进程一动手连引擎一起降）。此前这里直接
        # 起 shell，于是 `job` / `low_il` / `appcontainer` 拿到的隔离措施完全相同 ——
        # 用户切档位、面板也如实显示新档位，实际强度却没变。
        shell_argv = [shell_exe]
        if decision is not None:
            try:
                from app.isolation import child_launch_argv

                shell_argv = child_launch_argv(decision, shell_argv)
            except Exception as exc:  # noqa: BLE001
                # fail-closed：需要隔离却装不上隔离层时拒绝起 shell，不退回无隔离路径
                # （否则 persistent_shell 会变成绕过档位的后门）。
                raise RuntimeError(
                    f"persistent_shell 无法按 {decision.effective.value} 档启动：{exc}"
                ) from exc
        proc = await asyncio.create_subprocess_exec(
            *shell_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=ws,
            # S 安全修复：清理宿主 env(API key/JWT_SECRET/internal token 等)，
            # 仅保留 PATH/HOME 等基础变量并重定向到沙箱，防止模型 `env` 外带密钥。
            env=sandboxed_env(),
            # S5-1：长驻 shell 此前**没有资源限制**（一次性 shell_exec 走 run_in_sandbox
            # 现在也有了）。POSIX 下施加 RLIMIT；Windows 无 resource，靠调用方超时兜底。
            **_rlimit_kwargs(),
        )
        # Windows：把长驻 shell 挂进 Job Object。此前这里**没有任何进程树约束** ——
        # 模型在持久 shell 里起一个后台任务，调用方超时只能 kill 掉 shell 本身，
        # 孙进程留在内存里继续跑。job 与 proc 同生命周期（见 `_discard` / `_prune_procs`）。
        job = None
        try:
            from app.isolation import SHELL_LIMITS, resolve_level, wrap_async_process

            job = wrap_async_process(proc, resolve_level(), SHELL_LIMITS)
        except Exception as exc:  # noqa: BLE001 — 附加失败不应让 shell 起不来
            logger.warning("persistent_shell: job object attach failed: %s", exc)
        # 首命令 cd 到沙箱 workspace，保证状态持久在隔离目录内（S 安全修复）
        # stdin 显式用了 PIPE，这里只是把类型收窄（Popen 的返回类型是 StreamWriter | None）。
        assert proc.stdin is not None
        try:
            if sys.platform == "win32":
                proc.stdin.write(f'cd /d "{ws}"\n'.encode())
            else:
                proc.stdin.write(f'cd "{ws}"\n'.encode())
            await proc.stdin.drain()
        except Exception:
            pass
        # 同 key 的旧 shell 已被上面的 _prune_procs 清过；若它仍在跑（比如并发替换），
        # 先关旧 job —— 否则旧句柄泄漏，且旧进程树永远不会被回收。
        stale = self._jobs.pop(key, None)
        if stale is not None:
            stale.close()
        self._procs[key] = proc
        self._levels[key] = decision.effective if decision is not None else None
        if job is not None:
            self._jobs[key] = job
        return proc

    async def _open_self_built_session(self, key: str, level: Any) -> Any:
        """`restricted` / `appcontainer` 档：起一个**自建**常驻 shell。

        这两档都必须自建进程，因为 `asyncio.create_subprocess_exec` 表达不了它们的
        建进程参数（受限令牌要 `CreateProcessAsUserW`，AppContainer 要进程属性列表）。
        起好之后对外暴露的接口与普通 `Process` 一致，后续 `execute` 走同一套逻辑。

        内部全是同步 Win32 调用，必须丢线程，否则会卡住事件循环 ——
        一次建进程就能拖住所有会话。
        """
        from app.isolation import SHELL_LIMITS, Level, win_launch, wrap_async_process

        shell_exe = _shell_command()
        ws = str(workspace_dir())
        if level is Level.APPCONTAINER:
            opener = win_launch.open_appcontainer_session
            hint = "请改用 shell_exec，或把隔离档位调到 low_il。"
        else:
            opener = win_launch.open_restricted_session
            hint = "请改用 shell_exec，或把隔离档位调到 low_il。"
        session, err = await asyncio.to_thread(
            opener,
            [shell_exe],
            cwd=ws,
            env=sandboxed_env(),
        )
        if session is None:
            # fail-closed：起不来就拒绝，**不**退回无隔离的普通 shell。
            # 退回等于让 persistent_shell 成为绕过该档位的后门。
            raise RuntimeError(
                f"persistent_shell 在 {level.value} 档下无法启动受限进程：{err}。{hint}"
            )

        # Job Object 同样要挂：令牌/包 SID 只约束权限，不约束进程树规模。
        job = None
        try:
            from app.isolation import resolve_level

            job = wrap_async_process(session, resolve_level(), SHELL_LIMITS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("persistent_shell(%s): job attach failed: %s", level.value, exc)

        # 首命令 cd：shell 的起始目录已在 cwd 里给了，这里只是让后续
        # 命令与普通路径保持同样的工作目录语义（用户可能 cd 到子目录）。
        try:
            session.stdin.write(f'cd /d "{ws}"\n'.encode())
            await session.stdin.drain()
        except Exception:
            pass

        stale = self._sessions.pop(key, None)
        if stale is not None:
            stale.close()
        stale_job = self._jobs.pop(key, None)
        if stale_job is not None:
            stale_job.close()
        self._procs[key] = session
        self._sessions[key] = session
        # 记录**实际**档位：`execute` 据此决定临时目录落在哪、以及是否需要
        # 走 workspace 内的哨兵文件（low_il/appcontainer 写不了系统 %TEMP%）。
        self._levels[key] = level
        if job is not None:
            self._jobs[key] = job
        return session

    async def execute(
        self, key: str, command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        proc = await self._get_proc(key)

        # 准入检查：逃逸拦截 + **可执行名白名单**，与 shell_exec 同一套规则。
        # S5-1：此前只做逃逸拦截（没有白名单），于是 `persistent_shell("rm -rf x")`
        # 与 `shell_exec` 的判定不一致；且本进程**长驻**，缺口比一次性 shell 更危险。
        reason = check_command_text(command)
        if reason:
            return {
                "output": f"[blocked: {reason}] command not allowed in sandbox",
                "exit_code": -1,
                "persistent": True,
            }

        # 每命令一个临时工作目录：out=输出 / code=退出码 / done=完成哨兵
        #
        # **必须落在 workspace 内**，只要 shell 的档位在 `low_il` 及以上：
        #
        # * `restricted`：受限进程只被授权写工作区；
        # * `low_il`：Windows 的完整性检查是 no-write-up —— Low IL 进程写不了 Medium
        #   目录，而系统 `%TEMP%` 正是 Medium。此前这里只看 `key in self._sessions`
        #   （即只照顾 restricted），于是 `low_il` 档下 `mkdtemp` 建在 `%TEMP%`，哨兵
        #   写不出来 ⇒ `done_file` 永不出现 ⇒ 每条命令都跑满 timeout 才返回"超时"。
        #   命令本身其实成功了，输出却读不到，看起来像 shell 卡死。
        #
        # 实测依据：low_il 子进程在打了 Low 标签的 workspace 内 mkdtemp + 写
        # out/code/done 三个文件全部成功，写 `%TEMP%`（sandbox_dir 父层之外）则
        # PermissionError(13)。
        base = str(workspace_dir()) if self._needs_workspace_scratch(key) else None
        workdir = Path(tempfile.mkdtemp(prefix="chiron_sh_", dir=base))
        out_file = workdir / "out.txt"
        code_file = workdir / "code.txt"
        done_file = workdir / "done.txt"

        if sys.platform == "win32":
            lines = (
                f'{command} > "{out_file}" 2>&1',
                f'echo %errorlevel% > "{code_file}"',
                f'type nul > "{done_file}"',
            )
        else:
            lines = (
                f'{command} > "{out_file}" 2>&1',
                f'echo $? > "{code_file}"',
                f'touch "{done_file}"',
            )

        # stdin 显式用了 PIPE，这里只是把类型收窄。
        assert proc.stdin is not None
        try:
            proc.stdin.write("\n".join(lines).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            await self._discard(key)
            return {
                "output": "[shell exited — reset]",
                "exit_code": -1,
                "persistent": False,
                "reset": True,
            }

        # 轮询完成哨兵
        elapsed = 0.0
        while elapsed < timeout:
            if done_file.exists():
                break
            if proc.returncode is not None:
                await self._discard(key)
                return {
                    "output": f"[shell exited: code {proc.returncode} — reset]",
                    "exit_code": proc.returncode or 0,
                    "persistent": False,
                    "reset": True,
                }
            await asyncio.sleep(0.05)
            elapsed += 0.05
        else:
            # 超时：关闭不确定的 shell，下次重建
            await self._discard(key)
            partial = (
                out_file.read_text(encoding="utf-8", errors="replace")
                if out_file.exists()
                else ""
            )
            return {
                "output": (partial + "\n" if partial else "")
                + f"[timed out after {timeout}s — shell reset]",
                "exit_code": -1,
                "persistent": False,
                "reset": True,
            }

        output = (
            out_file.read_text(encoding="utf-8", errors="replace")
            if out_file.exists()
            else ""
        )
        exit_code = 0
        try:
            exit_code = int(code_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pass

        # 临时目录清理：防止 /tmp 长期累积（S10 修复）
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

        return {"output": output, "exit_code": exit_code, "persistent": True}

    async def _discard(self, key: str) -> None:
        proc = self._procs.pop(key, None)
        job = self._jobs.pop(key, None)
        session = self._sessions.pop(key, None)
        self._levels.pop(key, None)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        # 先 kill 再关 job：反过来会让 KILL_ON_JOB_CLOSE 抢在 wait() 之前动手，
        # 那段 await 就永远等不到一个「正常退出」的进程。
        if job is not None:
            job.close()
        # 受限会话的管道/线程句柄必须显式释放（`_procs` 里的同一个对象）。
        if session is not None:
            session.close()

    async def close_all(self) -> None:
        for key in list(self._procs.keys()):
            await self._discard(key)


# 进程内单例
_terminal = PersistentTerminal()


async def persistent_shell(
    command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Run *command* in the session's persistent shell.

    cwd, exported variables, activated environments, functions and background
    jobs persist across calls in the same session. A timeout or shell exit
    resets the shell; the next call starts fresh (the result says so).
    """
    if not command.strip():
        return {"error": "command is required"}
    key = get_session_id() or "default"

    # S5-3：审计 —— 在**单一出口**记录，覆盖"被拦下 / 超时 / 成功 / 失败"四类结果
    import time

    from app.tools.exec_audit import (
        OUTCOME_BLOCKED,
        OUTCOME_ERROR,
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        record_execution,
    )

    started = time.monotonic()
    result = await _terminal.execute(key, command, timeout=timeout)
    output = str(result.get("output", ""))
    if output.startswith("[blocked"):
        outcome = OUTCOME_BLOCKED
    elif "timed out" in output:
        outcome = OUTCOME_TIMEOUT
    elif result.get("error"):
        outcome = OUTCOME_ERROR
    elif result.get("exit_code") == 0:
        outcome = OUTCOME_OK
    else:
        outcome = OUTCOME_ERROR

    raw_exit = result.get("exit_code")
    record_execution(
        tool="persistent_shell",
        command=command,
        outcome=outcome,
        exit_code=raw_exit if isinstance(raw_exit, int) else None,
        reason=output[:200] if outcome == OUTCOME_BLOCKED else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


registry.register(
    name="persistent_shell",
    description=(
        "Run a command in a persistent shell shared across calls in this session. "
        "State (cwd, exports, activated venv, functions, background jobs) persists "
        "between calls — use `cd` once and it stays. Prefer this over shell_exec "
        "for multi-step work. "
        "The command's stdout/stderr are captured and returned as `output`; "
        "do NOT use `>`/`2>` redirection to files inside the command (it is "
        "captured by the tool) — save files with the write_file tool instead. "
        "A timeout or shell crash resets the shell and the next call starts fresh."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT_SECONDS,
                "description": "Seconds before the shell is reset",
            },
        },
        "required": ["command"],
    },
    handler=persistent_shell,
)
