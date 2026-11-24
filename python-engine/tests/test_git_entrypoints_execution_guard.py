"""H25 攻击探针：Git 仓库配置不得借**任何入口**执行程序。

PRD 9.7 H25 的验收是四句，这里逐句落成可复现的攻击：

1. **所有 Git 入口使用固定安全 flags、动态 filter 覆盖、禁用 hooks/外部 helper** ——
   `git_status/diff/log/commit` 已有覆盖（`test_git_tools_security.py`），本文件补的是
   **`shell_exec` 里的 `git`**（此前只挡了 clone/push，恶意仓库的 `core.fsmonitor`
   / `filter.*.clean` / hooks / `core.pager` 在一条只读命令里照样执行）与
   **长驻 shell 的 `GIT_CONFIG_*` 环境**（`persistent_shell` 里敲的 git 改不了 argv）。
2. **仓库配置探测失败时拒绝执行** —— 探测不出配置就不敢声称覆盖了 filter。
3. **模型给出的 git 参数不得把安全配置重新打开** —— `-c core.fsmonitor=...` 之类。
4. **设备端与引擎端口径一致** —— `frontend-vue/electron/device-fs.cjs` 的固定安全配置
   必须覆盖引擎的每一个键（只读断言：口径漂移即失败）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.tools import exec_audit, git_tools, sandbox


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """沙箱工作区 = 一个**恶意仓库**：四处都埋了"执行即留 marker"的配置。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(sandbox, "workspace_dir", lambda: repo)
    monkeypatch.setattr(sandbox, "project_root", lambda: None)
    monkeypatch.setattr(sandbox, "_sandbox_tmp_dir", lambda: temp)
    monkeypatch.setattr(sandbox, "isolation_exec_argv", lambda command: list(command))
    monkeypatch.setattr("app.isolation.attach_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", tmp_path / "audit")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Git Entry Test")
    _git(repo, "config", "user.email", "git-entry@example.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _probe(tmp_path: Path) -> tuple[Path, str]:
    """生成一个"跑一次就留 marker"的探针命令（跨平台：用当前解释器执行脚本）。"""
    marker = tmp_path / "executed.marker"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}"'
    return marker, command


def _poison(repo: Path, tmp_path: Path) -> Path:
    """往仓库配置/hook 里埋满"只读操作也会执行"的入口，返回 marker 路径。"""
    marker, command = _probe(tmp_path)
    _git(repo, "config", "core.fsmonitor", command)
    _git(repo, "config", "core.pager", command)
    _git(repo, "config", "filter.malicious.clean", command)
    _git(repo, "config", "filter.malicious.smudge", command)
    _git(repo, "config", "filter.malicious.required", "true")
    _git(repo, "config", "diff.malicious.textconv", command)
    (repo / ".gitattributes").write_text(
        "* filter=malicious diff=malicious\n", encoding="utf-8"
    )
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for hook in ("pre-commit", "post-index-change", "pre-status"):
        (hooks / hook).write_text(f"#!/bin/sh\n{command}\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    return marker


def _probe_is_live(command: str, marker: Path) -> None:
    """自检：探针本身确实会留 marker（否则下面的"没执行"断言全是空转）。"""
    assert marker.exists() is False
    subprocess.run(command, shell=True, check=False, timeout=30)
    assert marker.exists(), "probe command itself does not work — assertions would be vacuous"
    marker.unlink()


@pytest.mark.asyncio
async def test_shell_exec_git_never_executes_repository_config(
    git_repo: Path, tmp_path: Path
) -> None:
    """攻击探针：`shell_exec("git ...")` 在恶意仓库里不得执行任何配置/钩子程序。"""
    marker = _poison(git_repo, tmp_path)
    _, command = _probe(tmp_path)
    _probe_is_live(command, marker)

    status = await sandbox.run_in_sandbox("git status --porcelain")
    diff = await sandbox.run_in_sandbox("git diff")
    staged = await sandbox.run_in_sandbox("git add -A")
    committed = await sandbox.run_in_sandbox("git commit -m shell-exec")

    for name, result in (
        ("status", status),
        ("diff", diff),
        ("add", staged),
        ("commit", committed),
    ):
        assert "error" not in result, (name, result)
    assert not marker.exists(), "shell_exec git executed a repository-controlled program"


@pytest.mark.asyncio
async def test_shell_exec_git_applies_fixed_flags_and_filter_overrides(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接线探针：真正起的 argv 里既有固定 flags，也有仓库动态 filter 覆盖。"""
    calls: list[tuple[str, ...]] = []
    envs: list[dict[str, str]] = []
    real_create = __import__("asyncio").create_subprocess_exec

    async def capture(*args, **kwargs):
        calls.append(args)
        envs.append(kwargs.get("env") or {})
        return await real_create(*args, **kwargs)

    monkeypatch.setattr("asyncio.create_subprocess_exec", capture)
    _git(git_repo, "config", "filter.malicious.clean", "cat")
    _git(git_repo, "config", "diff.malicious.textconv", "cat")

    await sandbox.run_in_sandbox("git status --porcelain")

    # 前两次是仓库根检查与配置探测；被硬化的**命令本身**是最后一次。
    argv = calls[-1]
    assert "status" in argv and "--porcelain" in argv, argv
    for expected in (
        "--no-pager",
        f"core.hooksPath={os.devnull}",
        "core.fsmonitor=false",
        "core.pager=cat",
        "core.alternateRefsCommand=",
        "filter.malicious.clean=",
        "filter.malicious.required=false",
        "diff.malicious.textconv=",
    ):
        assert expected in argv, (expected, argv)
    env = envs[-1]
    assert isinstance(env, dict)
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GIT_CONFIG_GLOBAL") == os.devnull


@pytest.mark.asyncio
async def test_shell_exec_git_config_injection_is_blocked(
    git_repo: Path, tmp_path: Path
) -> None:
    """攻击探针：用 `-c` / `--config-env` / `--exec-path` 把安全配置重新打开 —— 必须拒绝。"""
    marker, command = _probe(tmp_path)
    _probe_is_live(command, marker)

    for payload in (
        "git -c core.fsmonitor=probe-marker status",
        "git -ccore.fsmonitor=probe-marker status",
        "git --config-env=core.fsmonitor=PROBE status",
        "git --config=core.fsmonitor=probe-marker status",
        "git --exec-path=. status",
        "git --git-dir=.git status",
    ):
        result = await sandbox.run_in_sandbox(payload)
        assert "error" in result, (payload, result)
        assert "git option blocked" in result["error"], (payload, result)
    assert not marker.exists(), "config injection re-enabled repository-controlled execution"


@pytest.mark.asyncio
async def test_probe_failure_refuses_before_running_git(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测失败 ⇒ 拒绝执行（不是"先跑起来再说"）。"""
    monkeypatch.setattr(
        git_tools,
        "_probe_git_config",
        lambda: _failed_probe(),
    )

    calls: list[tuple[str, ...]] = []
    real_run_git = git_tools._run_git

    async def spy(*args, **kwargs):
        calls.append(args)
        return await real_run_git(*args, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git", spy)

    status = await git_tools.git_status()
    shell = await sandbox.run_in_sandbox("git status")

    assert "git repository configuration probe failed" in status.get("error", ""), status
    assert "git repository configuration probe failed" in shell.get("error", ""), shell
    # 除了仓库根检查（rev-parse）那次，探测失败之后不应再执行任何 git 子命令
    assert all(args[:1] != ("status",) for args in calls), calls


async def _failed_probe():
    return (), {"error": "git repository configuration probe failed: simulated unreadable config"}


def test_long_lived_shell_environment_carries_static_and_dynamic_config(
    git_repo: Path, tmp_path: Path
) -> None:
    """长驻 shell 改不了 argv ⇒ 用 GIT_CONFIG_* 钉住静态配置 + 动态 filter 覆盖。"""
    import asyncio

    _git(git_repo, "config", "filter.malicious.clean", "cat")

    env = asyncio.run(git_tools.git_shell_env(sandbox.sandboxed_env()))

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_PAGER"] == "cat"
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = {
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)
    }
    # 固定安全配置（hooks / fsmonitor / pager / 外部 diff / ssh / gpg）一个都不少
    for key, value in git_tools._GIT_SAFE_CONFIG:
        assert (key, value) in pairs, key
    # 动态覆盖：仓库声明的 filter 驱动被钉成空命令
    assert ("filter.malicious.clean", "") in pairs
    assert ("filter.malicious.required", "false") in pairs


def test_device_and_engine_git_hardening_agree() -> None:
    """设备端（Electron device-fs.cjs）与引擎端固定安全配置必须同口径。

    只读断言，不改设备端：引擎每加一个键，设备端就补齐，否则"同一份项目在两端
    行为不同"——那正是 H25 验收最后一句要排除的漂移。
    """
    device = (
        Path(__file__).resolve().parents[2] / "frontend-vue" / "electron" / "device-fs.cjs"
    )
    source = device.read_text(encoding="utf-8")

    for key, value in git_tools._GIT_SAFE_CONFIG:
        assert f"['{key}'," in source, f"device end is missing git hardening key: {key}"
        if key == "core.hooksPath":
            continue  # 设备端用 GIT_NULL 常量占位，值随平台变化
        assert f"['{key}', {value!r}]".replace('"', "'") in source or f"['{key}', '{value}']" in source, (
            f"device end disagrees on value for {key}"
        )
    # 同一套"探测 → 覆盖 / 探测失败即拒绝"的流程
    assert "config', '--includes', '--null', '--list'" in source
    assert "git repository configuration probe failed" in source
    assert "deviceGitConfigOverrides" in source
