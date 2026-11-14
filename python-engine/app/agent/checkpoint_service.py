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

见 design §4.2：用 HEAD 做父会让检查点链与用户分支历史纠缠，用户 rebase 之后
祖先关系就不代表会话的时间顺序了。当前 HEAD 另存 ``head_sha`` 供审计。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.agent import git_checkpoint as gc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckpointRecord:
    """一次成功创建的检查点。字段与 ``project_checkpoints`` 一一对应。"""

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
    #: 未跟踪文件的 ``(相对路径, sha256)`` 清单，按路径排序。
    #:
    #: 这个清单必须跟记录一起带走：网关的 `checkpoint_untracked` 表靠它落库，
    #: 而回滚未跟踪文件时要从 CAS 按 sha256 取回内容。只带 `untracked_count`
    #: （一个数字）看起来够用，实际会让"快照里有这些文件、回滚时一个也找不回"。
    untracked: tuple[tuple[str, str], ...] = ()


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
    """
    if not session_id:
        logger.warning("checkpoint skipped: empty session_id")
        return None

    index_file = state_dir / f"snap-{session_id}.idx"
    store_root = state_dir / "untracked"

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        head_sha = _head_sha(root, index_file)
        snap = gc.snapshot(
            root,
            index_file=index_file,
            store_root=store_root,
            settings=settings,
        )
        commit = gc.commit_tree(
            root,
            tree=snap.tree,
            parent=parent_sha,
            message=f"chiron checkpoint {session_id} #{seq} ({kind})",
            head_sha=head_sha,
            index_file=index_file,
        )
        ref = gc.checkpoint_ref(session_id, seq, kind)
        gc.write_ref(root, ref, commit, index_file=index_file)
    except Exception as exc:  # noqa: BLE001 — fail-open 是本模块的契约
        # 宽泛捕获是刻意的：这里的失败原因几乎无穷（OSError、GitError、权限、
        # 编码），而**每一种**的处理方式都相同 —— 记录后放行，让对话继续。
        logger.warning("checkpoint failed session=%s seq=%s kind=%s: %s", session_id, seq, kind, exc)
        return None

    return CheckpointRecord(
        seq=seq,
        kind=kind,
        commit_sha=commit,
        tree_sha=snap.tree,
        parent_sha=parent_sha,
        head_sha=head_sha,
        ref_name=ref,
        file_count=len(snap.tree_paths),
        # 两类"没被收进快照"的路径分开报：秘密是**主动排除**（用户不该见到它进快照
        # 的提示），超大文件是**容量所限**（用户需要知道自己的文件没被保护）。
        # 注意这两个字段是**计数**（int），不是路径集合。
        skipped_count=snap.skipped_secrets + snap.skipped_oversized,
        untracked_count=len(snap.untracked),
        # 按路径排序后带走：网关落表不关心顺序，但固定顺序让重复上报的清单
        # 在测试与日志里可比对，也让"同一快照两次上报"的 diff 干净。
        untracked=tuple(sorted(snap.untracked.items())),
    )
