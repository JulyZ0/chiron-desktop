"""rerun_subagent 工具 —— 重跑一个**已结束**的子 Agent run（中断/失败后的补救）。

本模块只保留**工具形态**（注册表 + 模型可见的说明 + 错误转成 ``error`` 字段）；
判定与派发的实现在 :mod:`app.subagent.rerun` —— 网关的内网端点
（``POST /v1/internal/subagent/runs/{run_id}/rerun``，前端面板「重试」按钮）走同一份代码。
两份实现必然漂移，而漂移方向通常是**放宽**（访问谓词少一条、写权限跟着变宽）。

行为契约（与内网端点一致）：

* 只允许**终态** run（``completed/failed/cancelled/lost/partial``）—— running 的已经有
  owner，重跑会得到两份并发作业；
* 任务描述取**库里那一列**（``subagent_runs.task``）：让模型重新拼一遍几乎必然与原来不
  一致，那就不叫重跑了；``task_override`` 是给用户显式改写留的口子；
* 原 run 的写权限**照旧**（``read_only=true`` 重跑后仍只读），``rerun_of`` 记录血缘，
  ``parent_run_id`` 让新 run 在任务树里挂在原 run 下面。
"""

from __future__ import annotations

import logging
from typing import Any

from app.subagent.rerun import (
    RerunRefusedError,
    rerun_run,
)
from app.tools.registry import registry

logger = logging.getLogger(__name__)


async def rerun_subagent(run_id: str, task_override: str = "") -> dict[str, Any]:
    """按原任务与 Profile 重新派发一个已结束的 run。

    Args:
        run_id: 要重跑的 run（来自 ``subagent`` 的返回值或 ``list_subagent_runs``）。
        task_override: 可选——用新的任务描述重跑（其余保持原样）。
    """
    try:
        payload = await rerun_run(run_id=run_id, task_override=task_override)
    except RerunRefusedError as exc:
        # 工具层只能返回结构化错误（没有 HTTP 状态码可言），但**绝不**返回假 run_id
        return {"error": exc.error, "code": exc.code}
    except Exception as exc:  # noqa: BLE001 - 派发失败要如实返回
        logger.warning("rerun_subagent 失败: %s", str(exc)[:200])
        return {"error": f"rerun failed: {str(exc)[:160]}"}
    # 「请继续做别的事」那类提示是派发用的，重跑不带给模型（它已经知道这是后台）
    payload.pop("note", None)
    return payload


registry.register(
    name="rerun_subagent",
    description=(
        "Re-run a **finished** subagent run by its run_id, reusing the task and profile it was "
        "originally dispatched with (optionally with a new task_override). Use it after a run "
        "ended as failed / cancelled / lost / partial — e.g. the engine instance restarted, the "
        "user stopped it, or the upstream provider errored. Running runs cannot be rerun (they "
        "already have an owner); the new run records rerun_of=<run_id> and parent_run_id=<run_id> "
        "for lineage."
    ),
    parameters={
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The finished run to rerun (from subagent or list_subagent_runs)",
            },
            "task_override": {
                "type": "string",
                "default": "",
                "description": "Optional replacement task; empty = reuse the stored task verbatim",
            },
        },
        "required": ["run_id"],
    },
    handler=rerun_subagent,
)
