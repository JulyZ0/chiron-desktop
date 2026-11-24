"""Regression tests for repository-controlled Git execution hooks."""

from __future__ import annotations

import asyncio
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
    _git(repo, "config", "user.name", "Git Tool Test")
    _git(repo, "config", "user.email", "git-tool@example.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _harmless_probe(tmp_path: Path) -> tuple[Path, Path, str]:
    marker = tmp_path / "executed.marker"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}"'
    return script, marker, command


def _modify_tracked(repo: Path) -> None:
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_normal_status_diff_and_commit(git_repo: Path) -> None:
    _modify_tracked(git_repo)
    (git_repo / "new.txt").write_text("new\n", encoding="utf-8")

    status = await git_tools.git_status()
    assert status.get("exit_code") == 0, status
    assert {item["path"] for item in status["files"]} == {"tracked.txt", "new.txt"}

    diff = await git_tools.git_diff()
    assert diff.get("exit_code") == 0, diff
    assert "-before" in diff["diff"]
    assert "+after" in diff["diff"]

    staged_diff = await git_tools.git_diff(staged=True)
    assert staged_diff.get("exit_code") == 0, staged_diff

    committed = await git_tools.git_commit("safe commit")
    assert committed.get("exit_code") == 0, committed
    assert _git(git_repo, "log", "-1", "--format=%s").stdout.strip() == "safe commit"
    clean = await git_tools.git_status()
    assert clean.get("exit_code") == 0, clean
    assert clean["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "diff", "commit"])
async def test_repository_filter_never_executes(git_repo: Path, tmp_path: Path, operation: str) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "filter.malicious.clean", command)
    _git(git_repo, "config", "filter.malicious.smudge", command)
    _git(git_repo, "config", "filter.malicious.process", command)
    _git(git_repo, "config", "filter.malicious.required", "true")
    (git_repo / ".gitattributes").write_text("*.txt filter=malicious\n", encoding="utf-8")
    _modify_tracked(git_repo)

    if operation == "status":
        result = await git_tools.git_status()
    elif operation == "diff":
        result = await git_tools.git_diff()
    else:
        result = await git_tools.git_commit("filter must not run")

    assert result.get("exit_code") == 0, result
    assert not marker.exists(), "repository filter executed"


@pytest.mark.asyncio
async def test_included_filter_is_discovered_and_overridden(git_repo: Path, tmp_path: Path) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    included = git_repo / "included.config"
    included.write_text(f'[filter "included"]\n\tclean = {command}\n', encoding="utf-8")
    _git(git_repo, "config", "include.path", str(included))
    (git_repo / ".gitattributes").write_text("*.txt filter=included\n", encoding="utf-8")
    _modify_tracked(git_repo)

    result = await git_tools.git_commit("included filter must not run")

    assert result.get("exit_code") == 0, result
    assert not marker.exists(), "included repository filter executed"


@pytest.mark.asyncio
async def test_worktree_filter_is_discovered_and_overridden(git_repo: Path, tmp_path: Path) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "extensions.worktreeConfig", "true")
    (git_repo / ".git" / "config.worktree").write_text(f'[filter "worktree"]\n\tclean = {command}\n', encoding="utf-8")
    (git_repo / ".gitattributes").write_text("*.txt filter=worktree\n", encoding="utf-8")
    _modify_tracked(git_repo)

    result = await git_tools.git_commit("worktree filter must not run")

    assert result.get("exit_code") == 0, result
    assert not marker.exists(), "worktree repository filter executed"


@pytest.mark.asyncio
async def test_textconv_and_external_diff_never_execute(git_repo: Path, tmp_path: Path) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "diff.malicious.textconv", command)
    _git(git_repo, "config", "diff.external", command)
    (git_repo / ".gitattributes").write_text("*.txt diff=malicious\n", encoding="utf-8")
    _modify_tracked(git_repo)

    result = await git_tools.git_diff()

    assert result.get("exit_code") == 0, result
    assert "+after" in result["diff"]
    assert not marker.exists(), "repository diff helper executed"


@pytest.mark.asyncio
async def test_commit_hooks_never_execute(git_repo: Path, tmp_path: Path) -> None:
    script, marker, command = _harmless_probe(tmp_path)
    del script
    for hook_name in ("pre-commit", "commit-msg", "post-commit"):
        hook = git_repo / ".git" / "hooks" / hook_name
        hook.write_text(f"#!/bin/sh\n{command}\n", encoding="utf-8")
        hook.chmod(0o755)
    _modify_tracked(git_repo)

    result = await git_tools.git_commit("hooks must not run")

    assert result.get("exit_code") == 0, result
    assert not marker.exists(), "repository hook executed"


@pytest.mark.asyncio
async def test_fsmonitor_and_pager_never_execute(git_repo: Path, tmp_path: Path) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "core.fsmonitor", command)
    _git(git_repo, "config", "core.pager", command)
    _git(git_repo, "config", "pager.log", "true")
    _modify_tracked(git_repo)

    status = await git_tools.git_status()
    log = await git_tools.git_log()

    assert status.get("exit_code") == 0, status
    assert log.get("exit_code") == 0, log
    assert not marker.exists(), "repository fsmonitor or pager executed"


@pytest.mark.asyncio
async def test_credential_helper_never_executes(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "credential.helper", command)
    real_create = asyncio.create_subprocess_exec

    async def create_without_stdin(*args, **kwargs):
        kwargs["stdin"] = asyncio.subprocess.DEVNULL
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_without_stdin)

    await git_tools._run_git("credential", "fill")

    assert not marker.exists(), "repository credential helper executed"


@pytest.mark.asyncio
async def test_commit_signing_helper_never_executes(git_repo: Path, tmp_path: Path) -> None:
    _, marker, command = _harmless_probe(tmp_path)
    _git(git_repo, "config", "commit.gpgSign", "true")
    _git(git_repo, "config", "log.showSignature", "true")
    _git(git_repo, "config", "gpg.program", command)
    _modify_tracked(git_repo)

    log = await git_tools.git_log()
    result = await git_tools.git_commit("signing helper must not run")

    assert log.get("exit_code") == 0, log
    assert result.get("exit_code") == 0, result
    assert not marker.exists(), "repository signing helper executed"


@pytest.mark.asyncio
async def test_git_argv_and_environment_are_hardened(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_TRACE",
    ):
        monkeypatch.setenv(name, "malicious-value")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_repo / "hostile-global.config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
    monkeypatch.setenv("HOST_SECRET", "must-not-leak")

    captured: dict[str, object] = {}
    real_create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs["env"]
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    result = await git_tools._run_git("status", "--porcelain")

    assert result.get("exit_code") == 0, result
    argv = captured["argv"]
    assert isinstance(argv, tuple)
    assert "--no-pager" in argv
    for setting in (
        f"core.hooksPath={os.devnull}",
        "core.fsmonitor=false",
        "core.pager=cat",
        "credential.helper=",
        "diff.external=",
        "commit.gpgSign=false",
        "tag.gpgSign=false",
        "log.showSignature=false",
        "protocol.allow=never",
    ):
        assert setting in argv, setting

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_PROTOCOL_FROM_USER"] == "0"
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_TRACE",
        "HOST_SECRET",
    ):
        assert name not in env, name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe_failure",
    [
        {"error": "configuration unavailable"},
        {"exit_code": 1, "stdout": "", "stderr": "bad repository configuration"},
        {"exit_code": 0, "stdout": "malformed", "stderr": ""},
    ],
)
async def test_configuration_probe_failure_refuses_execution(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_failure: dict[str, object],
) -> None:
    real_run = git_tools._run_git
    calls: list[tuple[str, ...]] = []

    async def fail_config_probe(*args: str, timeout: int = 30, **kwargs):
        calls.append(args)
        if args[0] == "config":
            return probe_failure
        return await real_run(*args, timeout=timeout, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git", fail_config_probe)

    result = await git_tools.git_status()

    assert "configuration probe failed" in result.get("error", ""), result
    assert not any(args[0] == "status" for args in calls), calls


@pytest.mark.asyncio
async def test_malformed_repository_configuration_is_rejected(git_repo: Path) -> None:
    config = git_repo / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "\n[broken\n", encoding="utf-8")

    result = await git_tools.git_status()

    assert "probe failed" in result.get("error", ""), result
