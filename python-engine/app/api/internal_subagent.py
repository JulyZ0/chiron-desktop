"""S3：远端子 agent 的**接收端**（内网端点）。

认证形态是 **body-身份端点**：走 `X-Internal-Token`（与网关代理路径同一凭据，由
`app/middleware/auth.py` 校验），身份由本端点自己从 body 取 —— 与 `/v1/agent/submit` 同形态，
因此不需要额外的头剥离逻辑（该形态的存在前提正是"网关已剥离客户端可伪造的同名头"）。

**接收方重新校验**（安全前置，逐条落地）：

* `tenant_id` / `user_id` **必须非空** —— 拒绝"无归属"的跨实例调用（否则无从做租户隔离与审计）；
* **不信任请求体里的任何"已授权"标记**：只读与否由本实例按 `allow_write` 重新决定，
  不接受调用方声称的"已批准"；
* 深度受 `MAX_DEPTH` 约束 —— 远端不得成为绕过递归限制的通道；
* 结果一律经 `to_tool_payload()` 返回，其中的 `output` 已带 `<subagent-result>` 不可信包裹
  （调用侧还会再补一道，见 `app/subagent/remote.py`）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

#: 重跑事件冲刷任务的引用（不持引用会被 GC 静默回收 —— 那时事件就再也不会进 Redis）
_RERUN_TASKS: set[asyncio.Task[Any]] = set()


class InternalSubagentRequest(BaseModel):
    """跨实例委派请求（**身份必填**：这是接收方重新校验的依据）。"""

    task: str
    tenant_id: int = 0
    user_id: str = ""
    session_id: str = ""
    mode: str = "normal"
    max_turns: int = 40
    allow_write: bool = False
    #: A4：per-call **收窄**（`None` = 不收窄；空列表 = 不允许任何工具）。
    #: 注意方向：这是**收窄**而非授权，所以与本文档开头的"不信任请求体里的授权标记"不冲突
    #: —— 远端**只能要得更少**，天花板仍由本实例按 Profile 决定。
    call_tools: list[str] | None = None
    #: R2：本次委派的**写路径声明**。远端知道自己的目标文件，
    #: 报上来才能与其它可写委派按路径并行（否则未声明 = 整工作区独占，只能串行）。
    #: 注意它**只收窄粒度、不放宽权限** —— "能不能写"仍由本实例的 `allow_write` 决定。
    write_paths: list[str] | None = None
    depth: int = 0
    #: 父回合是否无人值守（自动续跑）。与 `call_tools`/`write_paths` 同一方向：请求体只能
    #: **收窄**本实例的行为 —— 置真让审批闸门立刻拒绝，置假最多退回等待，都换不到权限。
    unattended: bool = False


@router.post("/v1/internal/subagent/run")
async def internal_subagent_run(body: InternalSubagentRequest) -> dict[str, Any]:
    """在**本实例**执行一次远端委派（同步，返回工具载荷形态的结果）。"""
    from app.tools.subagent import MAX_DEPTH, MAX_TURNS_CAP

    if not (body.task or "").strip():
        return {"ok": False, "error": "task is required"}
    if not body.tenant_id or not body.user_id:
        # 身份重校验：无归属的跨实例调用一律拒绝（宁可让调用方回退本实例）
        logger.warning("internal subagent rejected: missing tenant/user identity")
        return {"ok": False, "error": "tenant_id and user_id are required"}

    depth = max(0, int(body.depth or 0))
    if depth >= MAX_DEPTH:
        return {"ok": False, "error": f"delegation depth exceeded (max {MAX_DEPTH})"}

    from app.main import get_gateway

    gateway = await get_gateway()
    if gateway is None:
        return {"ok": False, "error": "engine gateway unavailable"}

    from app.agent.subagent_runner import SubAgentRunner
    from app.subagent.store import SubagentRunStore

    pool = None
    store = None
    try:
        from app.db import get_pool

        pool = get_pool()
        if pool is not None:
            store = SubagentRunStore(pool)
    except Exception as exc:  # noqa: BLE001 — 落库能力缺失不该阻断委派（与工具层同语义）
        logger.warning("internal subagent: db unavailable (run will not be persisted): %s", exc)

    runner = SubAgentRunner(
        gateway,
        store=store,
        pool=pool,
        depth=depth,
        parent_session_id=body.session_id,
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        background=False,
        allow_write=bool(body.allow_write),
        call_tools=body.call_tools,
        write_paths=body.write_paths,
        unattended=bool(body.unattended),
    )
    try:
        result = await runner.run(
            body.task,
            mode=body.mode or "normal",
            max_turns=max(1, min(int(body.max_turns or 40), MAX_TURNS_CAP)),
        )
    except Exception as exc:  # noqa: BLE001 — 单次委派失败以结构化错误回给调用方
        logger.warning("internal subagent run failed (session=%s): %s", body.session_id, exc)
        return {"ok": False, "error": f"subagent run failed: {str(exc)[:200]}"}

    payload = result.to_tool_payload()
    return {"ok": True, "data": payload if isinstance(payload, dict) else {}}


class InternalSubagentRerunRequest(BaseModel):
    """重跑请求（**身份必填**，与 ``/run`` 同一套接收方重新校验）。

    刻意**没有** ``task`` 字段：重跑必须用库里存的原任务描述。让调用方随请求带一份
    "任务文本"，等于是给它一个伪造上下文的口子（与 ``/run`` 的"不信任请求体里的授权
    标记"同一条纪律）。取不到原任务 ⇒ 409，由前端把任务放回输入框、交用户确认。
    """

    tenant_id: int = 0
    user_id: str = ""
    #: 原 run 归属的会话（网关从 subagent_runs.root_session_id 带过来）。
    #: 访问谓词按它过滤 ⇒ 传错会话一律 404，不会"重跑到别的会话里"。
    session_id: str = ""


async def _deliver_rerun_event(payload: Any, runtime_cache: Any, cache_tenant: str) -> None:
    """把重跑 run 的事件投进运行期缓存（Stream + pub/sub），网关中继后进会话事件流。

    与 ``main.py`` 的 ``_persist_subagent_event`` **同一口径**（那边是父 SSE 路径）：
    Stream 供回放、pub/sub 供实时；``event_id`` 供多副本网关去重。
    """
    from app.subagent import registry as subagent_registry

    if not isinstance(payload, dict):
        return
    run_id = str(payload.get("run_id") or "")
    if run_id:
        # 事件即心跳：看门狗据此认定"仍在产出内容的 run"没有卡死
        subagent_registry.touch(run_id)
    if runtime_cache is None:
        return
    if run_id:
        await runtime_cache.push_event(run_id=run_id, tenant=cache_tenant, payload=payload)
    await runtime_cache.publish_live_event(payload={**payload, "event_id": uuid.uuid4().hex})


async def _drain_sink_until_done(sink: Any, run_id: str) -> None:
    """把合并类事件（text/reasoning/notice）冲出来。

    为什么需要：``EventSink`` 对内容型事件做**合并窗口**缓冲，只有 ``drain()`` 会把它
    们推进投递队列 —— 而这条路径**没有**父 SSE 生成器去 drain（``main.py`` 里是
    生成器每轮调用）。少了这一步，重跑 run 的正文/思考永远留在 sink 里，前端只能看到
    status / tool 事件，看起来像"跑起来了但什么都不输出"。
    """
    from app.subagent import registry as subagent_registry

    try:
        while True:
            sink.drain()
            if not subagent_registry.is_active(run_id):
                # 终态前的最后一段内容可能刚好落在窗口内：等过窗口再冲一次再退出
                await asyncio.sleep(0.3)
                sink.drain()
                return
            await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 冲刷是旁路，失败不能影响子 Agent
        logger.debug("subagent rerun sink drain stopped: %s", str(exc)[:160])


@router.post("/v1/internal/subagent/runs/{run_id}/rerun")
async def internal_subagent_rerun(
    run_id: str, body: InternalSubagentRerunRequest
) -> dict[str, Any]:
    """按**原任务描述**重新派发一个已结束的子 Agent run（P0-2）。

    立即返回新 run 的 id（后台运行）；原 run 的终态校验、任务描述读取、写权限沿用与
    ``rerun_subagent`` 工具**同一份实现**（:mod:`app.subagent.rerun`）。

    状态码口径（网关据此如实回给前端，不做"假成功"）：

    =========  ==========================================================
    404        不存在 / 不属于该会话-租户-用户（不泄露存在性）
    409        原 run 非终态、或库里没有任务描述（要求前端重发）
    503        落库 / 派发能力不可用（引擎未接网关、DB 不可用、配额拒绝）
    =========  ==========================================================
    """
    if not (body.user_id or "").strip() or not body.tenant_id:
        # 身份重校验：无归属的重跑一律拒绝（否则无从做租户隔离与审计）
        logger.warning("internal subagent rerun rejected: missing tenant/user identity")
        raise HTTPException(
            status_code=400, detail={"code": "identity_required",
                                     "error": "tenant_id and user_id are required"}
        )
    session_id = (body.session_id or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "session_required", "error": "session_id is required"},
        )

    try:
        from app.main import get_gateway

        gateway = await get_gateway()
    except Exception as exc:  # noqa: BLE001 - 引擎未完成启动时如实报"不可用"
        logger.warning("internal subagent rerun: gateway unavailable: %s", str(exc)[:160])
        gateway = None
    if gateway is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "engine_unavailable", "error": "engine gateway unavailable"},
        )

    # ── 事件旁路：新 run 的 subagent.* 必须出现在**会话事件流**里 ──
    # 这条路径没有父 SSE 生成器，所以由端点自己造一个 sink 并挂常驻投递器
    # （Stream + pub/sub → 网关 StartSubagentEventsRelay → SSE hub）。
    from app.agent.event_sink import EventSink

    sink = EventSink(session_id=session_id)
    runtime_cache = None
    try:
        from app.subagent.runtime_cache import get_runtime_cache

        runtime_cache = await get_runtime_cache()
    except Exception as cache_err:  # noqa: BLE001 - 缓存不可用不影响重跑本身
        logger.debug("subagent rerun: runtime cache unavailable: %s", str(cache_err)[:160])
    sink.attach_persistent(
        lambda payload: _deliver_rerun_event(payload, runtime_cache, str(body.tenant_id))
    )

    from app.subagent.rerun import RerunRefusedError, rerun_run
    from app.tools.context import get_all, restore_context, set_tool_context

    snapshot = get_all()
    try:
        set_tool_context(
            session_id=session_id,
            tenant_id=str(body.tenant_id),
            user_id=str(body.user_id),
            gateway=gateway,
            event_sink=sink,
            # 重跑没有人在等审批/提问：按无人值守处理（审批闸门直接拒绝而不是空等）
            unattended=True,
        )
        payload = await rerun_run(run_id=run_id)
    except RerunRefusedError as exc:
        logger.info("internal subagent rerun refused (run=%s, code=%s)", run_id, exc.code)
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code or "refused", "error": exc.error},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 未预期错误也要如实回答
        logger.warning("internal subagent rerun failed (run=%s): %s", run_id, str(exc)[:200])
        raise HTTPException(
            status_code=503,
            detail={"code": "rerun_failed", "error": f"rerun failed: {str(exc)[:160]}"},
        ) from exc
    finally:
        # 上下文是共享的：不清掉会让同一请求处理链上的后续调用继承"无人值守 + 这份 sink"
        restore_context(snapshot)

    new_run_id = str(payload.get("run_id") or "")
    if new_run_id:
        task = asyncio.create_task(_drain_sink_until_done(sink, new_run_id))
        _RERUN_TASKS.add(task)
        task.add_done_callback(_RERUN_TASKS.discard)

    logger.info(
        "internal subagent rerun accepted: from=%s to=%s session=%s",
        run_id, new_run_id, session_id,
    )
    return {"ok": True, **payload}


class InternalSubagentSteerRequest(BaseModel):
    """给运行中的子 Agent 追加一条指令（父→子 steer，功能 F1）。"""

    text: str
    sender: str = "user"
    user_id: str = ""


@router.post("/v1/internal/subagent/runs/{run_id}/steer")
async def internal_subagent_steer(
    run_id: str, body: InternalSubagentSteerRequest
) -> dict[str, Any]:
    """把指令投到**运行中**子 Agent 的轮次边界（不停 run，不插工具调用中间）。

    网关已做归属校验（run 属于调用者），这里只负责投递并把引擎的稳定错误码映射成
    HTTP 状态 —— 前端据此区分"该重试"与"不该重试"。

    =========  ==========================================================
    404        该 run 不在本实例（未注册/已收尾/已轮转）
    409        run 已结束（明确"话没送到"，绝不静默成功）
    400        文本非法（空/超长）
    429        该 run 的待注入队列已满
    503        投递通道不可用（Redis 故障等）
    =========  ==========================================================
    """
    from app.agent import steer as steer_mod

    result = await steer_mod.steer(
        run_id, body.text, sender=(body.sender or "user")
    )
    if result.ok:
        logger.info(
            "subagent steer accepted run=%s child_session=%s sender=%s",
            run_id,
            result.child_session[:12],
            body.sender or "user",
        )
        return {"ok": True, "delivery": result.reason or "shared"}

    status = {
        steer_mod.CODE_RUN_NOT_FOUND: 404,
        steer_mod.CODE_RUN_FINISHED: 409,
        steer_mod.CODE_INVALID_MESSAGE: 400,
        steer_mod.CODE_QUEUE_FULL: 429,
        steer_mod.CODE_DELIVERY_FAILED: 503,
    }.get(result.code, 400)
    raise HTTPException(
        status_code=status,
        detail={"code": result.code, "error": result.reason},
    )


class InternalSubagentDispatchRequest(BaseModel):
    """从界面直接发起一次后台委派（F3）。"""

    task: str
    session_id: str
    tenant_id: int = 0
    user_id: str = ""
    profile: str = ""
    allow_write: bool = False
    max_turns: int = 10


@router.post("/v1/internal/subagent/dispatch")
async def internal_subagent_dispatch(
    body: InternalSubagentDispatchRequest,
) -> dict[str, Any]:
    """在**当前会话**下直接启动一个后台子 Agent（不经过父模型的一次 turn）。

    网关已校验会话归属；这里复用 `subagent` 工具的完整后台路径（注册表 / 预算 /
    落库 / 事件旁路），因此界面发起的委派与模型自己 `subagent(...)` 完全同源。

    审批/提问仍会走 `subagent.*` 事件回到界面；与"无人值守重跑"不同，这里
    `unattended=False`，用户可以在面板里批准子 Agent 的请求。
    """
    if not (body.task or "").strip():
        raise HTTPException(status_code=400, detail={"code": "task_required", "error": "task is required"})
    if not (body.session_id or "").strip() or not body.tenant_id or not (body.user_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "identity_required", "error": "session_id, tenant_id and user_id are required"},
        )
    try:
        from app.main import get_gateway

        gateway = await get_gateway()
    except Exception:  # noqa: BLE001
        gateway = None
    if gateway is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "engine_unavailable", "error": "engine gateway unavailable"},
        )

    from app.agent.event_sink import EventSink
    from app.subagent.runtime_cache import get_runtime_cache
    from app.tools.context import get_all, restore_context, set_tool_context
    from app.tools.subagent import MAX_TURNS_CAP, subagent

    session_id = body.session_id.strip()
    sink = EventSink(session_id=session_id)
    runtime_cache = None
    try:
        runtime_cache = await get_runtime_cache()
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不影响委派
        logger.debug("subagent dispatch: runtime cache unavailable: %s", str(exc)[:160])
    sink.attach_persistent(
        lambda payload: _deliver_rerun_event(payload, runtime_cache, str(body.tenant_id))
    )

    snapshot = get_all()
    try:
        set_tool_context(
            session_id=session_id,
            tenant_id=str(body.tenant_id),
            user_id=body.user_id,
            gateway=gateway,
            event_sink=sink,
            subagent_depth=0,
            unattended=False,
        )
        result = await subagent(
            task=body.task,
            profile=body.profile or "",
            run_in_background=True,
            allow_write=bool(body.allow_write),
            max_turns=max(1, min(int(body.max_turns or MAX_TURNS_CAP), MAX_TURNS_CAP)),
        )
    finally:
        restore_context(snapshot)

    run_id = str(result.get("run_id") or "") if isinstance(result, dict) else ""
    if not run_id:
        err = result.get("error") if isinstance(result, dict) else "dispatch failed"
        logger.warning("subagent dispatch refused (session=%s): %s", session_id, str(err)[:200])
        raise HTTPException(
            status_code=409,
            detail={"code": "dispatch_refused", "error": str(err or "dispatch refused")[:300]},
        )

    task = asyncio.create_task(_drain_sink_until_done(sink, run_id))
    _RERUN_TASKS.add(task)
    task.add_done_callback(_RERUN_TASKS.discard)
    logger.info("subagent dispatch accepted: run=%s session=%s", run_id, session_id)
    return {"ok": True, "run_id": run_id, "status": "running"}
