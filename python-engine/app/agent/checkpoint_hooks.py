"""回合级检查点钩子：把 `checkpoint_service` 接到对话回合的起止。

**为什么要经过这一层，而不是在 `runtime.run()` 里直接调**

`run()` 已经很长，且它的每条退出路径（正常结束、异常、用户中断、审批挂起）都不同。
钩子只有两个动作 —— 回合前、回合后 —— 且必须 fail-open，把它单独放一层，
`run()` 里就只剩两行调用，评审时不必再读一遍 `run()` 才能确认"检查点失败会不会
影响对话"。

**seq 的权威来源**

落库时由 DB 事务给出 `max(seq)+1`。但引擎侧**没有 DB 连接**，所以这里退化为
扫描 `refs/chiron/checkpoints/<session>/` 取最大序号再加一。

这是刻意的降级，不是疏忽：两个来源同时存在时，**只有 DB 是权威**（ref 可能被
`git gc` 或外部改写影响）。引擎在会话内顺序执行回合，扫描结果与 DB 一致；
跨进程重启后若 ref 缺失，序号会回退 —— 这正是 `project_checkpoints` 需要
`(session_id, seq)` 唯一约束来兜底的原因。`create_checkpoint` 的 `write_ref`
用 CAS（旧值必须为空），因此**不可能**覆盖已有检查点，最坏只是本次检查点创建失败
（fail-open 已验证）。

**只有绑定了项目根才建检查点**

`root_path` 缺失（画布会话）时直接返回。沙箱目录不属于用户，给它建检查点毫无意义，
还会把 agent 的临时产物写进一个随时会被清理的地方。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.agent import checkpoint_service as cs
from app.tools.sandbox import project_root

logger = logging.getLogger(__name__)

_SEQ_RE = re.compile(r"/(\d+)-")


def _next_seq(root: Path, session_id: str) -> int:
    """本会话下一个序号：已有 ref 的最大序号 + 1；读不到就从 1 开始。"""
    base = root / ".git" / "refs" / "chiron" / "checkpoints" / session_id
    try:
        names = [p.name for p in base.iterdir()]
    except OSError:
        return 1
    seqs = [int(m.group(1)) for n in names if (m := _SEQ_RE.match(f"/{n}"))]
    return max(seqs, default=0) + 1


def turn_checkpoint_record(kind: str, *, state_dir: Path) -> cs.CheckpointRecord | None:
    """为一个回合节点建检查点，返回**完整记录**；不适用或失败返回 None。

    与 `turn_checkpoint()` 的区别只在返回值：那个是给 `run()` 主路径用的薄包装
    （只要 seq），这个供**上报时间线**用 —— 网关要落的字段（commit/tree sha、
    ref 名、文件计数）都在记录里，拿 seq 回去再查一次 ref 是多余的一次 git 往返。

    **从不抛异常**：调用点同样在 `run()` 的主路径上。
    """
    try:
        root = project_root()
        if root is None:
            # 未绑定项目：沙箱内容不是用户的代码，建检查点只会产生噪音。
            return None
        session_id = _session_id()
        if not session_id:
            return None
        seq = _next_seq(root, session_id)
        return cs.create_checkpoint(
            root,
            session_id=session_id,
            seq=seq,
            kind=kind,
            state_dir=state_dir,
        )
    except Exception as exc:  # noqa: BLE001 — 见 docstring
        logger.warning("turn checkpoint hook failed kind=%s: %s", kind, exc)
        return None


def turn_checkpoint(kind: str, *, state_dir: Path) -> int | None:
    """为一个回合节点建检查点，返回本次 seq；不适用或失败返回 None。

    **从不抛异常**：调用点在 `run()` 的主路径上，任何异常都会中断用户对话。
    """
    rec = turn_checkpoint_record(kind, state_dir=state_dir)
    return rec.seq if rec is not None else None


def _session_id() -> str:
    """当前会话 id；取不到返回空串（钩子据此放行）。"""
    from app.tools.context import get_session_id

    return get_session_id() or ""


def checkpoint_state_dir() -> Path:
    """检查点状态目录：必须**在工作区之外**（索引文件落在仓库里会污染快照）。

    默认落在用户的持久数据目录（Windows `%LOCALAPPDATA%`），**不能**用系统临时目录：
    这里存的是快照索引与未跟踪文件的 CAS blob，而临时目录会被系统/清理工具删掉。
    丢掉 CAS 的直接后果是「还原点还在（git ref 在 `.git` 里），但回滚未跟踪文件时
    报找不到 blob」—— 用户看到的正是"还原点不能正确工作"。
    """
    import os
    import tempfile

    env = os.environ.get("CHIRON_CHECKPOINT_STATE_DIR")
    if env:
        return Path(env)

    base: Path | None = None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "chiron" / "checkpoints"
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]) / "chiron" / "checkpoints"
    elif os.name != "nt" and os.environ.get("HOME"):
        base = Path(os.environ["HOME"]) / ".local" / "state" / "chiron" / "checkpoints"
    if base is None:
        # 极端兜底（无 HOME 的服务账户）：临时目录至少让本进程内可回滚。
        base = Path(tempfile.gettempdir()) / "chiron-checkpoints"
    return base


#: 单次上报携带的未跟踪清单上限。网关侧同一上限；超出的部分**不静默丢弃**，
#: 而是截断并告警 —— 清单不全意味着那些文件回滚不回来，用户需要知道。
INGEST_UNTRACKED_MAX = 5000


async def report_checkpoint(
    rec: cs.CheckpointRecord,
    *,
    session_id: str,
    turn_id: str = "",
    base_url: str | None = None,
    token: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """把检查点送到网关落表；失败只记日志，**从不抛异常**。

    这一步是整条回滚链路的接缝：快照早在 `create_checkpoint` 里做完了（git ref +
    未跟踪文件 CAS），但不送过来，`project_checkpoints` 就永远是空的，前端的
    时间线也就没有任何可回滚的点。

    归属**不在这里决定**：网关按 `session_id` 反查项目，本函数只如实转述"哪个会话、
    哪个回合、什么 seq"。这样引擎不需要、也不应该知道 workspace/tenant/user。

    超时取 5s 且失败即放行：调用点在回合收尾路径上，一个慢网关不该拖住用户的下一条。
    """
    if not session_id or not rec.commit_sha:
        return False

    addr = (base_url or "").strip()
    tok = token
    if not addr or tok is None:
        try:
            from app.config import settings

            addr = addr or (settings.gateway_internal_url or "").strip()
            if tok is None:
                tok = settings.internal_token or ""
        except Exception as exc:  # noqa: BLE001 — 配置不可读就不上报
            logger.warning("checkpoint report: settings unavailable: %s", exc)
            return False
    if not addr:
        logger.debug("checkpoint report skipped: no gateway address configured")
        return False

    untracked = list(rec.untracked)
    if len(untracked) > INGEST_UNTRACKED_MAX:
        logger.warning(
            "checkpoint report: untracked manifest truncated %d -> %d (session=%s seq=%s)",
            len(untracked), INGEST_UNTRACKED_MAX, session_id, rec.seq,
        )
        untracked = untracked[:INGEST_UNTRACKED_MAX]

    payload = {
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": rec.seq,
        "kind": rec.kind,
        "commit_sha": rec.commit_sha,
        "tree_sha": rec.tree_sha,
        "parent_sha": rec.parent_sha or "",
        "head_sha": rec.head_sha or "",
        "ref_name": rec.ref_name,
        "file_count": rec.file_count,
        "skipped_count": rec.skipped_count,
        # 未跟踪文件按字节算：网关的 byte_size 是"这次快照覆盖了多少内容"的粗估。
        "byte_size": 0,
        "untracked": [
            {"path": path, "sha256": digest, "size": 0} for path, digest in untracked
        ],
    }

    try:
        import httpx

        headers = {"X-Internal-Token": tok} if tok else {}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                addr.rstrip("/") + "/v1/internal/checkpoints",
                json=payload,
                headers=headers,
            )
        if resp.status_code >= 400:
            logger.warning(
                "checkpoint report rejected session=%s seq=%s: HTTP %s %s",
                session_id, rec.seq, resp.status_code, resp.text[:200],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — 见 docstring
        logger.warning(
            "checkpoint report failed session=%s seq=%s: %s", session_id, rec.seq, exc
        )
        return False
