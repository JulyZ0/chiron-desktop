"""N6：验证结论的记账口径。"""

from __future__ import annotations

import pytest

from app.agent.verification import (
    VerificationTracker,
    classify_verification_command,
    revision_nudge,
)


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("pnpm test", "test"),
        ("pnpm run test -- --run", "test"),
        ("pytest -q", "test"),
        ("go test ./internal/...", "test"),
        ("cargo test --all", "test"),
        ("npx vitest run", "test"),
        ("vue-tsc --noEmit", "typecheck"),
        ("go vet ./...", "typecheck"),
        ("ruff check .", "lint"),
        ("eslint src", "lint"),
        ("gofmt -l .", "lint"),
        ("go build ./...", "build"),
        ("pnpm build", "build"),
    ],
)
def test_classify_verification_command(command: str, kind: str) -> None:
    assert classify_verification_command(command) == kind


@pytest.mark.parametrize(
    "command",
    ["git status", "ls", "cat package.json", "npm start", "go run ./cmd/x", ""],
)
def test_non_verification_commands_are_ignored(command: str) -> None:
    assert classify_verification_command(command) == ""


def _note(tracker: VerificationTracker, name: str, args: object, result: object, executed: bool = True) -> None:
    tracker.note_execution(name, args, result, executed=executed)


def test_write_then_unverified() -> None:
    tracker = VerificationTracker()
    _note(tracker, "write_file", {"path": "a.py"}, {"ok": True})
    summary = tracker.summary()
    assert summary.status == "unverified"
    assert summary.wrote_files is True
    assert tracker.needs_revision() is True


def test_write_then_passing_check() -> None:
    tracker = VerificationTracker()
    _note(tracker, "edit_file", {"path": "a.py"}, {"ok": True})
    _note(tracker, "shell_exec", {"command": "pytest -q"}, {"exit_code": 0})
    summary = tracker.summary()
    assert summary.status == "passed"
    assert summary.command == "pytest -q"
    assert tracker.needs_revision() is False


def test_any_failure_wins_and_names_the_failed_command() -> None:
    tracker = VerificationTracker()
    _note(tracker, "write_file", {"path": "a.py"}, {"ok": True})
    _note(tracker, "shell_exec", {"command": "pytest -q"}, {"exit_code": 1})
    _note(tracker, "shell_exec", {"command": "vue-tsc --noEmit"}, {"exit_code": 0})
    summary = tracker.summary()
    assert summary.status == "failed"
    assert summary.command == "pytest -q"
    assert summary.exit_code == 1
    assert tracker.needs_revision() is True


def test_read_only_run_is_not_nudged() -> None:
    tracker = VerificationTracker()
    _note(tracker, "shell_exec", {"command": "pytest -q"}, {"exit_code": 1})
    assert tracker.summary().status == "failed"
    assert tracker.needs_revision() is False, "只读排查失败不该被要求续轮修复"


def test_not_executed_and_irrelevant_calls_are_ignored() -> None:
    tracker = VerificationTracker()
    _note(tracker, "shell_exec", {"command": "pytest -q"}, {"exit_code": 1}, executed=False)
    _note(tracker, "shell_exec", {"command": "git status"}, {"exit_code": 1})
    _note(tracker, "read_file", {"path": "a.py"}, {"content": "x"})
    assert tracker.summary().status == "not-needed"
    assert tracker.needs_revision() is False


def test_arguments_may_be_json_string() -> None:
    tracker = VerificationTracker()
    _note(tracker, "write_file", {"path": "a.py"}, {"ok": True})
    _note(tracker, "shell_exec", '{"command": "go test ./..."}', {"exit_code": 0})
    assert tracker.summary().status == "passed"


def test_nudge_text_states_facts() -> None:
    tracker = VerificationTracker()
    _note(tracker, "write_file", {"path": "a.py"}, {"ok": True})
    unverified = revision_nudge(tracker.summary())
    assert "未经验证" in unverified

    _note(tracker, "shell_exec", {"command": "pnpm test"}, {"exit_code": 2})
    failed = revision_nudge(tracker.summary())
    assert "pnpm test" in failed and "exit 2" in failed
