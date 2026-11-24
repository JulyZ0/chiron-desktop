"""Git 工具集 — 通过 subprocess 执行常用 git 命令。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)

_GIT_SAFE_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("log.showSignature", "false"),
    ("gpg.program", ""),
    ("core.sshCommand", ""),
    ("core.gitProxy", ""),
    ("core.askPass", ""),
    ("protocol.allow", "never"),
)
_GIT_ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"})


def _git_environment() -> dict[str, str]:
    from app.tools.sandbox import sandboxed_env

    env = {key: value for key, value in sandboxed_env().items() if key in _GIT_ENV_KEYS}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_PROTOCOL_FROM_USER": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return env


def _config_overrides(config_stdout: str) -> tuple[tuple[str, str], ...]:
    """Disable every repository-declared filter and text conversion command."""
    filter_drivers: dict[str, None] = {}
    diff_drivers: dict[str, None] = {}
    for entry in config_stdout.split("\0"):
        if not entry:
            continue
        key, separator, _value = entry.partition("\n")
        if not separator or not key:
            raise ValueError("malformed git configuration listing")
        lowered = key.lower()
        attribute = lowered.rsplit(".", 1)[-1]
        if lowered.startswith("filter.") and attribute in {"clean", "smudge", "process"}:
            driver = key[len("filter.") : -(len(attribute) + 1)]
            if not driver:
                raise ValueError("malformed git filter configuration")
            filter_drivers[driver] = None
        elif lowered.startswith("diff.") and attribute in {"textconv", "command"}:
            driver = key[len("diff.") : -(len(attribute) + 1)]
            if not driver:
                raise ValueError("malformed git diff configuration")
            diff_drivers[driver] = None

    overrides: list[tuple[str, str]] = []
    for driver in sorted(filter_drivers):
        overrides.extend((f"filter.{driver}.{attribute}", "") for attribute in ("clean", "smudge", "process"))
        overrides.append((f"filter.{driver}.required", "false"))
    for driver in sorted(diff_drivers):
        overrides.extend((f"diff.{driver}.{attribute}", "") for attribute in ("textconv", "command"))
    return tuple(overrides)


async def _run_git(
    *args: str,
    timeout: int = 30,
    config_overrides: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    """Run a git command in the sandbox workspace and return stdout/stderr/exit_code.

    使用 sandboxed_env 清理环境变量，防止宿主密钥泄露给 git 子进程。
    超时控制防止 git 命令卡住。

    **cwd 刻意不可传**：git 命令一律在工作区内执行（此前有个 `cwd` 参数被传进来又被忽略，
    是"参数说谎"的一种）。逃逸检查见 :func:`_run_git_guarded`。

    **审计**：这是模型可触达的执行路径之一（参数由模型给出），与
    `shell_exec` / `run_code` / `persistent_shell` / `tool_job` 同记一本账 —— 此前唯独它没有痕迹。
    """
    from app.tools.exec_audit import (
        OUTCOME_ERROR,
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        record_execution,
    )
    from app.tools.sandbox import isolation_exec_argv, workspace_dir

    started = time.monotonic()
    cmd = ["git", "--no-pager"]
    for key, value in (*_GIT_SAFE_CONFIG, *config_overrides):
        cmd.extend(("-c", f"{key}={value}"))
    cmd.extend(args)
    # 按隔离档位包装：git 能改工作区外的一切（`-C`、hooks、`core.pager`），
    # 不包装就等于给了一条绕过档位的执行入口。
    try:
        exec_argv = isolation_exec_argv(cmd)
    except Exception as exc:  # noqa: BLE001 — fail-closed，不退回无隔离执行
        record_execution(
            tool="git",
            command=" ".join(cmd),
            outcome=OUTCOME_ERROR,
            reason=f"isolation unavailable: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": f"isolation unavailable: {exc}"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *exec_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace_dir()),
            env=_git_environment(),
        )
    except Exception as exc:  # noqa: BLE001 — 启动失败按探测失败处理，不继续执行
        record_execution(
            tool="git",
            command=" ".join(cmd),
            outcome=OUTCOME_ERROR,
            reason=f"git process unavailable: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": f"git process unavailable: {exc}"}
    # Windows：挂进 Job Object。git 会起 `git-remote-https`、编辑器、hook 等子树，
    # 墙钟 timeout 只能 kill 直接子进程 —— Chiron 退出时那些孙进程会成为孤儿。
    from app.isolation import COMMAND_LIMITS, attach_job

    _job = attach_job(proc, COMMAND_LIMITS, label="git")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        record_execution(
            tool="git",
            command=" ".join(cmd),
            outcome=OUTCOME_TIMEOUT,
            reason=f"timeout after {timeout}s",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": "timeout", "timeout": timeout}
    exit_code = proc.returncode
    record_execution(
        tool="git",
        command=" ".join(cmd),
        outcome=OUTCOME_OK if exit_code == 0 else OUTCOME_ERROR,
        exit_code=exit_code,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def _probe_git_config() -> tuple[tuple[tuple[str, str], ...], dict[str, Any] | None]:
    result = await _run_git("config", "--includes", "--null", "--list")
    if "error" in result or result.get("exit_code") != 0:
        detail = result.get("stderr", "").strip() or result.get("error", "unknown git error")
        error: dict[str, Any] = {"error": f"git repository configuration probe failed: {detail}"}
        if "exit_code" in result:
            error["exit_code"] = result["exit_code"]
        return (), error
    config_stdout = result.get("stdout")
    if not isinstance(config_stdout, str):
        return (), {"error": "git repository configuration probe failed: invalid git output"}
    try:
        return _config_overrides(config_stdout), None
    except ValueError as exc:
        return (), {"error": f"git repository configuration probe failed: {exc}"}


async def _ensure_sandbox_repo() -> dict[str, Any] | None:
    """校验 git 仓库根没有**向上穿透**到工作根之外。

    两种工作根，同一条断言：

    - 未绑定项目时，工作根是沙箱 workspace。沙箱若位于宿主 git 仓库内，git 命令
      会向上穿透到宿主 `.git` —— 这里用 `rev-parse --show-toplevel` 挡住。
    - 绑定了项目时，工作根就是**用户的项目目录**。此时要求仓库根**恰好等于**工作根：
      若项目根是 monorepo 的子目录，`top` 会落在父层，git 操作会作用到整个仓库 ——
      这正是 D6「当前仅适配单仓库目录」要拒绝的形态。

    返回 None 表示通过；否则返回错误 dict。
    """
    from app.tools.sandbox import project_root, workspace_dir

    ws = os.path.normpath(str(workspace_dir()))
    check = await _run_git("rev-parse", "--show-toplevel")
    if "error" in check:
        return {"error": f"git repository probe failed: {check['error']}"}
    if check.get("exit_code") != 0:
        stderr = check.get("stderr", "").strip()
        if check.get("exit_code") == 128 and "not a git repository" in stderr.lower():
            return None
        return {"error": f"git repository probe failed: {stderr or 'unknown git error'}"}
    top = os.path.normpath(check.get("stdout", "").strip())
    if not top or not os.path.isabs(top):
        return {"error": "git repository probe failed: invalid repository root"}
    if top != ws:
        # 绑定项目时要求仓库根**恰为**项目根：项目根若落在 monorepo 子目录，top 会
        # 升到父层，git 操作将作用到整个仓库 —— 这是 D6「仅适配单仓库目录」要拒的。
        # 沙箱模式保留旧行为：agent 在 workspace 的子目录里自建仓库是合法的，
        # 需要拦的只是"向上穿透到 workspace 之外"。
        if project_root() is not None or not top.startswith(ws + os.sep):
            return {"error": f"git repository root does not match workspace: {top} != {ws}"}
    return None


async def _run_git_guarded(*args: str, timeout: int = 30) -> dict[str, Any]:
    """**所有** git 工具的统一入口：先过逃逸检查，再执行。

    为什么必须统一：此前只有 `git_status` 调了 `_ensure_sandbox_repo`，而
    `git_diff` / `git_log` / `git_commit` / `git_branch` 直接执行 —— 工作区位于宿主
    仓库内时，它们会**向上穿透**：`git_diff` 泄露宿主改动、`git_commit` 直接在宿主仓库
    提交。检查放在入口而不是逐个工具里，是为了"新增 git 工具时不会再漏"。
    """
    guard = await _ensure_sandbox_repo()
    if guard:
        return guard
    overrides, probe_error = await _probe_git_config()
    if probe_error:
        return probe_error
    return await _run_git(*args, timeout=timeout, config_overrides=overrides)


async def git_status() -> dict[str, Any]:
    """Return git status (modified, added, deleted files)."""
    result = await _run_git_guarded("status", "--porcelain")
    if "error" in result:
        return result
    # NOTE: do NOT .strip() the whole stdout — that eats the leading space of the
    # first porcelain line (e.g. " M file" becomes "M file", breaking the offset).
    lines = [ln.rstrip("\r") for ln in result["stdout"].splitlines() if ln.strip()]
    files: list[dict[str, str]] = []
    for line in lines:
        # Porcelain format: XY PATH  (XY = 2-char status, separated by a space from the path)
        if len(line) < 4:
            continue
        status = line[:2].strip()
        path = line[3:]
        files.append({"status": status, "path": path})
    return {
        "exit_code": result["exit_code"],
        "count": len(files),
        "files": files,
        "raw": result["stdout"].strip(),
    }


async def git_diff(staged: bool = False) -> dict[str, Any]:
    """Return diff of changes. If staged=True, shows staged changes."""
    args = ["diff", "--no-ext-diff", "--no-textconv"]
    if staged:
        args.append("--cached")
    result = await _run_git_guarded(*args)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "diff": result["stdout"],
        "staged": staged,
    }


async def git_log(limit: int = 10) -> dict[str, Any]:
    """Return recent commits."""
    result = await _run_git_guarded("log", "--oneline", f"-{limit}")
    if "error" in result:
        return result
    lines = [ln for ln in result["stdout"].strip().splitlines() if ln]
    commits: list[dict[str, str]] = []
    for line in lines:
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
    return {"exit_code": result["exit_code"], "count": len(commits), "commits": commits}


async def git_commit(message: str) -> dict[str, Any]:
    """Stage all changes and commit with the given message."""
    # Stage all
    stage = await _run_git_guarded("add", "-A")
    if "error" in stage:
        return stage
    if stage["exit_code"] != 0:
        return {"error": stage["stderr"].strip(), "exit_code": stage["exit_code"]}

    # Commit
    result = await _run_git_guarded("commit", "-m", message)
    if "error" in result:
        return result
    return {
        "exit_code": result["exit_code"],
        "message": message,
        "output": (result["stdout"] + result["stderr"]).strip(),
    }


async def git_branch() -> dict[str, Any]:
    """List branches and show current branch."""
    result = await _run_git_guarded("branch")
    if "error" in result:
        return result
    lines = [ln for ln in result["stdout"].strip().splitlines() if ln]
    current = ""
    branches: list[str] = []
    for line in lines:
        name = line.strip()
        if name.startswith("* "):
            name = name[2:]
            current = name
        branches.append(name)
    return {"exit_code": result["exit_code"], "current": current, "branches": branches}


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
registry.register(
    name="git_status",
    description="Show working-tree status inside the sandbox workspace (modified/added/deleted files).",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=git_status,
)

registry.register(
    name="git_diff",
    description="Show the diff of uncommitted changes in the sandbox workspace.",
    parameters={
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged (--cached) changes instead of the working tree",
                "default": False,
            },
        },
        "required": [],
    },
    handler=git_diff,
)

registry.register(
    name="git_log",
    description="List recent commits in the sandbox workspace.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max commits to return (default 10)",
                "default": 10,
            },
        },
        "required": [],
    },
    handler=git_log,
)

registry.register(
    name="git_commit",
    description=(
        "Stage all changes in the sandbox workspace and commit them. "
        "Only the sandbox repository is touched — a workspace nested inside a host repo "
        "is refused rather than committing to the host."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
        },
        "required": ["message"],
    },
    handler=git_commit,
)

registry.register(
    name="git_branch",
    description="List branches and show the current branch in the sandbox workspace.",
    parameters={"type": "object", "properties": {}, "required": []},
    handler=git_branch,
)
