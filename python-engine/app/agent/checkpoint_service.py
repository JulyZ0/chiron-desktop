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

import logging
from collections.abc import Mapping
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

    **monorepo 子目录项目现在受支持**：``snapshot()`` 会用
    ``git rev-parse --show-prefix`` 定出项目根在父仓库里的前缀，把独立索引裁到前缀内，
    因此快照只含项目根子树（父仓库其它文件不会被卷进来，见 ``git_checkpoint``
    的 ``RepoLayout``）。仍然走失败分支的只有"根本不在仓库里"与"拿不到前缀"
    这两类 —— 判定都落在 ``state='failed'`` 的记录里，"未启用版本控制的项目"
    因此得到明确的失败记录，而不是一条看起来成功、实际什么都没保护的空快照。
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
