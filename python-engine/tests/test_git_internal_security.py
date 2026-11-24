"""H25 regressions for internal Python Git context and checkpoint entry points."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent import git_checkpoint as gc
from app.agent.prompt_engine import PromptEngine


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env=env,
        timeout=15,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "H25 Test")
    _git(root, "config", "user.email", "h25@example.invalid")
    _git(root, "config", "commit.gpgSign", "false")
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _probe(tmp_path: Path) -> tuple[Path, str]:
    marker = tmp_path / "executed.marker"
    script = tmp_path / "probe.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "{marker.as_posix()}"'
    return marker, command


def _poison_repo(repo: Path, command: str) -> None:
    for key in (
        "filter.malicious.clean",
        "filter.malicious.smudge",
        "filter.malicious.process",
    ):
        _git(repo, "config", key, command)
    _git(repo, "config", "filter.malicious.required", "true")
    _git(repo, "config", "diff.malicious.textconv", command)
    _git(repo, "config", "core.fsmonitor", command)
    _git(repo, "config", "core.pager", command)
    (repo / ".gitattributes").write_text("*.txt filter=malicious diff=malicious\n", encoding="utf-8")


def test_prompt_context_is_functional_and_never_executes_repository_helpers(
    repo: Path, tmp_path: Path
) -> None:
    marker, command = _probe(tmp_path)
    _poison_repo(repo, command)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")

    context = PromptEngine()._get_git_context(str(repo))

    assert "**Branch:**" in context
    assert "tracked.txt" in context
    assert "initial" in context
    assert not marker.exists(), "prompt Git context executed a repository helper"


def test_prompt_context_fails_closed_on_malformed_configuration(repo: Path) -> None:
    config = repo / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "\n[broken\n", encoding="utf-8")

    assert PromptEngine()._get_git_context(str(repo)) == ""


def test_checkpoint_snapshot_and_restore_never_execute_repository_helpers(
    repo: Path, tmp_path: Path
) -> None:
    marker, command = _probe(tmp_path)
    _poison_repo(repo, command)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    state = tmp_path / "state"
    index = state / "snapshot.idx"

    snapshot = gc.snapshot(repo, index_file=index, store_root=state / "untracked")
    (repo / "tracked.txt").write_text("changed again\n", encoding="utf-8")
    restored = gc.restore(
        repo,
        target=snapshot,
        store_root=state / "untracked",
        index_file=index,
    )

    assert restored.ok, restored
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "after\n"
    assert not marker.exists(), "checkpoint Git command executed a repository helper"


def test_included_filter_configuration_is_guarded_for_prompt_and_checkpoint(
    repo: Path, tmp_path: Path
) -> None:
    marker, command = _probe(tmp_path)
    included = repo / "included.config"
    _git(repo, "config", "-f", str(included), "filter.included.clean", command)
    _git(repo, "config", "-f", str(included), "filter.included.smudge", command)
    _git(repo, "config", "include.path", str(included))
    _git(repo, "config", "filter.included.required", "true")
    (repo / ".gitattributes").write_text("*.txt filter=included\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("included probe\n", encoding="utf-8")

    context = PromptEngine()._get_git_context(str(repo))
    snapshot = gc.snapshot(
        repo,
        index_file=tmp_path / "state" / "snapshot.idx",
        store_root=tmp_path / "state" / "untracked",
    )

    assert "tracked.txt" in context
    assert snapshot.tree
    assert not marker.exists(), "included repository filter executed"


def test_checkpoint_refuses_malformed_configuration_before_worktree_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = repo / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "\n[broken\n", encoding="utf-8")
    commands: list[list[str]] = []
    real_run = subprocess.run

    def capture(cmd, **kwargs):
        commands.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)

    with pytest.raises(gc.GitError, match="configuration probe failed"):
        gc.snapshot(repo, index_file=repo.parent / "state" / "snapshot.idx")

    assert len(commands) == 1
    assert commands[0][-4:] == ["config", "--includes", "--null", "--list"]


def test_checkpoint_git_preserves_binary_stdin_and_isolated_index(
    repo: Path, tmp_path: Path
) -> None:
    index = tmp_path / "isolated.idx"
    shutil.copyfile(repo / ".git" / "index", index)
    index_before = index.read_bytes()
    payload = b"raw\x00bytes\xff\n"

    blob = gc._run_git(repo, "hash-object", "--stdin", stdin=payload)
    assert blob.returncode == 0, blob.stderr
    assert blob.stdout.strip() == hashlib.sha1(
        f"blob {len(payload)}\0".encode("ascii") + payload
    ).hexdigest().encode("ascii")

    listed = gc._run_git(
        repo,
        "ls-files",
        "-z",
        env={"GIT_INDEX_FILE": str(index)},
    )
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout == b"tracked.txt\0"
    assert index.read_bytes() == index_before


def test_checkpoint_git_environment_is_minimal_and_arguments_are_hardened(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_TRACE",
        "HOST_SECRET",
    ):
        monkeypatch.setenv(name, "must-not-leak")
    captured: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run
    git_executable = shutil.which("git")
    assert git_executable is not None
    monkeypatch.setenv(gc.GIT_BIN_ENV, git_executable)

    def capture(cmd, **kwargs):
        captured.append((list(cmd), dict(kwargs["env"])))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)

    result = gc._run_git(repo, "status", "--porcelain")

    assert result.returncode == 0, result.stderr
    argv, env = captured[-1]
    assert argv[0] == git_executable
    assert "--no-pager" in argv
    assert "core.hooksPath=" + os.devnull in argv
    assert "core.alternateRefsCommand=" in argv
    assert "core.fsmonitor=false" in argv
    assert "credential.helper=" in argv
    assert "diff.external=" in argv
    assert "protocol.allow=never" in argv
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_TRACE",
        "HOST_SECRET",
    ):
        assert name not in env, name
