"""Super-agent 接管 —— 调度面（周期循环 + 租约 + 接管段生命周期）。

**与 `watchdog_job.py` 的关系**

看门狗是**判定器**（`assess` 判完只发提示/建议终止）；接管是**执行器**（判定通过就真的起一次
run）。所以这里照搬看门狗的**调度骨架**（Redis 租约互斥、异常不打断循环、配置读不到即关），
但执行段是自己的一套。

**为什么新起一个循环，而不是塞进 `watchdog_loop`**

两者节奏不同：看门狗是"全局每 5 分钟看所有活跃回合"，接管是"每个会话按自己的间隔到点"。
塞在一起会让接管的周期被看门狗的周期绑架（用户设 15 分钟，实际最早 5 分钟才被看见）。
代价是多一个后台任务 —— 由 `enabled` 闸住，关掉时**不创建任务**（真零影响）。

**接管段（segment）是什么**

一次成功巡检 = 一个接管段：从 `begin_segment` 到 `end_segment` 之间，前端锁定操作
（只留"打断接管"）。段标记用**带 TTL 的 Redis 键**而不是内存字典，理由有两个：
多副本时前端可能连到另一个实例；实例崩溃时段会随 TTL 自动消失，不会永久锁住 UI。

**成本控制在这一层的位置**

L0 判定（`app/agent/takeover.py`）是唯一的"要不要花钱"闸门。本模块只负责把事实采集齐、
把判定结果执行掉、把结果记账。任何采集失败都按"不接管"处理 —— 见 :func:`_collect` 的
各分支，以及 :func:`tick` 里逐行独立的异常边界。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.agent import takeover as l0

logger = logging.getLogger(__name__)

#: 租约键：同一时刻只允许一个副本巡检（前缀由 `rkey` 统一加）。
LEASE_NAME = "engine:takeover:lease"
#: 租约时长必须**大于**一次 tick 的耗时上限，否则两个副本会前后脚都跑起来。
#:
#: 与看门狗的 120s 不同：接管的一拍可能真的起 run（虽然 run 在独立 task 里跑，
#: 但采集 + 落库仍在 tick 内），留足余量避免租约中途过期导致重复启动。
LEASE_TTL_SECONDS = 300

#: 接管段标记键前缀。TTL 取"单次 run 的上限 + 余量"：run 结束时主动删除，
#: 崩溃时靠 TTL 自愈（否则 UI 会被永久锁住）。
SEGMENT_KEY_PREFIX = "engine:takeover:segment:"
SEGMENT_TTL_SECONDS = 30 * 60

#: 前端 presence 键前缀（用户维度）。TTL 略大于让步窗口：窗口 10 分钟时，
#: 键活 15 分钟 —— 用户关掉页面后仍有一小段"刚走开"的保护期。
PRESENCE_KEY_PREFIX = "engine:takeover:presence:"
PRESENCE_TTL_SECONDS = 15 * 60

#: 审计文件名（与 `logs/isolation_audit.jsonl` 同款形状）。
AUDIT_FILE = "takeover_audit.jsonl"

#: 巡检指令。措辞三条硬要求：
#:   1. 明说"这是自动巡检、不是新指令" —— 否则模型会把它当成用户的新需求并扩展范围；
#:   2. 明说"没有活就结束" —— 否则它会为了显得有用而编造任务；
#:   3. 明说"需要确认会挂起" —— 让它知道绕过审批是走不通的（`unattended` 会拒绝）。
TAKEOVER_PROMPT = """\
【Super-agent 定时巡检】这是系统的自动巡检，不是用户的新指令。

请按以下顺序处理：
1. 读本会话的任务板与会话记录，判断哪些工作**用户已经要求过、但尚未完成**。
2. 只推进这些工作。不新增范围、不重构、不做未经请求的改动。
3. 若没有可推进的工作，直接回复"无事可做"并结束本轮。不要为了显得有用而编造任务。
4. 需要用户确认的操作会挂起等待，不要试图绕过。

推进时请简要说明：做了什么、还剩什么。
"""


@dataclass
class RunOutcome:
    """一次接管 run 的结果（由 runner 返回，用于记账）。"""

    ok: bool = False
    tokens: int = 0
    error: str = ""
    #: 接管 run 是否真的产生了推进（供 UI 显示）。False + ok=True = "巡检了但无事可做"。
    progressed: bool = False


@dataclass
class TickResult:
    """一拍的结果。返回出来是为了让测试断言"关掉时确实什么都没做"。"""

    considered: int = 0
    started: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    skipped_global: str = ""

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


# ── Redis 键 ─────────────────────────────────────────────────────────


def lease_key() -> str:
    from app.redis_keys import rkey

    return rkey(LEASE_NAME)


def segment_key(session_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(f"{SEGMENT_KEY_PREFIX}{session_id}")


def presence_key(user_id: str) -> str:
    from app.redis_keys import rkey

    return rkey(f"{PRESENCE_KEY_PREFIX}{user_id}")


async def _acquire_lease(redis: Any, *, ttl: int = LEASE_TTL_SECONDS) -> bool:
    """抢本拍的执行权。Redis 出错时返回 False（不巡检优于重复巡检）。"""
    if redis is None:
        return False
    try:
        return bool(await redis.set(lease_key(), uuid.uuid4().hex, nx=True, ex=ttl))
    except Exception as exc:  # noqa: BLE001 - 抢不到/出故障都按"本拍不跑"
        logger.debug("takeover: lease acquire failed (%s)", exc)
        return False


async def _release_lease(redis: Any) -> None:
    """主动释放，把等待期从"租约自然过期"缩短到一个 tick 的间隔。"""
    if redis is None:
        return
    try:
        await redis.delete(lease_key())
    except Exception as exc:  # noqa: BLE001 - 释放失败最多让它多占一个 TTL
        logger.debug("takeover: lease release failed (%s)", exc)


# ── 接管段与 presence ────────────────────────────────────────────────


async def begin_segment(redis: Any, *, session_id: str, takeover_id: str) -> bool:
    """标记"会话 X 正处于接管段"。前端据此显示覆盖层。

    失败时返回 False 但**不阻止 run**：段标记只影响 UI 外观，不该成为功能开关。
    反过来如果它失败却阻止了 run，那么 Redis 抖动一次就白丢一次巡检。
    """
    if redis is None:
        return False
    try:
        await redis.set(
            segment_key(session_id),
            json.dumps({"takeover_id": takeover_id, "started_at": time.time()}),
            ex=SEGMENT_TTL_SECONDS,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("takeover: begin segment failed (session=%s): %s", session_id, exc)
        return False


async def end_segment(redis: Any, *, session_id: str) -> None:
    """结束接管段。必须**无论 run 成败都调用** —— 否则 UI 要被锁到 TTL 过期。"""
    if redis is None:
        return
    try:
        await redis.delete(segment_key(session_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("takeover: end segment failed (session=%s): %s", session_id, exc)


async def segment_snapshot(redis: Any, *, session_id: str) -> dict[str, Any] | None:
    """读接管段（给 API 用：前端要知道"是不是接管中、由谁发起"）。"""
    if redis is None:
        return None
    try:
        raw = await redis.get(segment_key(session_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"takeover_id": "", "started_at": 0.0}
    return data if isinstance(data, dict) else None


async def mark_presence(redis: Any, *, user_id: str, now: float | None = None) -> bool:
    """记一次"用户还在"。**让步信号 2** 的唯一写入点。

    只写不带读：高频调用（前端每 30s 一次），任何读-改-写都会在并发下算错。

    ``now`` 可注入是为了让测试能对着固定时刻断言 —— 生产路径不传，取真实时钟。
    """
    if redis is None or not user_id:
        return False
    try:
        await redis.set(
            presence_key(user_id),
            str(now if now is not None else time.time()),
            ex=PRESENCE_TTL_SECONDS,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("takeover: presence write failed (%s)", exc)
        return False


async def read_presence(redis: Any, *, user_id: str) -> float | None:
    if redis is None or not user_id:
        return None
    try:
        raw = await redis.get(presence_key(user_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ── 事实采集 ─────────────────────────────────────────────────────────


def _epoch(value: Any) -> float | None:
    """把 asyncpg 的 naive datetime 转成 epoch 秒。

    `timestamp without time zone` 读出来是 naive 的；数据库写入时用的是 UTC
    （见各处的 `datetime.now(UTC).replace(tzinfo=None)`），所以这里按 UTC 解释。
    解释错了会让"用户多久没动"整体偏移时区差，判定就全错。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.timestamp()
    return None


def _row_int(row: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return default


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).date().isoformat()


async def _last_message_at(pool: Any, *, session_id: str) -> float | None:
    """会话最后一条消息的时间。**让步信号 3**。

    查 `unified_messages` 而不是 session_meta：后者是**进程内存**的 L1 簿记
    （`app/memory/session_meta.py`，IDLE_TTL 15 分钟），多副本时看不到别的实例的活动。
    """
    if pool is None or not session_id:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT max(created_at) AS last FROM unified_messages WHERE session_id = $1",
            session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("takeover: last message lookup failed (%s): %s", session_id, exc)
        return None
    if row is None:
        return None
    return _epoch(row["last"])


async def _has_pending_work(pool: Any, *, tenant_id: int, session_id: str) -> bool:
    """还有没有活可干：任务板有未完成项，或存在可续跑的快照。

    **两道取或**，因为两种"没做完"形态不同：任务板是模型自己写的计划（可能很粗），
    checkpoint 是被打断的 run（很具体）。只看其一都会漏。

    读不到时返回 **True**（保守：宁可多跑一次，也不因为一次 DB 抖动就把巡检永久关掉）。
    这与其它"读不到即不接管"的地方取向相反，理由是本函数的返回值只影响
    "要不要看内容"，不代表风险 —— 真正的风险由 `has_active_run` 与预算把关。
    """
    from app.agent import taskboard

    try:
        board = await taskboard.load()
        counts = taskboard.summarize(list(board.get("todos") or []))
        if counts.get("pending", 0) > 0 or counts.get("in_progress", 0) > 0:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("takeover: taskboard read failed (%s): %s", session_id, exc)

    try:
        from app.agent import checkpoint

        snap = await checkpoint.load(tenant_id=tenant_id, session_id=session_id)
        if snap:
            # 有快照 = 上次 run 没画句号，值得接着跑。
            # 但 `pending` 为空且 `replay_pending` 为空时其实无事可做（只剩历史尾巴）。
            if snap.get("pending") or snap.get("replay_pending"):
                return True
            return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("takeover: checkpoint read failed (%s): %s", session_id, exc)

    # 两者都没读到 / 都没有内容：没有依据认为"有活"。
    return False


async def _collect(
    *,
    redis: Any,
    pool: Any,
    row: dict[str, Any],
    now: float,
    idle_minutes: int,
    taskboard_fn: Any = None,
    last_message_fn: Any = None,
) -> l0.Signal:
    """把一行配置 + 环境事实折成 :class:`l0.Signal`。

    `taskboard_fn` / `last_message_fn` 可注入：测试不必碰 DB 即可回放"用户刚说过话"
    "任务板还有 3 项"这些样本（与 `watchdog_job.tick` 的 `collect_fn` 同款做法）。
    """
    session_id = str(row.get("session_id") or "")
    tenant_id = _row_int(row, "tenant_id")
    user_id = str(row.get("user_id") or "")

    # 会话锁 = 让步信号 1。Redis 不可用时**当作被占用**（不接管）——
    # 与 `resume._lock_is_held` 同一取向：宁可少接管，不可抢现场。
    has_active_run = True
    if redis is not None:
        try:
            has_active_run = (await redis.get(f"agent:run-lock:{session_id}")) is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug("takeover: lock probe failed (%s): %s", session_id, exc)

    presence_at = await read_presence(redis, user_id=user_id)

    if last_message_fn is None:
        last_message_fn = _last_message_at
    last_message_at = await last_message_fn(pool, session_id=session_id)

    if taskboard_fn is None:
        taskboard_fn = _has_pending_work
    has_pending_work = await taskboard_fn(pool, tenant_id=tenant_id, session_id=session_id)

    # 当日预算：`budget_day` 不是今天就把用量视为 0（按天重置，见迁移文件说明）。
    used = _row_int(row, "tokens_used_today")
    if str(row.get("budget_day") or "") != _today(now):
        used = 0

    return l0.Signal(
        now=now,
        user_enabled=bool(row.get("user_enabled")),
        session_enabled=bool(row.get("enabled")),
        schedule_kind=str(row.get("schedule_kind") or l0.KIND_INTERVAL),
        once_at=_epoch(row.get("once_at")),
        interval_seconds=_row_int(row, "interval_seconds"),
        last_run_at=_epoch(row.get("last_run_at")),
        cooldown_until=_epoch(row.get("cooldown_until")),
        melt_state=str(row.get("melt_state") or ""),
        consecutive_failures=_row_int(row, "consecutive_failures"),
        tokens_used_today=used,
        daily_token_budget=_row_int(row, "daily_token_budget"),
        has_active_run=has_active_run,
        presence_at=presence_at,
        last_message_at=last_message_at,
        idle_minutes=idle_minutes,
        has_pending_work=has_pending_work,
    )


# ── 审计 ─────────────────────────────────────────────────────────────


def _audit(entry: dict[str, Any]) -> None:
    """记一条巡检流水到 `logs/takeover_audit.jsonl`（**永不抛异常**）。

    与 `app/isolation/__init__.py::record_level_change` 同款契约：审计落盘失败只告警，
    绝不让"记不上账"变成"不巡检"。但日志里会留 warning，所以不会静默丢失。

    记什么：只记判定结果、原因、用量 —— 不记会话内容。审计的用途是回答
    "系统为什么在无人值守时动了手"，不是再存一份对话。
    """
    try:
        from pathlib import Path

        from app.audit_log import utc_timestamp

        log_dir = Path("logs")
        try:
            from app.config import settings

            log_dir = Path(getattr(settings, "log_dir", "") or "logs")
        except Exception:  # noqa: BLE001 — 配置不可读则用默认目录
            pass

        log_dir.mkdir(parents=True, exist_ok=True)
        payload = {"ts": utc_timestamp(), **entry}
        with (log_dir / AUDIT_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响巡检
        logger.warning("takeover audit log write failed", exc_info=True)


# ── 默认 runner ──────────────────────────────────────────────────────


async def _default_runner(row: dict[str, Any]) -> RunOutcome:
    """起一次**无人值守** run（`AgentRuntime(unattended=True)`）。

    与 `app/main.py::_auto_resume_runner` 同源，差别在上下文来源：续跑从 checkpoint 的
    `task_meta` 重建，接管则**必须**有 `task_meta` —— 没有它就不知道 `llm_config`
    （含 tools_mode）与 `system_prompt`，凭空造一个等于用错配置推进用户的活。

    所以拿不到 `task_meta` 时返回失败而不是勉强跑：这条会进 `consecutive_failures`，
    连续 3 次后熔断并提示用户"这个会话缺少可接管的上下文"。听起来严厉，但比
    "用默认配置悄悄改用户文件"安全得多。

    token 统计从 runtime 的 `usage` 事件累加，用于当日预算与 `tokens_used_today`。
    """
    session_id = str(row.get("session_id") or "")
    tenant_id = _row_int(row, "tenant_id")

    from app.agent import checkpoint

    snap = await checkpoint.load(tenant_id=tenant_id, session_id=session_id)
    if not snap:
        return RunOutcome(ok=False, error="no checkpoint snapshot; nothing to take over")
    meta = snap.get("task_meta")
    if not isinstance(meta, dict) or not meta:
        return RunOutcome(ok=False, error="snapshot has no task_meta; cannot rebuild run context")
    llm_config = meta.get("llm_config")
    if not isinstance(llm_config, dict):
        return RunOutcome(ok=False, error="snapshot has no llm_config")

    # 授权档位：用户当前属性（保存时快照）与快照里的档位取**更严**的那个，
    # 且无论如何 yolo 都被降为 auto（见 takeover.safe_tools_mode 的理由）。
    configured = l0.safe_tools_mode(row.get("tools_mode"))
    snapshot_mode = str(llm_config.get("tools_mode") or "")
    mode = configured
    if snapshot_mode and snapshot_mode != configured:
        # 取更严：ask < auto < yolo 的放行程度递增。
        order = {"ask": 0, "auto": 1, "yolo": 2}
        if order.get(l0.safe_tools_mode(snapshot_mode), 1) < order.get(configured, 1):
            mode = l0.safe_tools_mode(snapshot_mode)
    llm_config = {**llm_config, "tools_mode": mode}

    from app.agent.runtime import AgentTask
    from app.main import get_gateway
    from app.tools.context import set_tool_context

    gateway = await get_gateway()
    if gateway is None:
        return RunOutcome(ok=False, error="LLM gateway unavailable")

    from app.agent.event_sink import EventSink
    from app.agent.runtime import AgentRuntime
    from app.main import _session_cache
    from app.memory.service import get_service as get_memory_service

    set_tool_context(
        event_sink=EventSink(session_id=session_id),
        session_id=session_id,
        user_id=str(row.get("user_id") or ""),
        tenant_id=str(tenant_id),
        unattended=True,
        takeover=True,
    )

    task = AgentTask(
        id=f"takeover_{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        user_id=str(row.get("user_id") or ""),
        session_id=session_id,
        content=TAKEOVER_PROMPT,
        system_prompt=str(meta.get("system_prompt") or ""),
        llm_config=llm_config,
        workbench_context=dict(meta.get("workbench_context") or {}),
        max_turns=int(meta.get("max_turns") or 0) or 5,
        subagent_depth=0,
        run_token=uuid.uuid4().hex,
    )

    runtime = AgentRuntime(
        gateway=gateway,
        session_store=_session_cache,
        memory=get_memory_service(),
        unattended=True,
    )

    tokens = 0
    progressed = False
    try:
        async for ev in runtime.run(task):
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "usage":
                usage = ev.get("usage") or {}
                try:
                    tokens += int(usage.get("total_tokens") or 0) or (
                        int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                    )
                except (TypeError, ValueError):
                    pass
            elif ev.get("type") == "message" and str(ev.get("role") or "") == "assistant":
                progressed = True
    except Exception as exc:  # noqa: BLE001 - 单次 run 失败不能打散循环
        return RunOutcome(ok=False, tokens=tokens, error=str(exc)[:300])
    finally:
        # 清理本轮的 unattended 标记：它挂在 contextvar 上，不清会让同一 task
        # 里的后续调用继承"无人值守"——那条路径不该存在，但清了更安全。
        try:
            set_tool_context(unattended=False, takeover=False)
        except Exception:  # noqa: BLE001
            pass

    return RunOutcome(ok=True, tokens=tokens, progressed=progressed)


async def _record(pool: Any, *, row: dict[str, Any], decision: l0.Decision, outcome: RunOutcome | None, now: float) -> None:
    """把本拍结果写回配置行（用量、失败计数、熔断、一次性任务收尾）。

    写失败只告警：记账失败最多让下一拍重复评估，而抛异常会让整轮巡检中断。
    """
    if pool is None:
        return
    takeover_id = str(row.get("id") or "")
    if not takeover_id:
        return

    failures = _row_int(row, "consecutive_failures")
    used = _row_int(row, "tokens_used_today")
    if str(row.get("budget_day") or "") != _today(now):
        used = 0
    budget = _row_int(row, "daily_token_budget")

    verdict = decision.reason.value if decision.reason else "ran"
    last_run_at = _epoch(row.get("last_run_at"))

    if outcome is not None:
        used += outcome.tokens
        if outcome.ok:
            failures = 0
            last_run_at = now
            verdict = "ran" if outcome.progressed else "idle_run"
        else:
            failures += 1
            verdict = "failed"
    elif not decision.benign:
        failures += 1

    melt = l0.should_melt(
        consecutive_failures=failures, tokens_used_today=used, daily_token_budget=budget
    )

    # `once` 触发过后自动关闭：一次性任务不能因为某拍"用户正忙"就永远挂在到点状态。
    enabled = bool(row.get("enabled"))
    if outcome is not None and str(row.get("schedule_kind")) == l0.KIND_ONCE:
        enabled = False

    try:
        await pool.execute(
            """
            UPDATE superagent_takeovers
               SET enabled = $2,
                   tokens_used_today = $3,
                   budget_day = $4,
                   last_run_at = COALESCE($5, last_run_at),
                   last_verdict = $6,
                   last_reason = $7,
                   consecutive_failures = $8,
                   melt_state = $9,
                   updated_at = now()
             WHERE id = $1
            """,
            takeover_id,
            enabled,
            used,
            _today(now),
            datetime.fromtimestamp(last_run_at, UTC).replace(tzinfo=None)
            if last_run_at
            else None,
            verdict,
            decision.detail,
            failures,
            melt,
        )
    except Exception as exc:  # noqa: BLE001 - 记账失败是旁路
        logger.warning("takeover: record failed (id=%s): %s", takeover_id, exc)


# ── 一拍 ─────────────────────────────────────────────────────────────


async def tick(
    *,
    redis: Any,
    pool: Any = None,
    enabled: bool = False,
    now: float | None = None,
    rows: list[dict[str, Any]] | None = None,
    idle_minutes: int = 10,
    runner: Any = None,
    taskboard_fn: Any = None,
    last_message_fn: Any = None,
    record: bool = True,
) -> TickResult:
    """跑一拍。``enabled=False`` 时立即返回（零副作用，连 DB 都不查）。

    逐行**串行**处理而不是 `gather`：同时起 5 个 run 会瞬间打满并发、抢 LLM 配额，
    而且让"这一拍花了多少钱"变得难以归属。串行的代价是慢几十秒，接管场景不需要更快。

    每行独立的 try/except：一行坏掉不该让其它会话整轮失去巡检（与 `watchdog_loop`
    里 tick 级异常边界同一取向，只是这里下沉到行级）。
    """
    if not enabled:
        return TickResult(skipped_global="disabled")

    if not await _acquire_lease(redis):
        return TickResult(skipped_global="lease_busy")

    started_at = now if now is not None else time.time()
    result = TickResult()
    try:
        if rows is None:
            rows = await _load_rows(pool)
        result.considered = len(rows)

        if runner is None:
            runner = _default_runner

        for row in rows:
            try:
                await _tick_one(
                    redis=redis,
                    pool=pool,
                    row=row,
                    now=started_at,
                    idle_minutes=idle_minutes,
                    runner=runner,
                    taskboard_fn=taskboard_fn,
                    last_message_fn=last_message_fn,
                    do_record=record,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001 - 单行异常不打断整轮
                result.failed += 1
                logger.warning(
                    "takeover: row failed (id=%s): %s", row.get("id"), str(exc)[:200]
                )
        return result
    finally:
        await _release_lease(redis)


async def _tick_one(
    *,
    redis: Any,
    pool: Any,
    row: dict[str, Any],
    now: float,
    idle_minutes: int,
    runner: Any,
    taskboard_fn: Any,
    last_message_fn: Any,
    do_record: bool,
    result: TickResult,
) -> None:
    signal = await _collect(
        redis=redis,
        pool=pool,
        row=row,
        now=now,
        idle_minutes=idle_minutes,
        taskboard_fn=taskboard_fn,
        last_message_fn=last_message_fn,
    )
    decision = l0.decide(signal)

    if not decision.run:
        result.skip(str(decision.reason))
        _audit(
            {
                "action": "skip",
                "takeover_id": str(row.get("id") or ""),
                "session_id": str(row.get("session_id") or ""),
                "user_id": str(row.get("user_id") or ""),
                "reason": str(decision.reason),
                "detail": decision.detail,
            }
        )
        if do_record:
            await _record(pool, row=row, decision=decision, outcome=None, now=now)
        return

    session_id = str(row.get("session_id") or "")
    takeover_id = str(row.get("id") or "")

    # 接管段：先立标记再跑 run。顺序不能反 —— 前端要靠它决定何时上锁，
    # 若等 run 起来才标记，前几百毫秒用户仍能操作，正好是 run 读上下文的时候。
    await begin_segment(redis, session_id=session_id, takeover_id=takeover_id)
    outcome: RunOutcome | None = None
    try:
        outcome = await runner(row)
        result.started += 1
    finally:
        await end_segment(redis, session_id=session_id)

    _audit(
        {
            "action": "run",
            "takeover_id": takeover_id,
            "session_id": session_id,
            "user_id": str(row.get("user_id") or ""),
            "ok": bool(outcome and outcome.ok),
            "tokens": int(outcome.tokens) if outcome else 0,
            "progressed": bool(outcome and outcome.progressed),
            "error": (outcome.error if outcome else "")[:300],
        }
    )
    if do_record:
        await _record(pool, row=row, decision=decision, outcome=outcome, now=now)


async def _load_rows(pool: Any) -> list[dict[str, Any]]:
    """拉取"用户总开关打开、且会话开关打开"的配置行。

    总开关在 SQL 里 JOIN 而不是逐行查：接管的启用是**用户级 + 会话级**两道，
    只按会话级筛选会把总开关形同虚设。
    """
    if pool is None:
        return []
    try:
        records = await pool.fetch(
            """
            SELECT t.id::text              AS id,
                   t.tenant_id             AS tenant_id,
                   t.user_id               AS user_id,
                   t.session_id            AS session_id,
                   t.enabled               AS enabled,
                   t.schedule_kind         AS schedule_kind,
                   t.once_at               AS once_at,
                   t.interval_seconds      AS interval_seconds,
                   t.tools_mode            AS tools_mode,
                   t.daily_token_budget    AS daily_token_budget,
                   t.tokens_used_today     AS tokens_used_today,
                   t.budget_day            AS budget_day,
                   t.last_run_at           AS last_run_at,
                   t.cooldown_until        AS cooldown_until,
                   t.melt_state            AS melt_state,
                   t.consecutive_failures  AS consecutive_failures,
                   COALESCE(s.enabled, false)      AS user_enabled,
                   COALESCE(s.idle_minutes, 10)    AS idle_minutes
              FROM superagent_takeovers t
              LEFT JOIN superagent_settings s
                     ON s.tenant_id = t.tenant_id AND s.user_id = t.user_id
             WHERE t.enabled = true
             ORDER BY t.tenant_id, t.user_id, t.created_at
            """
        )
    except Exception as exc:  # noqa: BLE001 - 表没建（未迁移）/DB 抖动都按"无配置"
        logger.warning("takeover: load rows failed: %s", str(exc)[:200])
        return []
    return [dict(r) for r in (records or [])]


def _setting(name: str, default: Any) -> Any:
    """读配置；读不到就用默认值（fail-closed：默认值是"关"）。"""
    try:
        from app.config import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001
        return default


async def takeover_loop(redis: Any, *, should_stop: Any = None) -> None:
    """后台循环。间隔取配置；异常不得把循环打散（与 `watchdog_loop` 同一取向）。"""
    interval = int(_setting("superagent_takeover_tick_seconds", 60) or 60)
    interval = max(30, interval)
    logger.info("superagent takeover loop started (every %ss)", interval)
    while True:
        try:
            await _wait(interval)
            if should_stop is not None and should_stop():
                return
            if not bool(_setting("superagent_takeover_enabled", False)):
                continue
            pool = None
            try:
                from app.db import get_pool

                pool = get_pool()
            except Exception as exc:  # noqa: BLE001 - 没有 PG 就没有配置行
                logger.debug("takeover: db unavailable (%s)", exc)
            result = await tick(redis=redis, pool=pool, enabled=True)
            if result.started or result.failed:
                logger.info(
                    "superagent takeover tick: considered=%s started=%s failed=%s skipped=%s",
                    result.considered,
                    result.started,
                    result.failed,
                    result.skipped,
                )
        except Exception as exc:  # noqa: BLE001 - 接管自己不能把循环搞死
            logger.warning("superagent takeover tick failed: %s", str(exc)[:200])


async def _wait(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(max(1.0, float(seconds)))
