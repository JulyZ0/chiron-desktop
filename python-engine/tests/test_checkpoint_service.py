"""`checkpoint_service` 的契约测试：成功路径 + fail-open。

只测两件事，因为这两件是调用方唯一的依赖：

1. 正常仓库上能建出检查点，字段与 `project_checkpoints` 对齐。
2. **任何失败都不抛异常**。检查点是辅助能力，不能因为它而中断用户对话 ——
   这是本模块与 `git_checkpoint`（失败必须抛 `GitError`）的分界。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.agent import checkpoint_service as cs


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("print(1)\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_create_checkpoint_records_tree_and_ref(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    rec = cs.create_checkpoint(
        root, session_id="s1", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None
    assert rec.seq == 1 and rec.kind == "pre_turn"
    assert rec.file_count >= 1
    # ref 必须真的落下，且指向同一个 commit（gc 锚点的唯一依据）。
    from app.agent import git_checkpoint as gc

    assert gc.read_ref(root, rec.ref_name) == rec.commit_sha


def test_untracked_file_is_captured(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "new.py").write_text("x = 1\n", encoding="utf-8")
    rec = cs.create_checkpoint(
        root, session_id="s2", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None
    # D4：未跟踪文件走自有 CAS，必须被记进清单（否则 agent 新建的文件无法回滚）。
    assert rec.untracked_count == 1


def test_failure_returns_none_instead_of_raising(tmp_path: Path) -> None:
    """非 git 目录 → 返回 None。**抛异常就是契约破坏**。"""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rec = cs.create_checkpoint(
        plain, session_id="s3", seq=1, kind="pre_turn", state_dir=tmp_path / "state2"
    )
    assert rec is None


def test_empty_session_id_is_rejected_without_touching_git(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert cs.create_checkpoint(root, session_id="", seq=1, kind="pre_turn", state_dir=tmp_path / "s") is None
