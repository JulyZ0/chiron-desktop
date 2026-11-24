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
    """状态目录不可写/被文件占用 → 返回 None。**抛异常就是契约破坏**。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    blocker = tmp_path / "state2"
    blocker.write_text("not a directory", encoding="utf-8")
    rec = cs.create_checkpoint(
        plain, session_id="s3", seq=1, kind="pre_turn", state_dir=blocker
    )
    assert rec is None


def test_attempt_checkpoint_reports_failure_state(tmp_path: Path) -> None:
    """★★ 建点失败必须产出**可上报**的失败态记录（否则时间线上什么都不缺）。

    缺陷形态：`create_checkpoint` 失败只返回 None，上报层又把"无 commit"直接挡回去
    ⇒ 时间线永远只有成功的点。用户看到的是"还原点好像没有了"，而列表端注释明确写着
    "失败的检查点也要显示，用户需要知道这一刻没能保护成功"。

    判据：失败时 `state='failed'`、`commit_sha` 为空、`error` 非空且 `error_code` 可分类。
    """
    plain = tmp_path / "plain-failed"
    plain.mkdir()
    blocker = tmp_path / "state-failed"
    blocker.write_text("not a directory", encoding="utf-8")
    rec = cs.attempt_checkpoint(
        plain, session_id="s3", seq=1, kind="pre_turn", state_dir=blocker
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
    """``outer/`` 是仓库，``outer/inner`` 是**作为项目根**的 monorepo 子目录。"""
    outer = _repo(tmp_path)
    inner = outer / "inner"
    inner.mkdir()
    (inner / "x.py").write_text("print(1)\n", encoding="utf-8")
    return outer, inner


def test_nested_project_root_snapshots_only_its_subtree(tmp_path: Path) -> None:
    """★★ 项目根套在父仓库里 ⇒ 只快照子目录子树（monorepo 支持）。

    旧行为是**拒绝**（"project root is inside another git repository"）。原因是
    ``read-tree HEAD`` / ``write-tree`` 都是仓库全局口径：不裁剪就会把父仓库的
    全部文件（含别的项目的密钥）写进快照，而 ``ls-tree`` 在子目录里又被前缀过滤，
    上报成"0 个文件"。

    现在的判据：
      * 成功，且 ``file_count`` 只数子目录文件；
      * 树里没有父仓库根层的文件；
      * ref 落在父仓库（既定设计：对象库/ref 必须与树在同一个库里），`read_ref`
        从子目录与仓库顶层都能读到同一个 commit；
      * 未跟踪清单只含子目录的文件。
    """
    outer, inner = _nested(tmp_path)
    (outer / "outer_root.py").write_text("ROOT\n", encoding="utf-8")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-qm", "root file")
    (inner / "draft.py").write_text("draft\n", encoding="utf-8")

    rec = cs.attempt_checkpoint(
        inner, session_id="s-nest", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None and rec.state == "ok", rec.error
    # 只有 inner/x.py（已跟踪）+ inner/draft.py（未跟踪）—— 父仓库根层文件不算。
    assert rec.file_count == 1
    assert [p for p, _ in rec.untracked] == ["draft.py"]

    from app.agent import git_checkpoint as gc

    # ref 与对象都写在父仓库（同一个对象库），读取口径在这里必须一致。
    assert gc.read_ref(inner, rec.ref_name) == rec.commit_sha
    assert gc.read_ref(outer, rec.ref_name) == rec.commit_sha
    listed = subprocess.run(
        ["git", "-C", str(outer), "ls-tree", "-r", "--name-only", rec.commit_sha],
        capture_output=True,
        check=False,
    ).stdout.decode()
    assert "inner/x.py" in listed
    assert "outer_root.py" not in listed, "父仓库根层的文件绝不能进子目录快照"


def test_nested_project_seq_ignores_other_sessions(tmp_path: Path) -> None:
    """父仓库里别的会话的 ref 不影响子目录会话的序号（隔离靠 session_id）。"""
    from app.agent import git_checkpoint as gc

    outer, inner = _nested(tmp_path)
    tree = subprocess.run(
        ["git", "-C", str(outer), "write-tree"], capture_output=True, check=False
    ).stdout.decode().strip()
    sha = gc.commit_tree(outer, tree=tree)
    gc.write_ref(outer, gc.checkpoint_ref("other-session", 1, "pre_turn"), sha)

    assert gc.next_checkpoint_seq(inner, "s-nest") == 1
    rec = cs.attempt_checkpoint(
        inner, session_id="s-nest", seq=1, kind="pre_turn", state_dir=tmp_path / "state"
    )
    assert rec is not None and rec.succeeded
    assert gc.next_checkpoint_seq(inner, "s-nest") == 2


def test_plain_project_checkpoint_restore_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """非 Git 项目：创建 → 修改/新增 → 恢复，全程不污染用户目录。"""
    from app.agent import git_checkpoint as gc

    # basetemp 可能位于本仓库内；阻止 git 向上发现父仓库，确保覆盖真正的普通目录。
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("before\n", encoding="utf-8")
    (plain / "remove.txt").write_text("remove me\n", encoding="utf-8")
    (plain / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (plain / ".env").write_text("SECRET=do-not-snapshot\n", encoding="utf-8")
    (plain / "node_modules").mkdir()
    (plain / "node_modules" / "x.js").write_text("noise\n", encoding="utf-8")
    state = tmp_path / "state"

    rec = cs.attempt_checkpoint(
        plain, session_id="s-plain", seq=1, kind="pre_turn", state_dir=state
    )
    assert rec is not None and rec.state == "ok", rec.error
    assert rec.file_count == 3  # a.py / remove.txt / .gitignore；秘密与噪音排除
    assert [p for p, _ in rec.untracked] == [".gitignore", "a.py", "remove.txt"]
    assert not (plain / ".git").exists(), "普通项目绝不能被偷偷 git init"
    assert gc.read_ref(cs.shadow_git_dir(plain, state), rec.ref_name) == rec.commit_sha

    # 钩子看不到影子 ref，会再次传 seq=1；服务层必须依据影子 ref 自增到 2。
    second = cs.attempt_checkpoint(
        plain, session_id="s-plain", seq=1, kind="post_turn", state_dir=state,
        parent_sha=rec.commit_sha,
    )
    assert second is not None and second.succeeded and second.seq == 2, second.error

    (plain / "a.py").write_text("after\n", encoding="utf-8")
    (plain / "remove.txt").unlink()
    (plain / "new.txt").write_text("new\n", encoding="utf-8")
    result = cs.restore_plain_directory(
        plain,
        manifest=dict(rec.untracked),
        store_root=state / "untracked",
        allow_delete=True,
    )
    assert result.ok, result.failed
    assert (plain / "a.py").read_text(encoding="utf-8") == "before\n"
    assert (plain / "remove.txt").read_text(encoding="utf-8") == "remove me\n"
    assert not (plain / "new.txt").exists()
    assert all(p != ".env" for p, _ in rec.untracked)
    assert (plain / ".env").read_text(encoding="utf-8").startswith("SECRET=")


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

