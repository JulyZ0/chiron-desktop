"""检查点维护原语：**回滚差异预览** 与 **未跟踪 CAS 回收（GC）**。

这两件事都需要同一份"外部事实"——PG 里的 `project_checkpoints` /
`checkpoint_untracked`——所以放在一层，但**本模块不连数据库**：引用集合由调用方
（``app/api/internal_checkpoints.py``）查出来传进来。理由与 ``git_checkpoint``
一致：纯函数才能在临时目录里完整单测，而"GC 删错东西"是这类代码里唯一不可接受的
缺陷，必须有能在没有 PG、没有网关的机器上跑起来的回归测试。

## 差异预览为什么不能用 ``restore()``

``restore()`` 会**先对当前工作区做一次快照**再落地 —— 快照这一步本身没问题，
但它写的是持久化 CAS（``state_dir/untracked``）。预览是**只读**操作：用户可能点开
五个还原点的差异再关掉，不该因此往磁盘里写五个快照的 blob。所以这里用**临时目录**
里的索引与 CAS，``finally`` 里整个删掉：工作区一个字节都不动，磁盘上也不留痕。

## GC 的引用集合为什么必须**全局**取

``checkpoint_state_dir()/untracked`` 是**所有项目共享**的一个内容寻址存储
（``%LOCALAPPDATA%/chiron/checkpoints/untracked``）。若只按"当前 root 里的会话"
算引用，另一个项目正在用的 blob 就会因为"在我这次扫描里没人引用"被删掉 ——
表现为另一个项目的还原点回滚时报找不到 blob，而现场已经远离真因。

因此：**引用集合 = 全库 `checkpoint_untracked` 的 sha256**，与 root 无关。
root 只用来枚举 `refs/chiron/checkpoints/**` 做扫描统计（"扫了多少个点"）。

## 读不到引用集合 ⇒ 放弃，不删

数据库不可用、查询失败时**必须中止**：把"查不到"当成"没人引用"等于把整个 CAS
清空。这条在 :func:`gc_plan` 的调用契约里写成硬要求（调用方查失败就不调它），
并在 HTTP 层显式返回 503。

## 原子性

逐文件 ``unlink``，没有事务。安全性来自**引用集合在删除前一次性冻结**：删掉的
永远是"冻结时刻未被引用"的摘要，而 CAS 是内容寻址的 —— 同一摘要不会因为删除
顺序而变成"新的引用"。单个文件删除失败（占用/权限）只记入 ``failed``，不影响
其它文件；重复执行是幂等的（已经不存在的文件直接跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.agent import git_checkpoint as gc

# ---------------------------------------------------------------------------
# 差异预览
# ---------------------------------------------------------------------------

#: commit / tree 的校验。**必须**校验再拼进 git 命令：这个值虽然来自数据库，
#: 但形状校验是唯一能挡住"``--output=/etc/passwd`` 被当成 revision"的一层。
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class PreviewResult:
    """回滚前的前瞻：**只描述**，不落地。

    字段与 ``restore`` 的返回对齐（``restored`` / ``added`` / ``removed`` /
    ``protected_removed`` / ``unsupported``），前端因此可以用同一套渲染逻辑。
    """

    restored: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    protected_removed: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    #: ``True`` = 这个检查点的未跟踪清单在库里读不到（或没有 checkpoint_id）。
    #: 此时预览**只覆盖 git 跟踪的文件**，未跟踪那部分既不算 added 也不算 removed。
    #: 必须如实告诉用户：否则"预览看起来没有差异"会被理解成"回滚不会动任何东西"。
    untracked_omitted: bool = False
    #: 当前工作区快照的规模（文件数 / 字节数）。预览抽屉头部显示它，
    #: 用户点之前就知道自己面对多大的工作区。
    current_files: int = 0
    current_bytes: int = 0
    #: 目标快照的规模。null 表示读不到（tree 不存在等）。
    target_files: int = 0
    target_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "restored": list(self.restored),
            "added": list(self.added),
            "removed": list(self.removed),
            "protected_removed": list(self.protected_removed),
            "unsupported": list(self.unsupported),
            "untracked_omitted": self.untracked_omitted,
            "current_files": self.current_files,
            "current_bytes": self.current_bytes,
            "target_files": self.target_files,
            "target_bytes": self.target_bytes,
        }


def _tree_entry_sizes(root: Path, tree: str, *, prefix: str = "") -> dict[str, int]:
    """``ls-tree -r -l`` 的 ``path -> size``（只含普通文件）。

    ``-l`` 给出 blob 体积；symlink 的 size 是链接目标长度、gitlink 是 ``-``，
    两者都不算"工作区字节"，因此只取 ``100644`` / ``100755``（口径与
    :func:`git_checkpoint._tree_entries` 一致）。

    ``prefix`` 非空时按 ``--full-name`` 列出并剥前缀（monorepo 子目录），
    否则默认输出会被 cwd 过滤成只剩子目录 —— 而这里只用于求和，过滤与否都会
    让"当前工作区有多大"这个数字失真。键与 :func:`git_checkpoint._tree_entries`
    同为项目根口径，调用方可以互换。
    """
    args = ["ls-tree", "-r", "-l", "-z"]
    if prefix:
        args.append("--full-name")
    proc = gc._run_git(root, *args, tree)
    if proc.returncode != 0:
        return {}
    out: dict[str, int] = {}
    for item in proc.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, raw_path = item.partition(b"\t")
        parts = meta.split()
        if len(parts) < 4:
            continue
        if parts[0].decode("ascii", errors="replace") not in ("100644", "100755"):
            continue
        try:
            size = int(parts[3])
        except ValueError:
            continue
        path = os.fsdecode(raw_path).replace(os.sep, "/")
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        out[path] = size
    return out


def _store_bytes(store_root: Path, digests: Iterable[str]) -> int:
    """CAS 里这些摘要的体积合计；缺失的按 0 计（不抛）。"""
    total = 0
    for digest in digests:
        try:
            total += (Path(store_root) / digest[:2] / digest[2:]).stat().st_size
        except OSError:
            continue
    return total


def preview_diff(
    root: Path,
    *,
    commit_sha: str,
    untracked: Mapping[str, str] | None,
    state_dir: Path,
    session_id: str = "",
    settings: Mapping[str, object] | None = None,
) -> PreviewResult:
    """算出"回到 ``commit_sha``"需要动哪些文件；**绝不修改工作区**。

    ``untracked`` 是**目标快照**的 ``path -> sha256``（来自 PG 的
    ``checkpoint_untracked``）。传 ``None`` 表示读不到清单 —— 此时
    ``untracked_omitted=True``，预览只覆盖 git 跟踪的文件。

    只读性由三点保证：

    * 索引与 CAS 都落在 ``tempfile.mkdtemp()``（工作区之外，且非持久化 CAS）；
    * 不调用 ``restore()`` / 任何写文件的 git 命令；
    * ``finally`` 里整体删除临时目录。

    ``state_dir`` 只用于**确定仓库位置的口径提示**（当 ``session_id`` 为空时给
    临时索引命名），不写入它 —— 预览不该污染持久化 CAS。
    """
    root = Path(root)
    commit = (commit_sha or "").strip().lower()
    if not _SHA_RE.match(commit):
        raise gc.GitError(f"invalid commit sha: {commit_sha!r}")

    # 先把仓库布局解析掉：后续一切路径都是"项目根口径"，父仓库内容不进快照。
    layout = gc.resolve_layout(root)

    proc = gc._run_git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if proc.returncode != 0:
        raise gc.GitError(
            f"checkpoint commit not found in this repository: {commit_sha}"
        )
    tree = gc._must(
        gc._run_git(root, "rev-parse", "--verify", f"{commit}^{{tree}}"), "rev-parse tree"
    ).decode("ascii").strip()

    workdir = Path(tempfile.mkdtemp(prefix="chiron-preview-"))
    try:
        current = gc.snapshot(
            root,
            # 临时索引：既满足"索引必须在工作区之外"，又不会覆盖热态快照索引。
            index_file=workdir / "preview.idx",
            store_root=workdir / "untracked",
            settings=settings,
        )
        current_files = len(current.tree_paths) + len(current.untracked)
        current_bytes = sum(
            _tree_entry_sizes(root, current.tree, prefix=layout.prefix).values()
        ) + _store_bytes(workdir / "untracked", current.untracked.values())

        # 目标树的路径按项目根口径列（``prefix`` 用于剥掉 monorepo 前缀）。
        target_paths, target_special = gc._tree_entries(root, tree, prefix=layout.prefix)
        target_sizes = _tree_entry_sizes(root, tree, prefix=layout.prefix)
        omitted = untracked is None
        # 清单读不到时把**当前**清单当成目标：未跟踪那部分因此既不 added 也不
        # removed。用空字典代替会让每个当前存在的未跟踪文件都进 ``removed`` ——
        # 预览会声称"回滚会删掉你的草稿"，而真相只是清单没读到。
        target_untracked = dict(current.untracked) if omitted else dict(untracked or {})
        target = gc.Snapshot(
            tree=tree,
            untracked=target_untracked,
            tree_paths=target_paths,
            non_regular=target_special,
        )
        plan = gc.diff(root, current=current, target=target)

        target_bytes = sum(target_sizes.values()) + (
            0
            if omitted
            else _store_bytes(Path(state_dir) / "untracked", target_untracked.values())
        )

        return PreviewResult(
            restored=plan.restored,
            added=plan.added,
            removed=plan.removed,
            protected_removed=plan.protected_removed,
            unsupported=plan.unsupported,
            untracked_omitted=omitted,
            current_files=current_files,
            current_bytes=current_bytes,
            # 清单读不到时目标规模**不含**未跟踪部分：那是未知，不该算成已知。
            target_files=len(target_paths) + (0 if omitted else len(target_untracked)),
            target_bytes=target_bytes,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 引用计数与 GC
# ---------------------------------------------------------------------------

#: 分片文件名的形状：``<2 位 hex>/<62 位 hex>``，也就是 sha256 的十六进制。
_DIGEST_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{62}$")

_DEFAULT_RETENTION_DAYS = 30
_MAX_RETENTION_DAYS = 3650


def clamp_retention_days(value: object) -> int:
    """保留期（天）：默认 30，范围 0..3650。

    ``0`` 是**合法**值（"不设保留期，未引用即删"），但不接受负数 —— 负数会让
    ``cutoff`` 落到未来，于是所有文件都被判成"还年轻"，GC 静默什么都不做。
    """
    try:
        days = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_RETENTION_DAYS
    if days < 0:
        return _DEFAULT_RETENTION_DAYS
    return min(days, _MAX_RETENTION_DAYS)


@dataclass(frozen=True)
class RefScan:
    """一次 ``refs/chiron/checkpoints/**`` 扫描的结果（仅用于统计与自检）。"""

    refs: int = 0
    sessions: tuple[str, ...] = ()
    commits: int = 0
    #: 所有 commit 的 tree 路径去重后的数量。它是"索引覆盖了多少路径"的量级指标，
    #: 不参与引用计数（见模块头：CAS 是 sha256，git 树是 sha1，两者不可互推）。
    tree_paths: int = 0
    #: ref 指向的 commit 已不存在（被外部 gc 掉 / 强推）。这些点无法回滚，
    #: 单独报出来比静默跳过有用。
    dangling: tuple[str, ...] = ()


def scan_checkpoint_refs(
    roots: Sequence[Path],
    *,
    index_file: Path | None = None,
) -> RefScan:
    """枚举这些仓库里所有检查点 ref，返回扫描统计。

    ``git for-each-ref`` 而不是遍历 ``.git/refs``（理由见
    :func:`git_checkpoint.list_checkpoint_refs`：打包 ref 与 worktree）。
    ``roots`` 里不存在的目录 / 不是仓库的目录**跳过**（不抛）：项目被用户挪走或
    删掉是完全正常的事，GC 不该因此整体失败。

    monorepo 子目录项目同样参与扫描：ref 写在父仓库里（既定设计），
    ``-C <子目录>`` 的 ``for-each-ref`` 照样读得到。``tree_paths`` 只用于统计，
    因此树路径按仓库根口径计数（不剥前缀）不会影响任何删除决策。
    """
    prefix = "refs/chiron/checkpoints/"
    sessions: set[str] = set()
    commits: set[str] = set()
    paths: set[str] = set()
    dangling: list[str] = []
    ref_count = 0
    for raw_root in roots:
        root = Path(raw_root)
        if not root.is_dir():
            continue
        try:
            layout = gc.resolve_layout(root)
        except gc.GitError:
            continue
        proc = gc._run_git(
            root,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            prefix,
            env=gc._git_env(index_file),
        )
        if proc.returncode != 0:
            continue
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            name, _, sha = line.strip().partition(" ")
            if not name.startswith(prefix) or not sha:
                continue
            ref_count += 1
            tail = name[len(prefix):]
            session = tail.split("/", 1)[0]
            if session:
                sessions.add(session)
            exist = gc._run_git(root, "cat-file", "-e", f"{sha}^{{commit}}")
            if exist.returncode != 0:
                dangling.append(name)
                continue
            commits.add(sha)
            # 每个 commit 的 tree 路径集合：用于"索引覆盖了多少路径"这一统计，
            # 以及验证 tree 可读（读不出来说明对象库已经损坏，值得在报告里体现）。
            # 口径按该 root 的项目根（monorepo 下剥前缀），与快照口径一致。
            tree_paths, _special = gc._tree_entries(
                root, f"{sha}^{{tree}}", prefix=layout.prefix
            )
            paths.update(tree_paths)
    return RefScan(
        refs=ref_count,
        sessions=tuple(sorted(sessions)),
        commits=len(commits),
        tree_paths=len(paths),
        dangling=tuple(dangling),
    )


@dataclass(frozen=True)
class StoreFile:
    digest: str
    path: Path
    size: int
    mtime: float


def iter_store_files(store_root: Path) -> list[StoreFile]:
    """列出 CAS 里的分片文件；**只认形状合法的摘要路径**。

    任何不符合 ``<2 hex>/<62 hex>`` 的文件都被忽略：``.tmp-*`` 残留、用户手动放进
    去的备份、杀毒软件写的隔离文件 —— 它们都不该被 GC 当成"垃圾"删掉，也不该
    被算成引用。忽略比猜测安全。
    """
    root = Path(store_root)
    out: list[StoreFile] = []
    if not root.is_dir():
        return out
    try:
        shards = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return out
    for shard in shards:
        name = shard.name
        if len(name) != 2:
            continue
        try:
            files = [f for f in shard.iterdir() if f.is_file()]
        except OSError:
            continue
        for f in files:
            rel = f"{name}/{f.name}"
            if not _DIGEST_RE.match(rel):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            out.append(StoreFile(digest=name + f.name, path=f, size=st.st_size, mtime=st.st_mtime))
    out.sort(key=lambda item: item.digest)
    return out


@dataclass(frozen=True)
class GcReport:
    """GC 的结果。``deleted`` / ``freed_bytes`` 在 dry-run 下是**计划值**。"""

    scanned: int = 0
    referenced: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    dry_run: bool = True
    #: 未被引用、且已过保留期的文件（真正会被删的那批）。
    expired: int = 0
    #: 未被引用、且**从未**出现在任何检查点清单里的文件（无主碎片）。
    #: ``expired`` 的超集口径不同：orphan 指"库里连清单行都没有"。
    orphan: int = 0
    #: 未被引用但还年轻（保留期内）—— 保留，可能正在被一次未完成的上报使用。
    kept_young: int = 0
    store_files: int = 0
    store_bytes: int = 0
    refs: int = 0
    commits: int = 0
    tree_paths: int = 0
    dangling_refs: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    #: 计划删除的摘要（截断到 200 条，够排查，不会把响应撑爆）。
    planned: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "referenced": self.referenced,
            "deleted": self.deleted,
            "freed_bytes": self.freed_bytes,
            "dry_run": self.dry_run,
            "expired": self.expired,
            "orphan": self.orphan,
            "kept_young": self.kept_young,
            "store_files": self.store_files,
            "store_bytes": self.store_bytes,
            "refs": self.refs,
            "commits": self.commits,
            "tree_paths": self.tree_paths,
            "dangling_refs": list(self.dangling_refs),
            "failed": [{"path": p, "error": e} for p, e in self.failed],
            "planned": list(self.planned),
        }


@dataclass
class _Candidates:
    expired: list[StoreFile] = field(default_factory=list)
    kept_young: int = 0


def gc_plan(
    store_root: Path,
    *,
    referenced: Iterable[str],
    manifest_digests: Iterable[str] | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    now: float | None = None,
) -> _Candidates:
    """挑出可删的分片文件。

    ``referenced`` 是**活引用**（``checkpoint_untracked`` 里、且父检查点行还在的
    sha256）。``manifest_digests`` 是**所有**清单摘要（含父行已消失的历史）：
    用来把"过期快照留下的 blob"（``expired``）与"库里连清单都没有的无主碎片"
    （``orphan``）区分开 —— 两者都删，但报告里分开，排查时能看出是哪一种垃圾。

    两者都过同一条保留期闸门：年轻的分片一律保留。理由：一个刚写进 CAS、
    但清单上报失败（网络抖动）的 blob 在库里看起来"无人引用"，而它恰恰是那次
    回滚唯一的数据来源；等到保留期过了再删，代价只是多留几天。
    """
    live = {d for d in referenced if d}
    all_manifest = {d for d in (manifest_digests or ()) if d}
    cutoff = (now if now is not None else time.time()) - (
        clamp_retention_days(retention_days) * 86400
    )
    out = _Candidates()
    for item in iter_store_files(store_root):
        if item.digest in live:
            continue
        if item.mtime >= cutoff:
            out.kept_young += 1
            continue
        out.expired.append(item)
    _ = all_manifest  # 只用于调用方统计；保留参数便于将来做分栏报告
    return out


def gc_execute(
    store_root: Path,
    *,
    roots: Sequence[Path],
    referenced: Iterable[str],
    manifest_digests: Iterable[str] | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
    dry_run: bool = True,
    now: float | None = None,
) -> GcReport:
    """执行（或预演）一次回收，返回报告。

    ``referenced`` 由调用方在**调用本函数之前**查好；本函数不再读数据库，
    因此不会出现"边删边查"的竞态。

    ``dry_run=True`` 时只统计，不动文件（默认值，前端确认后才传 ``False``）。
    """
    store_root = Path(store_root)
    live = {d for d in referenced if d}
    all_manifest = {d for d in (manifest_digests or ()) if d}
    scan = scan_checkpoint_refs(roots)
    files = iter_store_files(store_root)
    store_bytes = sum(f.size for f in files)
    plan = gc_plan(
        store_root,
        referenced=live,
        manifest_digests=all_manifest,
        retention_days=retention_days,
        now=now,
    )

    orphan_count = sum(1 for f in plan.expired if f.digest not in all_manifest)
    expired = plan.expired
    deleted = 0
    freed = 0
    failed: list[tuple[str, str]] = []

    if not dry_run:
        for item in expired:
            try:
                item.path.unlink()
            except FileNotFoundError:
                # 已经被另一次 GC 删掉：幂等，不计入 deleted。
                continue
            except OSError as exc:
                failed.append((item.digest, str(exc)))
                continue
            deleted += 1
            freed += item.size
        _prune_shard_dirs(store_root)
    else:
        freed = sum(f.size for f in expired)

    return GcReport(
        scanned=scan.refs,
        referenced=len(live),
        deleted=deleted if not dry_run else len(expired),
        freed_bytes=freed,
        dry_run=dry_run,
        expired=len(expired),
        orphan=orphan_count,
        kept_young=plan.kept_young,
        store_files=len(files),
        store_bytes=store_bytes,
        refs=scan.refs,
        commits=scan.commits,
        tree_paths=scan.tree_paths,
        dangling_refs=scan.dangling,
        failed=tuple(failed),
        planned=tuple(f.digest for f in expired[:200]),
    )


def _prune_shard_dirs(store_root: Path) -> None:
    """删掉清空后的两字符分片目录（不删存储根）。失败静默。"""
    root = Path(store_root)
    try:
        shards = [d for d in root.iterdir() if d.is_dir() and len(d.name) == 2]
    except OSError:
        return
    for shard in shards:
        try:
            shard.rmdir()  # 仅空目录成功
        except OSError:
            continue
