"""按**原任务**重跑一个已结束的 subagent run —— 工具与内网端点共用一份实现。

为什么需要（P0-2）：后台子 Agent 与父回合解耦之后，「实例重启判 lost / 用户停止判
cancelled / provider 报错判 failed / 撞预算墙判 partial」都是常态。这些 run 的任务描述
与 Profile 都还在 DB 里（``subagent_runs.task`` / ``profile_name`` / ``read_only``），
重跑只是"按原样再派一次"——**不该**让模型或前端重新拼一遍任务文本（那几乎必然与原来
不一致，也就不是"重跑"了）。

两条入口，一份实现：

* 模型工具 ``rerun_subagent``（``app/tools/subagent_rerun.py``）；
* 网关内网端点 ``POST /v1/internal/subagent/runs/{run_id}/rerun``
  （``app/api/internal_subagent.py``，前端面板「重试」按钮走它）。

实现写两遍必然漂移，而漂移方向通常是**放宽**（访问谓词少一条、写权限跟着变宽）。

三条硬约束：

1. **只认终态**：running 的 run 已经有 owner，重跑会得到两份并发作业；
2. **不伪造上下文**：任务描述取自库里那一列，取不到就拒绝（409）—— 由前端把任务
   放回输入框、交用户确认后重新派发；
3. **重跑不是提权通道**：原 run 只读 ⇒ 重跑仍只读；新 run 以 ``parent_run_id`` 指向
   原 run、``rerun_of`` 记血缘，任务树里一眼能看出"这次是从哪一次重跑来的"。
"""

from __future__ import annotations

import logging
from typing import Any

from app.subagent.access import RUN_ACCESS_CLAUSE

#: ⚠ 身份读取**必须**是函数内延迟导入：`app.tools.__init__` 会 import 工具模块
#: （含 ``subagent_rerun``），而那个模块反过来 import 本模块 —— 顶层 import
#: ``app.tools.context`` 会触发"部分初始化的模块里拿不到 RERUNNABLE"这类循环导入。
#: `app.subagent.access` 不依赖 ``app.tools``，可以放在顶层。

logger = logging.getLogger(__name__)

#: 允许重跑的终态。与 ``app/subagent/store.py`` 的终态白名单一致（外加 `partial`）：
#: `partial`（有产物但不完整，典型是撞上 wall/token 预算）恰恰是用户最想重跑的那一类；
#: 把它排除在外，面板上的「重试」对这类 run 永远 409。
#: running 系列**不在**列 —— 它已经有 owner，重跑就是两份并发作业。
RERUNNABLE = ("completed", "failed", "cancelled", "lost", "partial")

#: 读原 run。访问谓词与 `read_subagent_result` / `resume_subagent` / 工具版重跑
#: **共用一份**（`app/subagent/access.py`）：同一件事写三遍必然漂移。
#: 参数顺序是谓词的一部分契约：`run_id, tenant_id, user_id, root_session_id`。
RERUN_RUN_SQL = f"""
SELECT id, task, profile_name, read_only, status, depth
  FROM subagent_runs
 WHERE id = $1{RUN_ACCESS_CLAUSE}
"""


class RerunRefused(Exception):
    """重跑被拒。

    ``status`` 是 HTTP 语义（工具层忽略它、只取 ``error``/``code``）：
    404 = 不存在或不属于本次会话/租户/用户；409 = 状态或任务描述不满足重跑条件；
    5xx = 落库/派发能力不可用（**不是**"重跑成功"）。
    """

    def __init__(self, status: int, error: str, code: str = "") -> None:
        super().__init__(error)
        self.status = int(status)
        self.error = str(error)
        self.code = str(code or "")


def _session_id() -> str:
    from app.tools.context import get_session_id

    return get_session_id()


def _tenant_id() -> int:
    """租户**必须是整数**：`tenant_id` 列是 smallint，传字符串会让 asyncpg 直接报错，
    那时重跑会以"store unavailable"失败 —— 看起来像迁移问题，实际是参数类型。
    """
    from app.tools.context import get_tenant_id_int

    return get_tenant_id_int()


def _user_id() -> str:
    from app.tools.context import get_user_id

    return get_user_id()


def _pool() -> Any:
    """取连接池；不可用时返回 None（调用方据此回 503，而不是假装成功）。"""
    try:
        from app.db import get_pool

        return get_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent rerun: db unavailable (%s)", str(exc)[:160])
        return None


async def load_rerun_target(
    *, run_id: str, pool: Any = None
) -> dict[str, Any]:
    """读出可重跑的原 run（含访问越权/状态/任务描述的判定）。

    身份（tenant / user / session）取自**当前工具上下文** —— 调用方（HTTP 端点或模型
    工具）负责在调用前把它设好，避免这里再接受一组"调用方声明的身份"。
    """
    rid = (run_id or "").strip()
    if not rid:
        raise RerunRefused(400, "run_id is required", "invalid")
    session_id = _session_id()
    if not session_id:
        raise RerunRefused(
            409,
            "no active session context: a rerun must be dispatched into a session",
            "no_session",
        )
    tenant_id = _tenant_id()
    user_id = _user_id()
    pool = pool if pool is not None else _pool()
    if pool is None:
        raise RerunRefused(503, "database unavailable", "store_unavailable")

    try:
        row = await pool.fetchrow(RERUN_RUN_SQL, rid, tenant_id, user_id, session_id)
    except Exception as exc:  # noqa: BLE001 - 表缺失 / 老库缺列
        logger.warning("rerun 查询失败（run=%s）: %s", rid, str(exc)[:200])
        raise RerunRefused(
            503, "subagent run store unavailable (check migrations)", "store_unavailable"
        ) from exc
    if not row:
        raise RerunRefused(404, f"subagent run not found: {rid}", "not_found")

    status = str(row["status"] or "")
    if status not in RERUNNABLE:
        raise RerunRefused(
            409,
            f"run {rid} is {status or 'unknown'}; only finished runs can be rerun "
            f"({', '.join(RERUNNABLE)}) — a running one already has an owner",
            "run_not_terminal",
        )
    return dict(row)


async def rerun_run(*, run_id: str, task_override: str = "") -> dict[str, Any]:
    """按原任务（或显式覆盖）重新派发一个已结束的 run，返回新 run 的载荷。

    派发走**同一个** ``subagent`` 工具路径（同一套工具面 / 预算 / 栅栏 / 落库），
    因此这里不复制任何派发细节。成功时返回 ``run_id``（新）/ ``rerun_of`` 等字段。

    失败一律抛 :class:`RerunRefused`：调用方必须能区分"没派出去"与"派了但很慢"。
    """
    row = await load_rerun_target(run_id=run_id)

    rid = str(row.get("id") or "").strip()
    task = (task_override or "").strip() or str(row.get("task") or "").strip()
    if not task:
        # 绝不"猜一个任务"：取不到就拒绝，由前端把任务放回输入框让用户确认。
        raise RerunRefused(
            409,
            f"run {rid} has no stored task to rerun; resend the task from the composer",
            "task_missing",
        )

    # 局部导入：工具模块之间不互相 import，避免注册期的循环依赖；`app.tools.context`
    # 同理由（见文件头的循环导入说明）。
    from app.tools.context import get_all, restore_context, set_tool_context
    from app.tools.subagent import MAX_DEPTH, subagent as delegate

    # 深度：新 run 是原 run 的子节点（parent_run_id 指向它），因此取原深度 + 1。
    # 夹到 MAX_DEPTH-1 是**必要的**：否则重跑一条已经很深的 run 会被
    # `subagent()` 的递归闸门直接拒掉 —— 而"重跑最深那一条"恰恰是常见需求。
    original_depth = max(0, int(row.get("depth") or 1))
    depth = min(original_depth, max(0, MAX_DEPTH - 1))

    snapshot = get_all()
    try:
        # 只有这一处需要改上下文：子 Agent 的层级来自 `subagent_depth`，父来自显式参数。
        set_tool_context(subagent_depth=depth)
        result = await delegate(
            task=task,
            profile=str(row.get("profile_name") or ""),
            run_in_background=True,
            # 写权限照旧：read_only 的 run 重跑后仍只读（重跑不是提权通道）
            allow_write=not bool(row.get("read_only")),
            rerun_of=rid,
            parent_run_id=rid,
        )
    except RerunRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - 派发失败要如实返回
        logger.warning("subagent rerun 派发失败（run=%s）: %s", rid, str(exc)[:200])
        raise RerunRefused(503, f"rerun failed: {str(exc)[:160]}", "dispatch_failed") from exc
    finally:
        # 上下文是**共享**的（同一 task 里后续调用都会看见），必须精确还原，
        # 否则"重跑深度"会污染父 Agent 之后派的每一个子任务。
        restore_context(snapshot)

    if not isinstance(result, dict):
        raise RerunRefused(503, "unexpected dispatch result", "dispatch_failed")
    if result.get("error"):
        # 配额拒绝 / 深度闸门等：这次**没有**派出作业，如实报错（不返回假 run_id）
        raise RerunRefused(503, str(result["error"]), "dispatch_refused")

    new_run_id = str(result.get("run_id") or "").strip()
    if not new_run_id:
        raise RerunRefused(503, "dispatch returned no run_id", "dispatch_failed")

    logger.info("subagent rerun launched: from=%s to=%s", rid, new_run_id)
    payload = {
        "status": "async_launched",
        "run_id": new_run_id,
        "result_ref": new_run_id,
        "rerun_of": rid,
        # 新 run 在任务树里的位置：原 run 的子节点
        "parent_run_id": rid,
        "depth": depth + 1,
    }
    if result.get("profile"):
        payload["profile"] = result["profile"]
    return payload
