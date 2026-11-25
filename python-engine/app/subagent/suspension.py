"""G20：子 Agent 的**挂起等人工**现场（``agent_runs`` 行、等待、超时收尾）。

**修前会怎样**

子 Agent 跑在合成会话 ``sub:<父>:<run>`` 里、**没有 ``agent_runs`` 行**（见
``app/agent/interactions.py`` 里那段说明）。而 ``AgentRuntime._register_interaction`` 的
前提是"这一轮有一份能被续跑的 run 现场"—— 它只看 ``task.run_token``。子 Agent 的
``run_token`` 为空 ⇒ 票**根本不登记** ⇒ 审批/提问退回"无人值守 ⇒ 即时拒绝"：
用户看到的是子 Agent 莫名失败，而不是"它在等我"。

**做法**：委派开始时为子会话写一行 ``agent_runs``（``running`` + 绑定 run fence），
于是 runtime 既能把票登记进 ``pending_interactions``，也能在挂起的回合末把现场写成
``awaiting_input``；答复到达后新的一段用 ``load_awaiting`` → ``claim_awaiting`` 接手，
``consume_grant`` 取走那张票继续执行。

**有界策略**（三方都必须有界，且都要**记录**）

1. **写槽**：挂起时释放（``subagent_runner`` 调 ``release``），续跑前重新取 —— 人在思考时
   不该继续占着工作区的写权，否则同批其它子 Agent 会被一个"没在跑"的 run 堵住。
2. **wall 预算**：挂起期间停表（``subagent_runner`` 的 ``_pause_wall``）。等人不是子 Agent
   的花费，但这条豁免必须显式记录 —— 停了多少秒会进日志与事件流。
3. **等待时长**：``SUBAGENT_SUSPEND_WAIT_SECONDS``（默认 900s）封顶，且被
   :data:`MAX_SUSPEND_WAIT_SECONDS` 硬截断 —— 配置写错（比如 86400 或 0）不该退化成
   "永久等"或"立刻失败"。
4. **超时收尾**：待办转 ``expired``、``agent_runs`` 行标 ``expired``，子 Agent 以一条
   ``error`` 事件收尾成 **failed**（明确失败，不是静默卡死）。

为什么这些 SQL 放在这里而不是 ``app/agent/checkpoint.py``：那一层的 ``mark()`` 只迁
**活跃集合**内的行（``running``/``checkpointed``/``resuming``），而子 Agent 收尾时行可能
正停在 ``awaiting_input``；用 ``mark()`` 会 0 行命中，行就永远悬挂着。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 默认等人工多久（秒）。与 ``AGENT_INTERACTION_WAIT_SECONDS`` 的默认量级对齐。
DEFAULT_SUSPEND_WAIT_SECONDS = 900.0
#: 配置值的**下限**（秒）。0 会让"挂起"退化成"立刻失败"，那正是本项要消灭的形态，
#: 所以只对**配置**取值夹下限；调用方显式传更小的值（测试、演练）按它自己的来。
MIN_SUSPEND_WAIT_SECONDS = 60.0
#: 硬上限（1 小时）：票本身的 TTL 是 24h，但一个子 Agent 挂在那里一小时已经远超人的耐心，
#: 而且它占着父会话的注意力。超过这个数的配置会被截断（不是被拒绝 —— 配置错不该让派发失败）。
MAX_SUSPEND_WAIT_SECONDS = 3600.0
#: 一次委派里最多可以挂起几轮（每轮 = 一次"等人"）。反复要确认的模型也必须收敛。
MAX_SUSPEND_ROUNDS = 3
#: 轮询间隔（秒）：票的答复可能来自**另一个副本**，进程内没有回调可挂，只能轮询。
#: 1s 的量级对人来说无感（人回答要几秒到几分钟），对 PG 也只是每 1s 一次索引命中。
DEFAULT_POLL_SECONDS = 1.0

#: ``agent_runs`` 里表示"子 Agent 挂起超时"的状态。与 ``pending_interactions`` 的
#: ``expired`` 同名：两处看到的都是同一件事（没人回答 ⇒ 这件事作废），用两个词只会让人
#: 去猜它们是不是一回事。
STATUS_EXPIRED = "expired"


def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


def _rowcount(result: Any) -> int:
    """从 asyncpg 的状态串（``UPDATE 1`` / ``INSERT 0 1``）里取行数。"""
    parts = str(result or "").split()
    if len(parts) >= 2 and parts[0] in ("UPDATE", "DELETE"):
        try:
            return int(parts[1])
        except ValueError:
            return 0
    if len(parts) == 3 and parts[0] == "INSERT":
        try:
            return int(parts[2])
        except ValueError:
            return 0
    return 0


# ── 等待上限（有界）───────────────────────────────────────────────────────


def clamp_wait_seconds(value: float) -> float:
    """把**任意**等待时长夹到 ``(0s, MAX_SUSPEND_WAIT_SECONDS]``（非法值回落默认值）。

    下限只要"正数"：调用方显式给的值（演练、测试）该按它自己的来。**配置**值另走
    ``MIN_SUSPEND_WAIT_SECONDS``（见 :func:`suspend_wait_seconds`）—— 配置写成 0 会让
    "挂起"悄悄变成"立刻失败"，那正是本项要消灭的形态。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = DEFAULT_SUSPEND_WAIT_SECONDS
    if number != number or number <= 0:  # NaN 也走默认值
        number = DEFAULT_SUSPEND_WAIT_SECONDS
    return min(float(number), MAX_SUSPEND_WAIT_SECONDS)


def suspend_wait_seconds() -> float:
    """本次等的上限：环境变量 > settings > 默认值，夹到一个**有界且不退化为 0** 的区间。"""
    raw = os.getenv("SUBAGENT_SUSPEND_WAIT_SECONDS", "")
    if not raw:
        try:
            from app.config import settings

            raw = str(getattr(settings, "subagent_suspend_wait_seconds", "") or "")
        except Exception:  # noqa: BLE001 - 配置读不出来就用默认值
            raw = ""
    return max(MIN_SUSPEND_WAIT_SECONDS, clamp_wait_seconds(raw or DEFAULT_SUSPEND_WAIT_SECONDS))


def new_run_token() -> str:
    """一段（segment）的 run_token。**每一段都要新凭据**：
    续跑要靠它把挂起的现场从旧段手里接过来（``claim_awaiting`` 的 CAS 条件就是 token）。"""
    return uuid.uuid4().hex


# ── 现场：桌面上的那一行 ──────────────────────────────────────────────────


async def ensure_run_row(
    *,
    tenant_id: int,
    session_id: str,
    run_token: str,
    turn_id: str = "",
    generation: int = 1,
) -> bool:
    """为子会话准备一行 ``agent_runs``，**并绑定 run fence**。失败只告警（不阻断委派）。

    两件事必须一起做：

    * 行存在 ⇒ ``checkpoint.save`` / ``mark`` 有目标（子 Agent 挂起前的现场才能落盘）；
    * fence 绑定 ⇒ ``checkpoint._generation_for`` 能从 ``run_registry`` 解析出 generation
      （不绑定 ⇒ 所有带 token 的写入都因 generation=0 而 0 行命中，等于白建行）。

    **UPSERT 的谓词刻意排除 ``awaiting_input``**：续跑那一段要先让
    ``load_awaiting`` → ``claim_awaiting`` 把行从 ``awaiting_input`` 迁到 ``resuming``
    并换上**新的** token/generation。这里若抢先把它改成 ``running``，接手的 CAS 就 0 行
    命中 —— 表现是"答复到了却接不上现场"，比不挂起更难查。
    """
    if not session_id or not run_token:
        return False
    try:
        from app import run_registry
        from app.tools.context import to_tenant_int

        # 先绑 fence：即使下面的 SQL 失败，runtime 的 fence 查询也是自洽的
        run_registry.bind_run_fence(run_token, max(1, int(generation)))

        pool = _pool()
        if pool is None:
            return False
        tenant = to_tenant_int(tenant_id)
        updated = await pool.execute(
            """
            UPDATE agent_runs
               SET run_token = $2,
                   status = 'running',
                   turn_id = COALESCE(NULLIF($3, ''), turn_id),
                   checkpoint = jsonb_set(
                       COALESCE(checkpoint, '{}'::jsonb), '{generation}',
                       to_jsonb($4::int), true
                   ),
                   updated_at = now()
             WHERE id = (
                       SELECT id FROM agent_runs
                        WHERE session_id = $1 AND ($5 = 0 OR tenant_id = $5)
                        ORDER BY updated_at DESC
                        LIMIT 1
                   )
               AND status <> 'awaiting_input'
            """,
            session_id,
            run_token,
            turn_id or "",
            max(1, int(generation)),
            tenant,
        )
        if _rowcount(updated) > 0:
            return True
        exists = await pool.fetchrow(
            """
            SELECT status FROM agent_runs
             WHERE session_id = $1 AND ($2 = 0 OR tenant_id = $2)
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            session_id,
            tenant,
        )
        if exists is not None:
            # 行在 `awaiting_input`：留给续跑那一段的 `claim_awaiting` 接手
            return True
        await pool.execute(
            """
            INSERT INTO agent_runs
                (id, tenant_id, session_id, turn_id, run_token, status, checkpoint,
                 checkpoint_at, instance_id, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, 'running',
                    jsonb_build_object('generation', $6::int), now(), NULL, now(), now())
            """,
            uuid.uuid4().hex,
            tenant,
            session_id,
            turn_id or None,
            run_token,
            max(1, int(generation)),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 建行失败只是"不能挂起"，不该让委派失败
        logger.warning("subagent run row unavailable (session=%s): %s", session_id, exc)
        return False


async def finish_run_row(*, tenant_id: int, session_id: str, status: str) -> bool:
    """把子会话的 ``agent_runs`` 行收口成终态（**不能**留悬挂的 ``running``）。

    直接用 SQL 而不是 ``checkpoint.mark``：后者只迁活跃集合内的行，而挂起过的行正停在
    ``awaiting_input`` —— 用它等于让"超时/取消/完成"这三种收尾全部 0 行命中，
    行就永远挂在库里（巡检会把它当僵尸，前端会一直转圈）。

    只在"还没到终态"的行上生效（``status NOT IN``）；重复调用因此是幂等的。
    """
    if not session_id or not status:
        return False
    try:
        from app.tools.context import to_tenant_int

        pool = _pool()
        if pool is None:
            return False
        result = await pool.execute(
            """
            UPDATE agent_runs
               SET status = $3, updated_at = now()
             WHERE session_id = $1
               AND ($2 = 0 OR tenant_id = $2)
               AND status NOT IN ('completed', 'failed', 'cancelled', 'expired', 'abandoned')
            """,
            session_id,
            to_tenant_int(tenant_id),
            status,
        )
        return _rowcount(result) > 0
    except Exception as exc:  # noqa: BLE001 - 收口失败只告警（上层已经有明确的终态结论）
        logger.warning("subagent run row not finalised (session=%s): %s", session_id, exc)
        return False


# ── 等待与超时 ────────────────────────────────────────────────────────────


def awaiting_info(runtime: Any) -> dict[str, Any] | None:
    """这一轮是否停在"等人"上（``runtime._awaiting_input`` 的只读视图）。

    为什么读私有属性：这是 runtime 在挂起时唯一留下的**进程内**信号
    （``_suspend_for_input`` 写、回合末用它决定 ``awaiting_input``）。它没有对外的 getter，
    而本次任务不动 ``runtime.py``。读一份拷贝而不是原对象：调用方不该改到 runtime 的状态。
    """
    info = getattr(runtime, "_awaiting_input", None)
    if not isinstance(info, dict):
        return None
    return dict(info)


async def wait_for_answer(
    *,
    tenant_id: int,
    user_id: str,
    session_id: str,
    wait_seconds: float | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> bool:
    """等这张票被回答；**有答复返回 True，等到上限返回 False**（绝不无限等）。

    为什么轮询而不是等事件：答复可能来自**另一个副本**（用户在任意实例上点确认），
    进程内没有回调可挂。票的状态才是唯一权威（同一张表、同一组谓词），轮询它最简单也最准。
    """
    from app.agent import interactions

    limit = clamp_wait_seconds(wait_seconds if wait_seconds is not None else suspend_wait_seconds())
    interval = max(0.01, min(float(poll_seconds or DEFAULT_POLL_SECONDS), 5.0))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while True:
        try:
            rows = await interactions.list_open(
                tenant_id=tenant_id, user_id=user_id or "", session_id=session_id
            )
        except Exception as exc:  # noqa: BLE001 - 读不到就继续等（不要因为一次抖动就判失败）
            logger.warning("subagent suspension poll failed (session=%s): %s", session_id, exc)
            rows = []
        if any(str(row.get("status") or "") == "answered" for row in rows):
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(interval, remaining))


async def expire_session(*, tenant_id: int, user_id: str, session_id: str) -> int:
    """把该子会话仍未完成的待办转成 ``expired``（超时收尾的**可见**痕迹）。"""
    from app.agent import interactions

    try:
        return await interactions.expire_session(
            tenant_id=tenant_id, user_id=user_id or "", session_id=session_id
        )
    except Exception as exc:  # noqa: BLE001 - 收尾失败不得改变"明确失败"这个结论
        logger.warning("subagent suspension expiry failed (session=%s): %s", session_id, exc)
        return 0
