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


def test_seq_survives_packed_refs(monkeypatch, tmp_path: Path) -> None:
    """★★ `git pack-refs` 之后序号必须继续递增（还原点不能"用一阵子就不工作了"）。

    缺陷形态（实测）：`_next_seq` 曾遍历 `.git/refs/chiron/checkpoints/<session>/`
    数松散文件。`git gc` 默认带 `pack-refs --all --prune`，打包后目录消失、ref 只留在
    `.git/packed-refs` 里 ⇒ 序号回退到 1 ⇒ `write_ref` 的 CAS（旧值必须为空）必然失败
    ⇒ 此后**每次**建点都静默失败（fail-open，对话照常），用户只看到时间线不再增长。

    判据：打包后仍能建点，且序号是 3 而不是重复的 1。
    """
    root = _repo(tmp_path)
    monkeypatch.setattr(hooks, "project_root", lambda: root)
    monkeypatch.setattr(hooks, "_session_id", lambda: "sess-p")

    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") == 1
    assert hooks.turn_checkpoint("post_turn", state_dir=tmp_path / "st") == 2

    # 把松散 ref 收进 packed-refs（等价于用户仓库被 gc 过）
    subprocess.run(
        ["git", "pack-refs", "--all", "--prune"],
        cwd=root, check=True, capture_output=True,
    )
    loose = root / ".git" / "refs" / "chiron" / "checkpoints" / "sess-p"
    assert not loose.exists(), "前提不成立：pack-refs 后松散目录应当消失"

    # 打包之后必须还能建点（旧实现会静默返回 None）
    seq = hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st")
    assert seq == 3, f"pack-refs 后序号回退/建点失败：seq={seq!r}（期望 3）"
