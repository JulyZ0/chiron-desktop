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


def test_attempt_checkpoint_reports_failure_state(tmp_path: Path) -> None:
    """★★ 建点失败必须产出**可上报**的失败态记录（否则时间线上什么都不缺）。

    缺陷形态：`create_checkpoint` 失败只返回 None，上报层又把"无 commit"直接挡回去
    ⇒ 时间线永远只有成功的点。用户看到的是"还原点好像没有了"，而列表端注释明确写着
    "失败的检查点也要显示，用户需要知道这一刻没能保护成功"。

    判据：失败时 `state='failed'`、`commit_sha` 为空、`error` 非空且 `error_code` 可分类。
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rec = cs.attempt_checkpoint(
        plain, session_id="s3", seq=1, kind="pre_turn", state_dir=tmp_path / "state2"
    )
    assert rec is not None, "失败也要有记录，否则用户看不到这一刻没保护成功"
    assert rec.succeeded is False
    assert rec.state == "failed"
    assert rec.commit_sha == "" and rec.tree_sha == "" and rec.ref_name == ""
    assert rec.error, "必须有可读原因：只有错误码时用户仍不知道为什么"
    assert rec.error_code in {"git", "io", "unknown"}


def test_empty_session_id_is_rejected_without_touching_git(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert cs.create_checkpoint(root, session_id="", seq=1, kind="pre_turn", state_dir=tmp_path / "s") is None


# ---------------------------------------------------------------------------
# 根因回归：git 会向上找父仓库 ⇒ 必须校验"仓库顶层 == 项目根"
# ---------------------------------------------------------------------------


def _nested(tmp_path: Path) -> tuple[Path, Path]:
    """``outer/`` 是仓库，``outer/inner`` 是"被当成项目根"的子目录。"""
    outer = _repo(tmp_path)
    inner = outer / "inner"
    inner.mkdir()
    (inner / "x.py").write_text("print(1)\n", encoding="utf-8")
    return outer, inner


def _all_refs(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "for-each-ref", "refs/chiron"],
        capture_output=True,
        check=False,
    )
    return proc.stdout.decode("utf-8", errors="replace")


def test_nested_project_root_does_not_snapshot_parent_repo(tmp_path: Path) -> None:
    """★★ 项目根套在父仓库里 ⇒ 必须失败，**绝不**快照父仓库内容。

    缺陷形态（实测）：``git -C inner write-tree`` 在子目录里同样成功 —— git 向上
    找到了父仓库。于是
      * 快照里是**父仓库**的文件（``outer_secret.py``），而不是 inner 里的；
      * commit / ref 写进父仓库（``refs/chiron/checkpoints/...``）；
      * 上报的却是 ``state='ok'``，且 ``ls-tree`` 在子目录下被路径前缀过滤成空清单
        ⇒ 时间线上显示"这一刻保护成功、0 个文件"。

    判据三条：失败态 + 可读原因 + 父仓库里一个 chiron ref 都没有。
    """
    outer, inner = _nested(tmp_path)
    rec = cs.attempt_checkpoint(
        inner, session_id="s-nest", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None
    assert rec.state == "failed", "嵌套在父仓库里的项目根不能报成功"
    assert "inside another git repository" in rec.error, rec.error
    assert rec.error_code == "git"
    assert rec.commit_sha == "" and rec.ref_name == ""
    assert _all_refs(outer) == "", "检查点 ref 绝不能写进父仓库"
    assert len(rec.untracked) == 0, "父仓库的未跟踪文件也绝不能进本次快照"


def test_uninitialized_project_fails_with_actionable_message(tmp_path: Path) -> None:
    """未 ``git init`` 的项目 ⇒ 失败态记录里的原因要能告诉用户"先 git init"。

    与上一条同源：此前非 git 目录若恰好位于某个仓库之下，会"成功"快照父仓库；
    现在的行为是**明确失败**，前端因此能显示"这一刻没保护成功 + 原因"。

    原因有两种正确的说法，取决于临时目录落在哪里 —— 这**不是**测试的模糊地带，
    而是两条不同的真实分支：普通目录得到 ``not a git repository``；
    而临时目录若正好在某个仓库里（本仓库的父目录、或 ``--basetemp`` 指到了仓库内），
    得到的是 ``inside another git repository``。两种都必须带 ``git init`` 这一行动项。
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    rec = cs.attempt_checkpoint(
        plain, session_id="s-plain", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None and rec.state == "failed"
    assert rec.error_code == "git"
    assert (
        "not a git repository" in rec.error
        or "inside another git repository" in rec.error
    ), rec.error
    assert "git init" in rec.error, "原因里要给出下一步动作，而不只是结论"


def test_missing_git_binary_failure_is_actionable(tmp_path: Path, monkeypatch) -> None:
    """找不到 git 可执行文件 ⇒ 错误信息必须点名 CHIRON_GIT_BIN，而不是 WinError 2。

    打包运行时（Electron 拉起的引擎）PATH 常常只有系统默认几项。此前 ``_run_git``
    直接把 ``FileNotFoundError`` 漏出去，用户看到的是 ``[WinError 2] 系统找不到
    指定的文件`` —— 既没说找不到什么，也没给出路。
    """
    root = _repo(tmp_path)
    monkeypatch.setenv("CHIRON_GIT_BIN", str(tmp_path / "no-such-git-binary"))
    rec = cs.attempt_checkpoint(
        root, session_id="s-bin", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None and rec.state == "failed"
    assert rec.error_code == "git"
    assert "未找到 git" in rec.error, rec.error
    assert "CHIRON_GIT_BIN" in rec.error, "必须告诉用户可以用哪个设置补救"

