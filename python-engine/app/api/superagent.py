"""Super-agent 接管的**运行时**端点（内部端点，共享密钥校验）。

## 职责边界：为什么这里只有四个端点，没有配置 CRUD

配置（开关、定时、预算）落在 PG 的 `superagent_takeovers` 表，**增删改查在 Go 网关**
（`internal/api/routes_superagent.go`）。理由：前端的所有配置请求都走网关并带 JWT，
配置的租户/用户归属由网关的 claims 决定；让引擎也实现一套 CRUD，等于让同一份数据
有两个写入口，两边的校验规则迟早分叉。

引擎这边只负责**只有引擎才知道的运行时事实**：

- ``POST  /v1/superagent/presence``            前端心跳（让步信号 2 的唯一写入点）
- ``GET   /v1/superagent/segment/{session_id}`` 该会话是否处于接管段（前端据此上锁）
- ``POST  /v1/superagent/interrupt``           打断本次接管（停 run + 清段 + 冷却）
- ``GET   /v1/superagent/status``              引擎侧开关状态（给 UI 显示"服务器未启用"）

## 为什么 ``status`` 必须存在

`superagent_takeover_enabled` 是**引擎进程**的配置，不是用户的配置。它关着的时候，
巡检循环根本不会创建（见 `worker._start_takeover`），用户的定时配置保存成功却永远不跑。
没有这个端点，前端只能显示"已开启"并让用户等一个永远不会到来的巡检 ——
那比功能缺失更糟，因为它看起来是好的。

## 打断为什么是"双拍"

打断必须同时做两件事，缺一件都会出现"打断后几十秒又起来"：

1. ``request_interrupt(session_id)`` —— 让**引擎里那个 run** 停下来；
2. 写 ``cooldown_until`` —— 让**下一拍巡检**不再启动。

只做 1：run 停了，但下一拍 L0 判定仍然通过，立刻又起一个。
只做 2：巡检停了，但正在跑的 run 会继续，用户以为打断生效了却还在被改文件。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent import takeover as l0
from app.api.isolation import _verify_gateway_key
from app.config import settings
from app.queue import takeover_job as job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/superagent", tags=["superagent"])


async def _redis() -> Any:
    """取 Redis；不可用返回 None（各端点自行决定"没有 Redis 时怎么退化"）。"""
    try:
        from app.redis_client import get_redis

        return await get_redis()
    except Exception as exc:  # noqa: BLE001
        logger.debug("superagent: redis unavailable (%s)", exc)
        return None


async def _pool() -> Any:
    try:
        from app.db import get_pool

        return get_pool()
    except Exception as exc:  # noqa: BLE001
        logger.debug("superagent: db unavailable (%s)", exc)
        return None


def _user_id(request: Request, body_value: str = "") -> str:
    """身份来源：网关注入的 ``X-User-Id`` 优先，其次请求体。

    不信任请求体里自报的 user_id 作为**鉴权**依据 —— 它只用来做键名，
    真正的边界是共享密钥（只有网关持有）。两者都要：密钥防外部调用，
    header 防"网关代 A 用户请求却操作了 B 用户的 presence"。
    """
    return request.headers.get("X-User-Id", "") or body_value


def _tenant_id(request: Request) -> int:
    raw = request.headers.get("X-Tenant-Id", "")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


# ── presence ─────────────────────────────────────────────────────────


class PresenceBody(BaseModel):
    user_id: str = Field("", max_length=36, description="缺省时取 X-User-Id")


@router.post("/presence")
async def post_presence(request: Request, body: PresenceBody | None = None) -> dict[str, Any]:
    """前端心跳：用户还在这个页面上。

    前端每 30 秒一次（见 `TakeoverPanel.vue` 的定时器）。频率的取舍：
    太快是纯浪费（每秒一次对判定没有增益，让步窗口是分钟级）；
    太慢会让"用户刚走开"被误判为"已空闲"—— 10 分钟窗口配 30 秒心跳有 20 倍余量。

    Redis 不可用时返回 200 + ``ok: false`` 而不是 5xx：心跳失败不该让前端报错弹窗，
    它会在下一拍重试，而判定侧在"读不到 presence"时的取向本来就是保守的。
    """
    _verify_gateway_key(request)
    redis = await _redis()
    user_id = _user_id(request, body.user_id if body else "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    ok = await job.mark_presence(redis, user_id=user_id)
    return {
        "ok": ok,
        "user_id": user_id,
        "ttl_seconds": job.PRESENCE_TTL_SECONDS if ok else 0,
    }


# ── 接管段 ───────────────────────────────────────────────────────────


@router.get("/segment/{session_id}")
async def get_segment(request: Request, session_id: str) -> dict[str, Any]:
    """该会话是否正在被接管。

    前端拿到 ``active: true`` 就显示覆盖层并锁定操作（保留"打断接管"按钮）。
    特意**同时**返回 ``takeover_id``：打断时要把它带上，否则引擎无法判断
    "用户打断的是不是当前这一段"（旧段已结束、新段刚开始的竞态）。

    Redis 不可用时返回 ``active: false`` 且 ``known: false`` —— 让前端知道
    "我不是确认了没有接管，而是查不到"。若不区分，一次 Redis 抖动就会让
    覆盖层在接管中途消失，用户正好在 run 改文件时开始输入。
    """
    _verify_gateway_key(request)
    redis = await _redis()
    snap = await job.segment_snapshot(redis, session_id=session_id)
    if snap is None:
        return {
            "active": False,
            "known": redis is not None,
            "session_id": session_id,
            "takeover_id": "",
            "started_at": None,
        }
    return {
        "active": True,
        "known": True,
        "session_id": session_id,
        "takeover_id": str(snap.get("takeover_id") or ""),
        "started_at": snap.get("started_at"),
    }


# ── 打断 ─────────────────────────────────────────────────────────────


class InterruptBody(BaseModel):
    session_id: str = Field(..., max_length=64)
    takeover_id: str = Field("", max_length=36, description="空 = 打断当前这一段")
    reason: str = Field("", max_length=500)


@router.post("/interrupt")
async def post_interrupt(request: Request, body: InterruptBody) -> dict[str, Any]:
    """打断本次接管。**双拍齐发**（见模块文档）。

    冷却时长的语义由 `l0.apply_interrupt` 决定，不是这里拍脑袋定的：
    ``once`` 模式本次即全部 ⇒ 直接关闭；``interval`` 模式只取消这一次，
    下个间隔到期再评估（用户打断的是"这一次"，不是"以后都不许"）。
    """
    _verify_gateway_key(request)
    session_id = body.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    redis = await _redis()
    now = time.time()

    # 1) 先把正在跑的 run 停住。**即使后面写库失败也要做** ——
    #    让用户尽快拿回控制权优先于记账完整。
    stopped = False
    try:
        from app.agent import interrupt as interrupt_mod

        stopped = await interrupt_mod.request_interrupt(session_id, redis=redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("superagent: interrupt flag failed (%s): %s", session_id, exc)

    # 2) 清段标记：覆盖层立刻消失。
    await job.end_segment(redis, session_id=session_id)

    # 3) 写冷却，让下一拍不再启动。
    cooldown_until, disabled = await _apply_cooldown(
        session_id=session_id, takeover_id=body.takeover_id, now=now
    )

    job._audit(  # noqa: SLF001 — 审计入口与 tick 共用同一份 jsonl
        {
            "action": "interrupt",
            "takeover_id": body.takeover_id,
            "session_id": session_id,
            "user_id": _user_id(request),
            "reason": body.reason,
            "stopped_run": stopped,
            "cooldown_until": cooldown_until,
            "disabled": disabled,
        }
    )

    return {
        "ok": True,
        "session_id": session_id,
        "stopped_run": stopped,
        "cooldown_until": cooldown_until,
        "disabled": disabled,
        "note": (
            "本次接管已取消（一次性任务不会再自动启动）"
            if disabled
            else "本次接管已取消，下个间隔到期后会重新评估"
        ),
    }


async def _apply_cooldown(
    *, session_id: str, takeover_id: str, now: float
) -> tuple[float | None, bool]:
    """把 ``cooldown_until``（或"关闭开关"）写回配置行。

    返回 ``(冷却截止时刻, 是否已关闭)``。``None`` 表示无法确定 ——
    前端据此提示"已停止当前 run，但定时未取消"，不假装成功。
    """
    pool = await _pool()
    if pool is None:
        return None, False

    row = None
    try:
        row = await pool.fetchrow(
            """
            SELECT id::text AS id, schedule_kind, interval_seconds, enabled
              FROM superagent_takeovers
             WHERE session_id = $1 AND ($2 = '' OR id::text = $2)
             ORDER BY created_at DESC
             LIMIT 1
            """,
            session_id,
            takeover_id,
        )
    except Exception as exc:  # noqa: BLE001 - 表未建/DB 抖动都按"无法确定"
        logger.warning("superagent: cooldown lookup failed (%s): %s", session_id, exc)
        return None, False
    if row is None:
        return None, False

    signal = l0.Signal(
        now=now,
        schedule_kind=str(row["schedule_kind"] or l0.KIND_INTERVAL),
        interval_seconds=int(row["interval_seconds"] or 0),
    )
    until = l0.apply_interrupt(signal, cancelled_at=now)

    # `+inf` 不能写进 timestamp 列：它代表"不要再自动启动"，落库形式是关掉开关。
    if until == float("inf"):
        try:
            await pool.execute(
                """
                UPDATE superagent_takeovers
                   SET enabled = false, cooldown_until = NULL,
                       last_verdict = 'interrupted', last_reason = 'user interrupted (once)',
                       updated_at = now()
                 WHERE id = $1
                """,
                row["id"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("superagent: disable after interrupt failed: %s", exc)
            return None, False
        return None, True

    from datetime import UTC, datetime

    try:
        await pool.execute(
            """
            UPDATE superagent_takeovers
               SET cooldown_until = $2, last_verdict = 'interrupted',
                   last_reason = 'user interrupted', updated_at = now()
             WHERE id = $1
            """,
            row["id"],
            datetime.fromtimestamp(until, UTC).replace(tzinfo=None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("superagent: cooldown write failed: %s", exc)
        return None, False
    return until, False


# ── 引擎侧状态 ───────────────────────────────────────────────────────


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    """引擎侧接管的**能力与开关**状态。

    与用户配置无关：这里回答的是"这台引擎愿不愿意巡检"。前端必须据此区分
    "你打开了但服务器没启用"和"你关着"—— 否则用户面对一个永远不触发的功能，
    只能怀疑自己配错了。
    """
    _verify_gateway_key(request)
    enabled = bool(getattr(settings, "superagent_takeover_enabled", False))
    tick = int(getattr(settings, "superagent_takeover_tick_seconds", 60) or 60)
    return {
        "engine_enabled": enabled,
        "tick_seconds": max(30, tick),
        "max_sessions": l0.MAX_TAKEOVER_SESSIONS,
        "min_interval_seconds": l0.MIN_INTERVAL_SECONDS,
        "max_once_horizon_seconds": l0.MAX_ONCE_HORIZON_SECONDS,
        "presence_ttl_seconds": job.PRESENCE_TTL_SECONDS,
        "segment_ttl_seconds": job.SEGMENT_TTL_SECONDS,
        # 无人值守**永远**不继承 yolo —— 前端在配置页要把这条说清楚，
        # 否则用户会以为"我在聊天里开了 yolo，接管也会自动执行"。
        "unattended_tools_mode": l0.UNATTENDED_FALLBACK_MODE,
        "unsafe_tools_mode": l0.UNATTENDED_UNSAFE_MODE,
    }
