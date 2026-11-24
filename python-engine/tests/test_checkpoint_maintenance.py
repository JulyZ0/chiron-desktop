"""P1-3 回归：回滚差异预览 与 未跟踪 CAS 回收（GC）。

这两条路径的共同点是**一旦错了就会丢用户数据**（预览骗人说"没有差异"、
GC 删掉还被引用的 blob），因此用例全部落在"错了也不报错"的形态上：

* 预览必须**只读**：跑完之后工作区与 git 状态一个字节都不能变；
* 预览读不到未跟踪清单时必须 ``untracked_omitted=True``，且**不得**把当前多出来的
  未跟踪文件列进 ``removed``（否则用户会以为回滚会删掉它们）；
* GC 必须保留：被引用的分片（无论多老）、未被引用但还在保留期内的分片、
  形状不合法（``.tmp-*`` 等）的杂项文件；
* ``dry_run=True`` 是默认，且真的不动文件；重复执行幂等。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from app.agent import checkpoint_maintenance as cm
from app.agent import git_checkpoint as gc


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init", "-q").returncode == 0
    for key, value in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        assert _git(path, "config", key, value).returncode == 0
    return path


class Env:
    """一个仓库 + 工作区之外的状态目录。"""

    def __init__(self, tmp_path: Path) -> None:
        self.root = _init(tmp_path / "repo")
        self.state = tmp_path / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.index = self.state / "snap-s1.idx"
        self.store = self.state / "untracked"
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        assert _git(self.root, "add", "-A").returncode == 0
        assert _git(self.root, "commit", "-qm", "base").returncode == 0

    def snapshot(self) -> gc.Snapshot:
        return gc.snapshot(self.root, index_file=self.index, store_root=self.store)

    def checkpoint(self, session: str = "s1", seq: int = 1) -> tuple[gc.Snapshot, str]:
        snap = self.snapshot()
        commit = gc.commit_tree(self.root, tree=snap.tree, index_file=self.index)
        gc.write_ref(self.root, gc.checkpoint_ref(session, seq, "pre_turn"), commit, index_file=self.index)
        return snap, commit


# ---------------------------------------------------------------------------
# 差异预览
# ---------------------------------------------------------------------------


def test_preview_is_read_only_and_reports_tracked_changes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    snap, commit = env.checkpoint()

    # 改一个已跟踪文件，再加一个未跟踪文件。
    (env.root / "a.txt").write_text("two\n", encoding="utf-8")
    (env.root / "b.txt").write_text("brand new\n", encoding="utf-8")
    before = _worktree_files(env.root)

    result = cm.preview_diff(
        env.root,
        commit_sha=commit,
        untracked=dict(snap.untracked),
        state_dir=env.state,
        session_id="s1",
    )

    assert result.restored == ("a.txt",)
    assert result.removed == ("b.txt",)
    assert result.untracked_omitted is False
    assert result.current_files >= 2

    # 只读：工作区内容与文件集合都不变（预览不得覆盖用户的改动）。
    assert (env.root / "a.txt").read_text(encoding="utf-8") == "two\n"
    after = _worktree_files(env.root)
    assert after == before
    # git 状态也不能被预览弄脏（临时 index 必须在工作区之外）。
    status = _git(env.root, "status", "--porcelain")
    assert b"a.txt" in status.stdout


def test_preview_supports_plain_directory_shadow_checkpoint() -> None:
    """普通目录没有工作区 .git，也必须能预览影子仓库里的检查点差异。"""
    from app.agent import checkpoint_service as cs

    # 必须放在仓库之外：放在 tmp_path 会向上命中本仓库的 .git，不再是"普通目录"。
    with tempfile.TemporaryDirectory(prefix="chiron-plain-preview-") as td:
        base = Path(td)
        root, state = base / "plain", base / "state"
        root.mkdir()
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        rec = cs.create_checkpoint(root, session_id="s1", seq=1, kind="pre_turn", state_dir=state)
        assert rec is not None and rec.succeeded
        (root / "a.txt").write_text("two\n", encoding="utf-8")
        (root / "b.txt").write_text("new\n", encoding="utf-8")

        result = cm.preview_diff(
            root,
            commit_sha=rec.commit_sha,
            untracked=dict(rec.untracked),
            state_dir=state,
            session_id="s1",
        )
        assert result.restored == ("a.txt",)
        assert "b.txt" in result.removed
        assert not (root / ".git").exists(), "普通项目不应被预览写入 .git"


def test_preview_without_manifest_marks_untracked_omitted(tmp_path: Path) -> None:
    """清单读不到时：不列 removed、明确标记 omitted，且仍然给出跟踪文件的差异。"""
    env = Env(tmp_path)
    _snap, commit = env.checkpoint()

    (env.root / "a.txt").write_text("changed\n", encoding="utf-8")
    (env.root / "draft.md").write_text("scratch\n", encoding="utf-8")

    result = cm.preview_diff(
        env.root,
        commit_sha=commit,
        untracked=None,  # 数据库不可用 / 没有清单
        state_dir=env.state,
        session_id="s1",
    )

    assert result.untracked_omitted is True
    assert result.restored == ("a.txt",)
    # 关键：不知道清单时**不能**说"回滚会删掉 draft.md"。
    assert "draft.md" not in result.removed


def test_preview_rejects_unknown_commit(tmp_path: Path) -> None:
    env = Env(tmp_path)
    try:
        cm.preview_diff(
            env.root,
            commit_sha="0" * 40,
            untracked={},
            state_dir=env.state,
        )
    except gc.GitError as exc:
        assert "not found" in str(exc)
    else:  # pragma: no cover - 走到这里说明校验缺失
        raise AssertionError("unknown commit 应当报 GitError")


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


def _put(store: Path, digest: str, payload: bytes, *, age_days: float) -> Path:
    path = store / digest[:2] / digest[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _digest(ch: str) -> str:
    return ch * 64


#: 只比较**工作区**（``.git`` 之外）的文件集合。预览会用仓库自己的对象库写几个
#: 未引用的 blob/tree（``write-tree`` 的固有行为，``git gc`` 会回收），
#: 那不是"改了用户的文件"，但会出现在 ``.git`` 目录里。
def _worktree_files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    )


def test_gc_dry_run_reports_plan_without_deleting(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.checkpoint()
    ref = _digest("a")
    expired = _digest("b")
    young = _digest("c")
    _put(env.store, ref, b"x" * 10, age_days=400)
    _put(env.store, expired, b"y" * 7, age_days=90)
    _put(env.store, young, b"z" * 3, age_days=1)

    report = cm.gc_execute(
        env.store,
        roots=[env.root],
        referenced={ref},
        manifest_digests={ref, expired},
        retention_days=30,
        dry_run=True,
    )

    assert report.dry_run is True
    assert report.scanned >= 1
    assert report.referenced == 1
    assert report.deleted == 1
    assert report.freed_bytes == 7
    assert report.kept_young == 1
    assert report.orphan == 0
    # dry-run 一个文件都没删。
    assert (env.store / expired[:2] / expired[2:]).exists()
    assert (env.store / ref[:2] / ref[2:]).exists()
    assert (env.store / young[:2] / young[2:]).exists()


def test_gc_deletes_only_expired_unreferenced(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.checkpoint()
    ref = _digest("a")          # 被引用且很老 —— 必须留
    expired = _digest("b")      # 未引用且过期 —— 删
    orphan = _digest("c")       # 未引用、过期、且清单里从来没有 —— 删（计入 orphan）
    young = _digest("d")        # 未引用但年轻 —— 留
    _put(env.store, ref, b"r" * 5, age_days=3650)
    _put(env.store, expired, b"e" * 11, age_days=60)
    _put(env.store, orphan, b"o" * 4, age_days=60)
    _put(env.store, young, b"n" * 2, age_days=0.5)
    # 形状不合法的杂项文件：GC 不得碰它。
    junk = env.store / "zz" / ".tmp-keep"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"junk")

    report = cm.gc_execute(
        env.store,
        roots=[env.root],
        referenced={ref},
        manifest_digests={ref, expired},
        retention_days=30,
        dry_run=False,
    )

    assert report.deleted == 2
    assert report.freed_bytes == 15
    assert report.orphan == 1
    assert (env.store / ref[:2] / ref[2:]).exists()
    assert (env.store / young[:2] / young[2:]).exists()
    assert not (env.store / expired[:2] / expired[2:]).exists()
    assert not (env.store / orphan[:2] / orphan[2:]).exists()
    assert junk.exists()

    # 幂等：再跑一次没有可删的。
    again = cm.gc_execute(
        env.store,
        roots=[env.root],
        referenced={ref},
        manifest_digests={ref, expired},
        retention_days=30,
        dry_run=False,
    )
    assert again.deleted == 0
    assert again.freed_bytes == 0


def test_gc_retention_zero_deletes_all_unreferenced(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.checkpoint()
    fresh = _digest("e")
    _put(env.store, fresh, b"f" * 6, age_days=0)

    report = cm.gc_execute(
        env.store,
        roots=[env.root],
        referenced=set(),
        manifest_digests={fresh},
        retention_days=0,
        dry_run=True,
    )

    assert report.deleted == 1
    assert report.kept_young == 0


def test_gc_scans_refs_and_reports_dangling(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _snap, commit = env.checkpoint()
    scan = cm.scan_checkpoint_refs([env.root])
    assert scan.refs == 1
    assert scan.sessions == ("s1",)
    assert scan.commits == 1
    assert scan.tree_paths >= 1

    # ref 指向一个**不是 commit** 的对象（这里取 blob）：必须单独报成 dangling，
    # 而不是静默少算一个点。注意不能写全零 sha —— 那在 git 里等于"删除该 ref"。
    blob = _git(env.root, "rev-parse", "HEAD:a.txt").stdout.decode().strip()
    gc.write_ref(
        env.root,
        gc.checkpoint_ref("s1", 2, "post_turn"),
        blob,
        index_file=env.index,
    )
    scan2 = cm.scan_checkpoint_refs([env.root])
    assert scan2.refs == 2
    assert len(scan2.dangling) == 1
    assert scan2.commits == 1
