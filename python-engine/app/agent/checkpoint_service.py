"""检查点服务：把 `git_checkpoint` 的原语串成"一次会话节点快照"。

**fail-open 是这里唯一不可协商的语义**

检查点是**辅助能力**，不是对话的前置条件。任何一次检查点失败——磁盘满、索引被占用、
用户把 `.git` 删了、权限不足——都只能记录并放行，绝不能中断用户这一轮对话。
因此本模块所有公开函数都不抛异常，失败返回 ``None`` 并留下日志。

这与 `git_checkpoint` 的取向**相反**：那层的 ``GitError`` 必须抛出，因为它是被单测
与回滚逻辑直接依赖的原语，静默失败会让上层拿到一个"看起来成功其实没有"的结果。

**seq 由调用方给，不在这里自增**

序号必须落库且单调（``project_checkpoints.seq``），自增只能有一个权威来源。
服务层若自己数``refs/chiron/checkpoints/<session>/`` 下的条目，会把"ref 被 gc 掉"
误判成"序号回退"，进而覆盖已有检查点。调用方（DB 事务里已拿到 ``max(seq)+1``）
传进来即可。

**父提交是上一个检查点，不是 HEAD**

理由：用 HEAD 做父会让检查点链与用户分支历史纠缠，用户 rebase 之后
祖先关系就不代表会话的时间顺序了。当前 HEAD 另存 ``head_sha`` 供审计。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agent import git_checkpoint as gc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckpointRecord:
    """一次检查点**尝试**的结果（成功或失败），字段与 ``project_checkpoints`` 一一对应。

    ``state`` 非 ``"ok"`` 时表示这次没能保护成功：``commit_sha`` / ``tree_sha`` /
    ``ref_name`` 为空，``error`` 里是可读原因。这类记录**必须上报**——列表端刻意
    不过滤 ``state``，时间线要能显示"这一刻没保护成功"，否则用户会以为还原点是完整的。
    """

    seq: int
    kind: str
    commit_sha: str
    tree_sha: str
    parent_sha: str | None
    head_sha: str | None
    ref_name: str
    file_count: int
    skipped_count: int
    untracked_count: int
    #: ``"ok"`` | ``"failed"``。失败态由 :func:`create_checkpoint` 填。
    state: str = "ok"
    #: 失败原因（人类可读）。成功时为空。
    error: str = ""
    #: 稳定错误码，供前端分类/翻译（``git`` / ``io`` / ``unknown``）。
    error_code: str = ""
    #: 未跟踪文件的 ``(相对路径, sha256)`` 清单，按路径排序。
    #:
    #: 这个清单必须跟记录一起带走：网关的 `checkpoint_untracked` 表靠它落库，
    #: 而回滚未跟踪文件时要从 CAS 按 sha256 取回内容。只带 `untracked_count`
    #: （一个数字）看起来够用，实际会让"快照里有这些文件、回滚时一个也找不回"。
    untracked: tuple[tuple[str, str], ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.state == "ok"


def _classify_error(exc: BaseException) -> str:
    """把异常归到一个稳定错误码。前端据此分类，比解析自然语言可靠。

    只分三类：git 层失败（仓库损坏/权限/ref 冲突）、文件系统失败（磁盘满/占用）、
    其余归 unknown。再细的分法对用户没有额外行动价值。
    """
    if isinstance(exc, gc.GitError):
        return "git"
    if isinstance(exc, OSError):
        return "io"
    return "unknown"


def _head_sha(root: Path, index_file: Path) -> str | None:
    """当前 HEAD；空仓库或非 git 仓库返回 None（不是错误）。"""
    try:
        proc = gc._run_git(root, "rev-parse", "--verify", "--quiet", "HEAD", env=gc._git_env(index_file))
        if proc.returncode != 0:
            return None
        sha = proc.stdout.decode("ascii", errors="replace").strip()
        return sha or None
    except (OSError, ValueError):
        return None


def shadow_git_dir(root: Path, state_dir: Path) -> Path:
    """普通目录专用的、位于检查点状态目录中的裸 Git 仓库。

    用户目录里绝不创建 ``.git``。路径按解析后的项目根散列，保证同一项目在重启后
    仍能命中原来的检查点链，同时避免把用户路径直接暴露在目录名里。
    """
    key = hashlib.sha256(os.fsencode(str(Path(root).resolve()))).hexdigest()
    return Path(state_dir) / "shadow" / key


def _plain_manifest(
    root: Path,
    *,
    limits: gc.Limits,
    store: gc.UntrackedStore | None = None,
) -> tuple[dict[str, str], int, int, int]:
    """扫描非 Git 目录，返回 ``(路径->sha256, 密钥跳过数, 超大跳过数, 总字节)``。

    这里复用 Git 快照的密钥、噪音与容量规则，但不调用 ``git add``，因此不会在
    用户目录留下索引或对象。符号链接/junction 不跟随，避免把工作区外内容收进来。
    """
    root = Path(root)
    manifest: dict[str, str] = {}
    skipped_secrets = skipped_oversized = total = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            rel = child.relative_to(root).as_posix()
            if child.is_symlink() or gc._is_excluded(f"{rel}/.chiron-probe", gc.NOISE_GLOBS):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = current / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                continue
            if gc._is_excluded(rel, gc.SECRET_GLOBS):
                skipped_secrets += 1
                continue
            if rel == ".git" or gc._is_excluded(rel, gc.NOISE_GLOBS):
                continue
            try:
                size = path.stat().st_size
                if size > limits.max_file_bytes or total + size > limits.max_checkpoint_bytes:
                    skipped_oversized += 1
                    continue
                data = path.read_bytes()
                if len(data) > limits.max_file_bytes or total + len(data) > limits.max_checkpoint_bytes:
                    skipped_oversized += 1
                    continue
            except OSError:
                skipped_oversized += 1
                continue
            total += len(data)
            manifest[rel] = store.put(data) if store is not None else hashlib.sha256(data).hexdigest()
    return manifest, skipped_secrets, skipped_oversized, total


def _ensure_shadow_tree(root: Path, git_dir: Path, index_file: Path) -> str:
    """初始化影子裸仓库并返回空 tree；提交/ref 的读写都不碰用户仓库。"""
    if not (git_dir / "HEAD").exists():
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        gc._must(
            gc._run_git(root, "init", "--bare", "-q", "--template=", str(git_dir)),
            "git init shadow",
        )
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = gc._git_env(index_file)
    gc._must(gc._run_git(git_dir, "read-tree", "--empty", env=env), "read-tree empty shadow")
    return gc._must(gc._run_git(git_dir, "write-tree", env=env), "write-tree empty shadow").decode(
        "ascii"
    ).strip()


def _next_shadow_seq(git_dir: Path, session_id: str) -> int:
    """影子仓库里的下一序号。

    ``gc.next_checkpoint_seq`` 会先要求 ``--show-toplevel``，裸仓库不满足；钩子又看不到
    影子 ref，因而会重复传 1。必须直接读影子 ref，才能避免 CAS 写入同一次序号失败。
    """
    prefix = gc.checkpoint_ref_prefix(session_id)
    proc = gc._run_git(git_dir, "for-each-ref", "--format=%(refname)", prefix)
    if proc.returncode != 0:
        raise gc.GitError("cannot list shadow checkpoint refs: " + proc.stderr.decode(errors="replace"))
    best = 0
    for ref in proc.stdout.decode("utf-8", errors="replace").splitlines():
        tail = ref[len(prefix):] if ref.startswith(prefix) else ""
        head, _, _ = tail.partition("-")
        if head.isdigit():
            best = max(best, int(head))
    return best + 1


def _normal_manifest_path(path: str) -> str:
    text = str(path).replace("\\", "/").strip()
    parts = text.split("/")
    if (
        not text
        or text.startswith("/")
        or (len(text) > 1 and text[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise gc.GitError(f"invalid checkpoint path: {path!r}")
    return text


def _plain_destination(root: Path, rel: str) -> Path:
    """安全创建普通目录恢复的父目录，拒绝符号链接/junction 逃逸。

    返回值是 ``root/<rel>`` 本身 —— 注意必须拼在**逐级下钻后的** ``current`` 上，
    而不是 ``root / parts[-1]``：后者会把 ``sub/b.txt`` 落到 ``root/b.txt``，
    同时 ``added`` 里报的仍是 ``sub/b.txt``（"看起来恢复了、文件却在别处"）。
    """
    root_real = Path(root).resolve()
    parts = rel.split("/")
    current = root_real
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise gc.GitError(f"restore path escapes project root through symlink: {rel}")
        current.mkdir(exist_ok=True)
        if not current.resolve().is_relative_to(root_real):
            raise gc.GitError(f"restore path escapes project root: {rel}")
    dest = current / parts[-1]
    if not dest.parent.resolve().is_relative_to(root_real):
        raise gc.GitError(f"restore path escapes project root: {rel}")
    return dest


def restore_plain_directory(
    root: Path,
    *,
    manifest: Mapping[str, str],
    store_root: Path,
    paths: Sequence[str] | None = None,
    allow_delete: bool = False,
) -> gc.RestoreResult:
    """把普通目录恢复到 CAS 清单；两阶段取回/落地，默认不删除新增文件。"""
    root = Path(root)
    store = gc.UntrackedStore(Path(store_root))
    target = {_normal_manifest_path(path): str(digest) for path, digest in manifest.items() if str(digest)}
    current, _, _, _ = _plain_manifest(root, limits=gc.Limits.resolve(), store=None)
    wanted = set(paths) if paths is not None else None

    removed: list[str] = []
    protected: list[str] = []
    for rel in current.keys() - target.keys():
        if gc._is_excluded(rel, gc.PROTECTED_GLOBS):
            protected.append(rel)
        else:
            removed.append(rel)

    todo = sorted(
        rel
        for rel, digest in target.items()
        if (wanted is None or rel in wanted) and current.get(rel) != digest
    )
    written: list[str] = []
    added: list[str] = []
    failed: list[tuple[str, str]] = []
    stage = Path(tempfile.mkdtemp(prefix="chiron-restore-plain-"))
    staged: list[tuple[str, Path, bool]] = []
    try:
        # 第一阶段只读 CAS 并全部落临时区；缺失/损坏 blob 不触发工作区写入。
        for i, rel in enumerate(todo):
            try:
                blob = store.get(target[rel])
                staged_path = stage / f"{i}.bin"
                staged_path.write_bytes(blob)
                staged.append((rel, staged_path, rel in current))
            except (gc.GitError, OSError) as exc:
                failed.append((rel, str(exc)))
        # 第二阶段才改用户文件，逐文件报告占用/权限错误。
        for rel, blob, existed in staged:
            try:
                dest = _plain_destination(root, rel)
                fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".chiron-restore-")
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(blob.read_bytes())
                    # mkstemp 固定 0600；覆盖现有文件时保留其权限位，新增文件
                    # 使用普通源码文件权限，避免 POSIX 下回滚把 0755 脚本变成 0600。
                    mode = stat.S_IMODE(dest.stat().st_mode) if dest.is_file() else 0o644
                    os.chmod(tmp_name, mode)
                    os.replace(tmp_name, dest)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                (written if existed else added).append(rel)
            except (OSError, gc.GitError) as exc:
                failed.append((rel, str(exc)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    deleted: list[str] = []
    skipped: list[str] = []
    if allow_delete:
        for rel in sorted(removed):
            if wanted is not None and rel not in wanted:
                continue
            try:
                (root / rel).unlink()
                deleted.append(rel)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failed.append((rel, str(exc)))
        pruned = gc._prune_empty_dirs(root, deleted)
    else:
        skipped = sorted(rel for rel in removed if wanted is None or rel in wanted)
        pruned = []

    return gc.RestoreResult(
        written=tuple(written),
        added=tuple(added),
        deleted=tuple(deleted),
        pruned_dirs=tuple(pruned),
        failed=tuple(failed),
        skipped_removed=tuple(skipped),
        protected_removed=tuple(sorted(protected)),
    )


def create_checkpoint(
    root: Path,
    *,
    session_id: str,
    seq: int,
    kind: str,
    state_dir: Path,
    parent_sha: str | None = None,
    settings: Mapping[str, object] | None = None,
) -> CheckpointRecord | None:
    """在 ``root`` 上创建一次检查点；失败返回 None，**从不抛异常**。

    ``state_dir`` 必须位于工作区**之外**（模块头部第 1 条硬约束：索引文件若落在
    仓库里，会被自己的 ``git add`` 收进快照，于是每次 write-tree 都不同）。
    索引按 ``<session_id>`` 分开，避免多会话并发时互相覆盖暂存状态。

    需要**知道失败原因**（要落库成"这一刻没保护成功"）的调用方用
    :func:`attempt_checkpoint` —— 本函数保持"失败即 None"的既有契约。
    """
    rec = attempt_checkpoint(
        root,
        session_id=session_id,
        seq=seq,
        kind=kind,
        state_dir=state_dir,
        parent_sha=parent_sha,
        settings=settings,
    )
    return rec if rec is not None and rec.succeeded else None


def attempt_checkpoint(
    root: Path,
    *,
    session_id: str,
    seq: int,
    kind: str,
    state_dir: Path,
    parent_sha: str | None = None,
    settings: Mapping[str, object] | None = None,
) -> CheckpointRecord | None:
    """同 :func:`create_checkpoint`，但**失败也返回记录**（``state='failed'``）。

    为什么需要两种返回值：时间线必须能显示"这一刻没能保护成功"，那要求失败也上报一行；
    而既有调用方与测试依赖"失败 = None"这个更简单的契约。分成两个函数，两边都不迁就。

    返回 ``None`` 只剩两种情况：参数不成立（空 ``session_id``），或不是"建点失败"
    而是压根没有可报的东西 —— 这些都不该在时间线上留一行。

    **普通目录也受支持**：项目不在任何 Git 仓库中时，文件内容仍进入既有 CAS，
    提交/ref 则写入 ``state_dir`` 下的影子裸仓库。用户目录不会新增 ``.git``、索引或
    对象，已有仓库也仍走原路径（包括 monorepo 子目录前缀裁剪）。
    """
    if not session_id:
        logger.warning("checkpoint skipped: empty session_id")
        return None

    index_file = state_dir / f"snap-{session_id}.idx"
    store_root = state_dir / "untracked"

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            gc.resolve_layout(root)
            plain = False
        except gc.GitError:
            plain = True

        if plain:
            if state_dir.resolve().is_relative_to(Path(root).resolve()):
                raise gc.GitError("checkpoint state directory must be outside the project root")
            git_dir = shadow_git_dir(root, state_dir)
            shadow_index = state_dir / "shadow-index" / f"snap-{session_id}.idx"
            tree = _ensure_shadow_tree(root, git_dir, shadow_index)
            seq = max(seq, _next_shadow_seq(git_dir, session_id))
            manifest, skipped_secrets, skipped_oversized, _ = _plain_manifest(
                root,
                limits=gc.Limits.resolve(settings),
                store=gc.UntrackedStore(store_root),
            )
            snap = gc.Snapshot(
                tree=tree,
                untracked=manifest,
                skipped_secrets=skipped_secrets,
                skipped_oversized=skipped_oversized,
            )
            head_sha = None
            commit_root = git_dir
            commit_index = shadow_index
        else:
            head_sha = _head_sha(root, index_file)
            snap = gc.snapshot(
                root,
                index_file=index_file,
                store_root=store_root,
                settings=settings,
            )
            commit_root = root
            commit_index = index_file

        commit = gc.commit_tree(
            commit_root,
            tree=snap.tree,
            parent=parent_sha,
            message=f"chiron checkpoint {session_id} #{seq} ({kind})",
            head_sha=head_sha,
            index_file=commit_index,
        )
        ref = gc.checkpoint_ref(session_id, seq, kind)
        gc.write_ref(commit_root, ref, commit, index_file=commit_index)
    except Exception as exc:  # noqa: BLE001 — fail-open 是本模块的契约
        # 宽泛捕获是刻意的：这里的失败原因几乎无穷（OSError、GitError、权限、
        # 编码），而**每一种**的处理方式都相同 —— 记录后放行，让对话继续。
        #
        # 但"放行"不等于"当作没发生"：返回一条 ``state='failed'`` 的记录让调用方
        # 照常上报，时间线才能显示"这一刻没能保护成功"。此前直接返回 None，失败
        # 只留在引擎日志里 —— 用户的时间线上什么都不缺，反而更难发现。
        logger.warning("checkpoint failed session=%s seq=%s kind=%s: %s", session_id, seq, kind, exc)
        return CheckpointRecord(
            seq=seq,
            kind=kind,
            commit_sha="",
            tree_sha="",
            parent_sha=parent_sha,
            head_sha=None,
            ref_name="",
            file_count=0,
            skipped_count=0,
            untracked_count=0,
            state="failed",
            # 截断到 500 字符：网关侧 error_msg 是 text，但一条完整 traceback 对用户
            # 没有行动价值，真正的定位线索在引擎日志里。
            error=str(exc)[:500],
            error_code=_classify_error(exc),
        )

    return CheckpointRecord(
        seq=seq,
        kind=kind,
        commit_sha=commit,
        tree_sha=snap.tree,
        parent_sha=parent_sha,
        head_sha=head_sha,
        ref_name=ref,
        file_count=len(snap.untracked) if plain else len(snap.tree_paths),
        # 两类"没被收进快照"的路径分开报：秘密是**主动排除**（用户不该见到它进快照
        # 的提示），超大文件是**容量所限**（用户需要知道自己的文件没被保护）。
        # 注意这两个字段是**计数**（int），不是路径集合。
        skipped_count=snap.skipped_secrets + snap.skipped_oversized,
        untracked_count=len(snap.untracked),
        # 按路径排序后带走：网关落表不关心顺序，但固定顺序让重复上报的清单
        # 在测试与日志里可比对，也让"同一快照两次上报"的 diff 干净。
        untracked=tuple(sorted(snap.untracked.items())),
    )
