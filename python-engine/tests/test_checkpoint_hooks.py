"""`checkpoint_hooks` 的契约测试。

钩子挂在 `runtime.run()` 主路径上，因此**只有两个**要求：
未绑定项目时什么都不做；任何失败都不抛异常。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.agent import checkpoint_hooks as hooks


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=root, check=True, capture_output=True)
    return root


def test_no_project_root_is_a_noop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hooks, "project_root", lambda: None)
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") is None


def test_creates_checkpoint_when_project_bound(monkeypatch, tmp_path: Path) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(hooks, "project_root", lambda: root)
    monkeypatch.setattr(hooks, "_session_id", lambda: "sess-1")
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") == 1
    # 第二次必须是 2：序号来自已落下的 ref，不是每次从 1 重来。
    assert hooks.turn_checkpoint("post_turn", state_dir=tmp_path / "st") == 2


def test_failure_never_raises(monkeypatch, tmp_path: Path) -> None:
    """项目根是个普通目录（非 git）→ 返回 None，不抛异常。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(hooks, "project_root", lambda: plain)
    monkeypatch.setattr(hooks, "_session_id", lambda: "s2")
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st2") is None
