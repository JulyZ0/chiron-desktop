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


def turn_checkpoint(kind: str, *, state_dir: Path) -> int | None:
    """为一个回合节点建检查点，返回本次 seq；不适用或失败返回 None。

    **从不抛异常**：调用点在 `run()` 的主路径上，任何异常都会中断用户对话。
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
        rec = cs.create_checkpoint(
            root,
            session_id=session_id,
            seq=seq,
            kind=kind,
            state_dir=state_dir,
        )
        return rec.seq if rec is not None else None
    except Exception as exc:  # noqa: BLE001 — 见 docstring
        logger.warning("turn checkpoint hook failed kind=%s: %s", kind, exc)
        return None


def _session_id() -> str:
    """当前会话 id；取不到返回空串（钩子据此放行）。"""
    from app.tools.context import get_session_id

    return get_session_id() or ""


def checkpoint_state_dir() -> Path:
    """检查点状态目录：必须**在工作区之外**（索引文件落在仓库里会污染快照）。"""
    import os
    import tempfile

    base = os.environ.get("CHIRON_CHECKPOINT_STATE_DIR") or str(
        Path(tempfile.gettempdir()) / "chiron-checkpoints"
    )
    return Path(base)
