"""P0.5 回归：git 检查点原语（snapshot / diff / restore）。

这里验证的都是 ``docs/code-agent-checkpoint-design.md`` 里**用实测换来的**结论，
每一条都对应一个真实踩过的坑，因此不是"补充覆盖率"的凑数用例：

* §10.1 密钥：pathspec 排除 vs ``add`` 后 ``rm --cached`` 的泄漏差异；
* §16.2 ``-z``：不用它中文路径会被八进制转义；
* §16.3 CRLF：``git show`` 静默改行尾，``cat-file --filters`` 才保真；
* §5.2 三分类：``removed`` 默认不删（D5 未定案时的安全默认）；
* §14.6 两阶段：失败必须逐文件报告，且不回退已落地的文件。

另外三条是本次落地时**新发现**的缺陷（都有实测样本），单独立了回归用例：
已提交的 ``.env`` 随 ``read-tree HEAD`` 漏进树；用户已 ``git add`` 的新文件
被三处机制同时漏掉；已跟踪的超限文件同样漏进树。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent import git_checkpoint as gc

# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
    )


def _init(path: Path, *, autocrlf: str = "false") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init", "-q").returncode == 0
    for key, value in (
        ("user.email", "t@t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", autocrlf),
    ):
        assert _git(path, "config", key, value).returncode == 0
    return path


def _commit_all(path: Path, msg: str = "base") -> None:
    assert _git(path, "add", "-A").returncode == 0
    proc = _git(path, "commit", "-qm", msg)
    assert proc.returncode == 0, proc.stderr


class Env:
    """一个仓库 + 工作区之外的 index / CAS 目录。"""

    def __init__(self, tmp_path: Path, *, autocrlf: str = "false") -> None:
        # index 必须与工作区分离：放进工作区会被 add 收进快照，
        # 于是 write-tree 每次都不同（模块头部第 1 条）。
        self.root = _init(tmp_path / "repo", autocrlf=autocrlf)
        self.state = tmp_path / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.index = self.state / "snap.idx"
        self.store = self.state / "untracked"

    def write(self, rel: str, data: bytes | str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def snap(self, **kwargs: object) -> gc.Snapshot:
        return gc.snapshot(
            self.root,
            index_file=self.index,
            store_root=self.store,
            **kwargs,  # type: ignore[arg-type]
        )

    def restore(self, target: gc.Snapshot, **kwargs: object) -> gc.RestoreResult:
        return gc.restore(
            self.root,
            target=target,
            store_root=self.store,
            index_file=self.index,
            **kwargs,  # type: ignore[arg-type]
        )

    def status(self) -> str:
        return _git(self.root, "status", "--porcelain").stdout.decode()


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ---------------------------------------------------------------------------
# 排除规则
# ---------------------------------------------------------------------------


def test_is_excluded_matches_basename_dir_and_glob() -> None:
    assert gc._is_excluded(".env", gc.SECRET_GLOBS)
    assert gc._is_excluded("sub/.env", gc.SECRET_GLOBS)
    assert gc._is_excluded("sub/deep/.env.local", gc.SECRET_GLOBS)
    assert gc._is_excluded("node_modules/pkg/index.js", gc.NOISE_GLOBS)
    assert gc._is_excluded("a/b/node_modules/x.js", gc.NOISE_GLOBS)
    assert gc._is_excluded("package.pem", gc.SECRET_GLOBS)
    assert not gc._is_excluded("src/app.py", gc.SECRET_GLOBS + gc.NOISE_GLOBS)


def test_is_excluded_does_not_eat_dot_prefix() -> None:
    """``rel.lstrip("./")`` 按字符集剥离，会把 ``.env`` 变成 ``env``。

    这是落地时差点写进去的真实缺陷：密钥排除在 Python 侧整个失效，
    而 git 侧仍然排除 —— 于是清单与树对不上，用户看不到任何异常。
    """
    assert gc._is_excluded("./.env", gc.SECRET_GLOBS)
    assert gc._is_excluded("./sub/.env", gc.SECRET_GLOBS)


def test_exclude_spec_covers_root_and_nested() -> None:
    spec = gc._exclude_spec(["node_modules/**"])
    assert ":(exclude,glob)node_modules/**" in spec
    assert ":(exclude,glob)**/node_modules/**" in spec


# ---------------------------------------------------------------------------
# snapshot：基本语义
# ---------------------------------------------------------------------------


def test_snapshot_is_idempotent(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    first = env.snap()
    second = env.snap()
    assert first.tree == second.tree
    assert first.tree == _git(env.root, "rev-parse", "HEAD^{tree}").stdout.decode().strip()


def test_snapshot_sees_uncommitted_edits(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    head_tree = env.snap().tree
    env.write("a.py", "print(2)\n")
    snap = env.snap()
    assert snap.tree != head_tree
    assert gc._read_blob(env.root, snap.tree, "a.py") == b"print(2)\n"


def test_snapshot_without_any_commit(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    snap = env.snap()  # read-tree HEAD 失败 → --empty，不应抛
    assert snap.tree
    assert "a.py" not in snap.tree_paths  # 无 HEAD，未跟踪走 CAS
    assert "a.py" in snap.untracked


def test_snapshot_keeps_tracked_dir_named_dist(env: Env) -> None:
    """噪声排除不能作用于**已跟踪**文件。

    若按 ``dist/**`` 排除已跟踪路径，用户那个叫 ``dist`` 的源码目录会被静默剔掉 ——
    这类丢失最难排查，因为 git 自己认为一切正常。
    """
    env.write("dist/lib.py", "VALUE = 1\n")
    _commit_all(env.root)
    snap = env.snap()
    assert "dist/lib.py" in snap.tree_paths


def test_snapshot_handles_non_ascii_and_space_paths(env: Env) -> None:
    """``-z`` 解析：默认 ``core.quotepath`` 会把中文转成八进制转义。"""
    env.write("中文 文件名.py", "x = 1\n")
    _commit_all(env.root)
    snap = env.snap()
    assert "中文 文件名.py" in snap.tree_paths


# ---------------------------------------------------------------------------
# snapshot：密钥（§10.1）
# ---------------------------------------------------------------------------


def test_untracked_secret_is_not_written_anywhere(env: Env) -> None:
    """未跟踪的 ``.env`` 既不进树，也不进对象库，也不进 CAS。"""
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write(".env", "TOPSECRET=1\n")
    snap = env.snap()

    assert ".env" not in snap.tree_paths
    assert ".env" not in snap.untracked
    assert snap.skipped_secrets >= 1

    # 对象库里不能有任何 blob 含明文。这是 pathspec 排除与
    # "add 后 rm --cached" 的分水岭：后者会留下可被 fsck 捞出的 blob。
    objects = _git(env.root, "cat-file", "--batch-all-objects", "--batch")
    assert b"TOPSECRET" not in objects.stdout


def test_committed_secret_is_removed_from_tree(env: Env) -> None:
    """用户**提交过** ``.env`` 时，``read-tree HEAD`` 会把它带进索引。

    pathspec 排除只作用于"这次要 add 的路径"，管不到索引里已有的条目。
    实测（$TMPDIR/headenv）确认：只加 pathspec 时树里仍然有 ``.env``。
    因此快照必须额外 ``update-index --force-remove``。
    """
    env.write("a.py", "print(1)\n")
    env.write(".env", "TOPSECRET=1\n")
    _commit_all(env.root)
    assert ".env" in _git(env.root, "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()

    snap = env.snap()
    assert ".env" not in snap.tree_paths
    # 工作区文件不能被删掉 —— 我们只摘索引条目。
    assert (env.root / ".env").exists()


def test_oversized_tracked_file_is_excluded(env: Env) -> None:
    """已跟踪的超限文件同样会随 ``read-tree HEAD`` 漏进来，机理与密钥一致。"""
    env.write("small.py", "x = 1\n")
    env.write("big.bin", "y" * 64)
    _commit_all(env.root)

    snap = env.snap(limits=gc.Limits(max_file_bytes=10))
    assert "big.bin" not in snap.tree_paths
    assert "small.py" in snap.tree_paths
    assert snap.skipped_oversized >= 1


# ---------------------------------------------------------------------------
# snapshot：用户已暂存的新文件（落地时新发现的缺陷）
# ---------------------------------------------------------------------------


def test_staged_new_file_is_included(env: Env) -> None:
    """用户 ``git add`` 过、但还没提交的新文件，三处机制都不管。

    * ``read-tree HEAD`` 里没有它；
    * ``add -u`` 只更新"已跟踪"，它不叫已跟踪；
    * ``ls-files -o`` 因为它已进索引，也不算未跟踪。

    实测（$TMPDIR/staged）确认它会**整个消失**。这是最坏的一类丢失：用户已经
    明确表达过"这个文件要留下"。
    """
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("new.py", "print('new')\n")
    assert _git(env.root, "add", "new.py").returncode == 0

    snap = env.snap()
    assert "new.py" in snap.tree_paths
    assert gc._read_blob(env.root, snap.tree, "new.py") == b"print('new')\n"


def test_staged_secret_new_file_is_still_excluded(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write(".env.production", "TOPSECRET=1\n")
    assert _git(env.root, "add", "-f", ".env.production").returncode == 0

    snap = env.snap()
    assert ".env.production" not in snap.tree_paths
    # 用户自己 `add -f` 时 blob 已经落盘，我们无权也不该阻止这个显式操作；
    # 但检查点**不能**再把它引用进树或清单。
    assert ".env.production" not in snap.untracked
    assert ".env.production" not in snap.tree_paths


# ---------------------------------------------------------------------------
# 未跟踪文件：CAS（D4）
# ---------------------------------------------------------------------------


def test_untracked_goes_to_cas_not_objects(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("notes.txt", "agent note UNIQUE_MARKER\n")

    snap = env.snap()
    assert "notes.txt" in snap.untracked
    digest = snap.untracked["notes.txt"]
    store = gc.UntrackedStore(env.store)
    assert store.get(digest) == b"agent note UNIQUE_MARKER\n"
    # 哈希分片：<2 字符>/<剩余 62 字符>
    assert (env.store / digest[:2] / digest[2:]).exists()
    assert len(digest) == 64
    # 不进 git 对象库 —— 这是 D4 选 CAS 而非 add -A 的理由
    objects = _git(env.root, "cat-file", "--batch-all-objects", "--batch")
    assert b"UNIQUE_MARKER" not in objects.stdout


def test_untracked_dedup_by_content(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("one.txt", "same\n")
    env.write("two.txt", "same\n")
    snap = env.snap()
    assert snap.untracked["one.txt"] == snap.untracked["two.txt"]


def test_untracked_respects_gitignore(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    env.write(".gitignore", "ignored.txt\n")
    _commit_all(env.root)
    env.write("ignored.txt", "x\n")
    snap = env.snap()
    # 用户自己的 .gitignore 说了不算数，这里就听它的：检查点是"用户认知中的项目"。
    assert "ignored.txt" not in snap.untracked


def test_untracked_store_detects_corruption(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("notes.txt", "payload\n")
    digest = env.snap().untracked["notes.txt"]
    target = env.store / digest[:2] / digest[2:]
    target.write_bytes(b"tampered\n")
    with pytest.raises(gc.GitError, match="corrupt"):
        gc.UntrackedStore(env.store).get(digest)


# ---------------------------------------------------------------------------
# diff：三分类（§5.2）
# ---------------------------------------------------------------------------


def test_diff_three_classes(env: Env) -> None:
    env.write("keep.py", "v1\n")
    env.write("gone.py", "bye\n")
    _commit_all(env.root)
    env.write("note.txt", "keep me\n")
    target = env.snap()

    env.write("keep.py", "v2\n")
    (env.root / "gone.py").unlink()
    env.write("fresh.py", "hello\n")
    _commit_all(env.root, "second")
    (env.root / "note.txt").unlink()
    env.write("other.txt", "drop\n")
    current = env.snap()

    plan = gc.diff(env.root, current=current, target=target)
    assert "keep.py" in plan.restored
    assert "gone.py" in plan.added
    assert "note.txt" in plan.added
    assert "fresh.py" in plan.removed
    assert "other.txt" in plan.removed
    assert not plan.is_empty()


def test_diff_protected_paths_never_become_removable(env: Env) -> None:
    """命中保护列表的路径只进 ``protected_removed``，绝不进 ``removed``。

    否则回滚 UI 会诱导非技术用户勾选"删除 node_modules"。
    """
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    env.write("cache/x.txt", "temp\n")
    current = env.snap()
    # 手工构造一个"不含 cache/x.txt"的目标快照
    target = gc.Snapshot(tree=current.tree, tree_paths=current.tree_paths, mode="store")

    plan = gc.diff(env.root, current=current, target=target, protected=("cache/**",))
    assert "cache/x.txt" in plan.protected_removed
    assert "cache/x.txt" not in plan.removed


def test_diff_empty_when_unchanged(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    snap = env.snap()
    assert gc.diff(env.root, current=snap, target=snap).is_empty()


# ---------------------------------------------------------------------------
# restore（§5.2 / §14.6）
# ---------------------------------------------------------------------------


def test_restore_rewrites_modified_file(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("a.py", "v2\n")
    result = env.restore(target)
    assert result.ok
    assert "a.py" in result.written
    assert (env.root / "a.py").read_bytes() == b"v1\n"
    assert env.status() == ""


def test_restore_recreates_deleted_directory(env: Env) -> None:
    """恢复 ``added`` 类必须先 ``mkdir -p`` 父目录（§16.4 实测）。"""
    env.write("sub/deep/a.py", "deep\n")
    _commit_all(env.root)
    target = env.snap()

    shutil.rmtree(env.root / "sub")
    result = env.restore(target)
    assert result.ok
    assert "sub/deep/a.py" in result.added
    assert (env.root / "sub/deep/a.py").read_bytes() == b"deep\n"


def test_restore_does_not_delete_by_default(env: Env) -> None:
    """D5 未定案时的安全默认：``removed`` 只报告，不动手。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("new.py", "agent created\n")
    result = env.restore(target)
    assert (env.root / "new.py").exists()
    assert "new.py" in result.skipped_removed
    assert result.deleted == ()


def test_restore_deletes_when_explicitly_allowed(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("new.py", "agent created\n")
    result = env.restore(target, allow_delete=True)
    assert result.deleted == ("new.py",)
    assert not (env.root / "new.py").exists()


def test_restore_single_path(env: Env) -> None:
    env.write("a.py", "v1\n")
    env.write("b.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("a.py", "v2\n")
    env.write("b.py", "v2\n")
    result = env.restore(target, paths=["a.py"])
    assert result.written == ("a.py",)
    assert (env.root / "a.py").read_bytes() == b"v1\n"
    assert (env.root / "b.py").read_bytes() == b"v2\n"


def test_restore_untracked_from_cas(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    env.write("note.txt", "original note\n")
    target = env.snap()

    env.write("note.txt", "agent rewrote this\n")
    result = env.restore(target)
    assert result.ok
    assert "note.txt" in result.written
    assert (env.root / "note.txt").read_bytes() == b"original note\n"


def test_restore_preserves_crlf(env: Env, tmp_path: Path) -> None:
    """``git show`` 会静默把 CRLF 改成 LF；``cat-file --filters`` 才保真（§16.3）。"""
    crlf_env = Env(tmp_path, autocrlf="true")
    crlf_env.write("f.txt", b"line1\r\nline2\r\n")
    _commit_all(crlf_env.root)
    # 仓库内存的是规范化后的 LF
    assert _git(crlf_env.root, "cat-file", "blob", "HEAD:f.txt").stdout == b"line1\nline2\n"

    target = crlf_env.snap()
    crlf_env.write("f.txt", b"line1\r\nline2\r\nMOD\r\n")

    result = crlf_env.restore(target)
    assert result.ok
    assert (crlf_env.root / "f.txt").read_bytes() == b"line1\r\nline2\r\n"
    assert crlf_env.status() == ""


def test_restore_reports_partial_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落地阶段失败必须逐文件报告，且**不回退**已落地的文件（§14.6）。

    真实样本是只读文件让 ``cp`` 报 ``Permission denied``，其余文件已经落地。
    这里用注入失败来稳定复现同一分支 —— 反转已落地的文件风险更大。
    """
    env.write("a.py", "v1\n")
    env.write("b.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()
    env.write("a.py", "v2\n")
    env.write("b.py", "v2\n")

    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src: str, dst: str) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("Permission denied")
        return real_move(src, dst)

    monkeypatch.setattr(gc.shutil, "move", flaky)
    result = env.restore(target)

    assert not result.ok
    assert len(result.failed) == 1
    assert len(result.written) + len(result.added) == 1
    landed = {*result.written, *result.added}
    assert landed <= {"a.py", "b.py"}
    assert (env.root / next(iter(landed))).read_bytes() == b"v1\n"


def test_restore_does_not_clobber_caller_index(env: Env) -> None:
    """``restore`` 只用一个派生的探测 index，不动调用方的热态 index。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    sentinel = b"SENTINEL-INDEX-BYTES"
    env.index.write_bytes(sentinel)
    env.write("a.py", "v2\n")

    env.restore(target)
    assert env.index.read_bytes() == sentinel


# ---------------------------------------------------------------------------
# commit / ref（P1 的接口，但纯函数可测）
# ---------------------------------------------------------------------------


def test_commit_tree_parents_previous_checkpoint(env: Env) -> None:
    """父提交是上一个检查点，不是 HEAD（§4.2）。

    用 HEAD 做父会让检查点链与用户分支纠缠：用户 rebase 之后，
    检查点的祖先关系就代表不了"会话的时间顺序"。
    """
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    tree = env.snap().tree

    first = gc.commit_tree(env.root, tree=tree, head_sha="deadbeef")
    second = gc.commit_tree(env.root, tree=tree, parent=first)

    parent = _git(env.root, "rev-parse", f"{second}^").stdout.decode().strip()
    assert parent == first
    body = _git(env.root, "log", "-1", "--format=%B", first).stdout.decode()
    assert "Chiron-Head: deadbeef" in body


def test_checkpoint_ref_is_hidden_from_branches(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    tree = env.snap().tree
    sha = gc.commit_tree(env.root, tree=tree)

    ref = gc.checkpoint_ref("sess-1", 1, "turn")
    assert ref == "refs/chiron/checkpoints/sess-1/000001-turn"
    gc.write_ref(env.root, ref, sha)

    assert gc.read_ref(env.root, ref) == sha
    branches = _git(env.root, "for-each-ref", "refs/heads").stdout.decode()
    assert "chiron" not in branches


def test_write_ref_refuses_to_overwrite(env: Env) -> None:
    """CAS 写入：旧值必须是空，因此物理上不可能覆盖已有检查点。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    tree = env.snap().tree
    first = gc.commit_tree(env.root, tree=tree)
    second = gc.commit_tree(env.root, tree=tree, message="second")

    ref = gc.checkpoint_ref("sess-1", 1, "turn")
    gc.write_ref(env.root, ref, first)
    with pytest.raises(gc.GitError, match="update-ref"):
        gc.write_ref(env.root, ref, second)
    assert gc.read_ref(env.root, ref) == first


def test_read_ref_returns_none_when_missing(env: Env) -> None:
    """ref 缺失是**正常**的修复场景，不能抛异常。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    assert gc.read_ref(env.root, "refs/chiron/checkpoints/nope/000001-turn") is None


def test_checkpoint_survives_gc(env: Env) -> None:
    """refs/chiron/** 是真实 ref，因此 gc 不会回收它（实测 SURVIVED_GC）。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    tree = env.snap().tree
    sha = gc.commit_tree(env.root, tree=tree)
    ref = gc.checkpoint_ref("sess-1", 1, "turn")
    gc.write_ref(env.root, ref, sha)

    _git(env.root, "reflog", "expire", "--expire=now", "--all")
    _git(env.root, "gc", "--prune=now", "--quiet")

    assert gc.read_ref(env.root, ref) == sha
    assert _git(env.root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


# ---------------------------------------------------------------------------
# D7 预算
# ---------------------------------------------------------------------------


def test_min_budget_is_4gb() -> None:
    """D7 下限是 4 GB —— 4 GB 从"上限"变成了"下限"。"""
    assert gc.MIN_CHECKPOINT_BYTES == 4 * 1024**3
    assert gc.Limits().max_checkpoint_bytes == 4 * 1024**3


def test_adaptive_limits_never_below_4gb() -> None:
    gb4 = 4 * 1024**3
    assert gc.adaptive_limits(0).max_checkpoint_bytes == gb4
    assert gc.adaptive_limits(1024).max_checkpoint_bytes == gb4
    # 64 MB 级仓库按 ×10 = 640 MB，低于下限 → 抬到 4 GB
    assert gc.adaptive_limits(64 * 1024 * 1024).max_checkpoint_bytes == gb4
    # 大仓库可以超过下限：1 GB 对象 → 10 GB 预算
    assert gc.adaptive_limits(1024**3).max_checkpoint_bytes == 10 * 1024**3


def test_adaptive_limits_override_respects_floor() -> None:
    gb4 = 4 * 1024**3
    # 用户配 1 GB → 抬到 4 GB（下限不是建议值）
    assert gc.adaptive_limits(0, override=1024**3).max_checkpoint_bytes == gb4
    # 用户配 8 GB → 用 8 GB
    assert gc.adaptive_limits(0, override=8 * 1024**3).max_checkpoint_bytes == 8 * 1024**3
    # 非法 override 回退自适应
    assert gc.adaptive_limits(0, override=None).max_checkpoint_bytes == gb4


def test_limits_resolve_from_settings() -> None:
    gb4 = 4 * 1024**3
    assert gc.Limits.resolve(None).max_checkpoint_bytes == gb4
    assert gc.Limits.resolve({}).max_checkpoint_bytes == gb4
    # 人类可读写法必须认：用户填 "8GB" 却被静默忽略是最难解释的失效
    assert gc.Limits.resolve({"max_checkpoint_bytes": "8gb"}).max_checkpoint_bytes == 8 * 1024**3
    assert gc.Limits.resolve({"max_checkpoint_bytes": "8192 MB"}).max_checkpoint_bytes == 8 * 1024**3
    # 低于下限被抬到 4 GB
    assert gc.Limits.resolve({"max_checkpoint_bytes": "1gb"}).max_checkpoint_bytes == gb4
    # 非法值回退默认，而不是抛异常
    assert gc.Limits.resolve({"max_checkpoint_bytes": "lots"}).max_checkpoint_bytes == gb4

    assert gc.Limits.resolve({"max_file_bytes": "1mb"}).max_file_bytes == 1024**2
    assert gc.Limits.resolve({"max_file_bytes": 4096}).max_file_bytes == 4096


def test_limits_resolve_rejects_bool_and_negative() -> None:
    """``bool`` 是 ``int`` 的子类，不先排除会把 ``True`` 当成 1 字节。"""
    default = gc.Limits().max_file_bytes
    assert gc.Limits.resolve({"max_file_bytes": True}).max_file_bytes == default
    assert gc.Limits.resolve({"max_file_bytes": -5}).max_file_bytes == default
    assert gc.Limits.resolve({"max_file_bytes": "abc"}).max_file_bytes == default


def test_limits_skip_oversized_untracked(env: Env) -> None:
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("huge.bin", "z" * 64)
    snap = env.snap(limits=gc.Limits(max_file_bytes=10))
    assert "huge.bin" not in snap.untracked
    assert snap.skipped_oversized >= 1


# ---------------------------------------------------------------------------
# D5：删除型回滚「可自定义，但默认禁止」
# ---------------------------------------------------------------------------


def test_rollback_policy_defaults_to_forbidden() -> None:
    assert gc.RollbackPolicy().allow_delete is False
    assert gc.RollbackPolicy.from_settings(None).allow_delete is False
    assert gc.RollbackPolicy.from_settings({}).allow_delete is False


def test_rollback_policy_accepts_explicit_opt_in() -> None:
    assert gc.RollbackPolicy.from_settings({"allow_delete": True}).allow_delete is True
    assert gc.RollbackPolicy.from_settings({"allow_delete": "true"}).allow_delete is True
    assert gc.RollbackPolicy.from_settings({"allow_delete": "1"}).allow_delete is True
    assert gc.RollbackPolicy.from_settings({"allow_delete": "off"}).allow_delete is False


def test_rollback_policy_fails_safe_on_garbage() -> None:
    """解析不了的配置一律当作"没配置"，因此落在"禁止删除"。"""
    assert gc.RollbackPolicy.from_settings({"allow_delete": "maybe"}).allow_delete is False
    assert gc.RollbackPolicy.from_settings({"allow_delete": 42}).allow_delete is False
    assert gc.RollbackPolicy.from_settings({"allow_delete": None}).allow_delete is False


def test_restore_honours_settings_opt_in(env: Env) -> None:
    """用户在设置里打开后，``restore`` 才真的删除。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("agent_new.py", "created by agent\n")
    result = env.restore(target, settings={"allow_delete": True})
    assert result.deleted == ("agent_new.py",)
    assert not (env.root / "agent_new.py").exists()


def test_restore_ignores_garbage_settings(env: Env) -> None:
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("agent_new.py", "created by agent\n")
    result = env.restore(target, settings={"allow_delete": "yes-please"})
    assert result.deleted == ()
    assert (env.root / "agent_new.py").exists()
    assert "agent_new.py" in result.skipped_removed


def test_explicit_argument_beats_settings(env: Env) -> None:
    """内部 API 传显式值时以它为准，不被用户设置悄悄反转。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("agent_new.py", "created by agent\n")
    result = env.restore(target, allow_delete=True, settings={"allow_delete": False})
    assert result.deleted == ("agent_new.py",)


def test_delete_prunes_empty_parent_dirs(env: Env) -> None:
    """删完文件要收空目录：git 不跟踪空目录，``status`` 干净但用户看到空壳。"""
    env.write("a.py", "v1\n")
    _commit_all(env.root)
    target = env.snap()

    env.write("scratch/deep/tmp.txt", "temp\n")
    result = env.restore(target, allow_delete=True)

    assert result.deleted == ("scratch/deep/tmp.txt",)
    assert not (env.root / "scratch").exists()
    assert "scratch/deep" in result.pruned_dirs
    # 仓库根绝不能被删
    assert env.root.exists()



def test_untracked_mode_tree_puts_everything_in_tree(env: Env) -> None:
    """``untracked_mode="tree"`` 是 D4 的另一条路（并入 git 对象库）。"""
    env.write("a.py", "print(1)\n")
    _commit_all(env.root)
    env.write("notes.txt", "in tree mode\n")

    snap = env.snap(untracked_mode="tree")
    assert "notes.txt" in snap.tree_paths
    assert snap.untracked == {}
    assert gc._read_blob(env.root, snap.tree, "notes.txt") == b"in tree mode\n"
