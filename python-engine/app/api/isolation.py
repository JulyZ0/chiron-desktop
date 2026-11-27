"""隔离档位 API —— 查询、切换、撤销（内部端点，共享密钥校验）。

六个端点：

- ``GET    /v1/system/isolation``         当前档位：请求值 / 实际值 / 能力矩阵 / 可选档位
- ``PUT    /v1/system/isolation``         切换档位（**只在进程内生效，不写 .env**）
- ``DELETE /v1/system/isolation``         撤销运行时覆盖，回到配置文件的值
- ``POST   /v1/system/isolation/revoke``  撤销**磁盘产物**（历史容器配置、目录标签）
- ``POST   /v1/system/isolation/probe``   清探测缓存并重新实测
- ``POST   /v1/system/isolation/container/prepare`` 下载并校验 Kiri 运行时包

## 为什么切换不写 .env

写盘会把"改一下试试"变成持久状态，撤销时要同时回滚文件和内存，还可能因为
进程重启时序不同而出现"文件改了但没生效"或反之。放在内存里：

* 切换立即对**后续**执行生效；
* 重启即回到配置文件的值 —— 撤销是免费的，且必然成功；
* ``DELETE`` 把内存变量清掉，语义与"从没改过"完全一致。

## 为什么需要两种撤销

用户要求"随时能够撤销或更改隔离程度"。这句话拆开是两件事：

1. **改回来**（``DELETE``）：撤销设置，恢复配置档位。
2. **擦掉痕迹**（``POST /revoke``）：历史容器 profile 与工作区完整性标签会
   **留在磁盘上**，不撤销会让目录长期处于 Low 标签，影响后续未隔离使用。

只做第一件是不够的 —— 那是"设置撤销"，不是"副作用撤销"。

## 鉴权

``X-API-Key`` 必须等于 ``LLM_GATEWAY_KEY``（与 ``/v1/plugins/*`` 同款）。
未配置时返回 503 —— 这些端点能改安全边界，不能在没有凭据的情况下开放。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import isolation as iso
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/system", tags=["isolation"])


def _verify_gateway_key(request: Request) -> None:
    """校验调用方凭据。**两条合法路径，缺一不可拒绝**。

    为什么要认两种头：

    * ``X-API-Key``：直连引擎（``/v1/plugins/*`` 同款），供运维脚本使用；
    * ``X-Internal-Token``：**前端的主路径** —— 网关用 ``PythonClient.GetJSON``
      重建请求，只注入 ``X-Internal-Token``，客户端带来的 ``X-API-Key`` 根本不会
      到达引擎。只认 ``X-API-Key`` 会让前端永远 401。

    两者未配置时返回 503：这些端点能改安全边界，宁可不可用也不可放开。
    """
    internal = settings.internal_token
    provided_internal = request.headers.get("X-Internal-Token", "")
    if internal and provided_internal and hmac.compare_digest(provided_internal, internal):
        return

    gateway_key = settings.llm_gateway_key
    provided_key = request.headers.get("X-API-Key", "")
    if gateway_key and provided_key and hmac.compare_digest(provided_key, gateway_key):
        return

    if not internal and not gateway_key:
        raise HTTPException(
            status_code=503,
            detail="neither INTERNAL_TOKEN nor LLM_GATEWAY_KEY configured",
        )
    raise HTTPException(status_code=401, detail="invalid gateway credentials")


def _actor(request: Request) -> str:
    """调用者标识，只用于审计。

    优先取网关注入的 ``X-User-ID``，其次退回客户端地址。**不记 token** ——
    审计文件不该成为凭据泄露源。
    """
    user = request.headers.get("X-User-ID", "")
    if user:
        return f"user:{user}"
    client = request.client
    return f"ip:{client.host}" if client else "unknown"


class LevelChange(BaseModel):
    """切换请求。

    ``level`` 只接受五个档位字面值。低于安全底线的值**允许传**：``job`` 直接生效，
    ``none`` 需要 ``acknowledge_below_floor=true`` —— 不传就被抬到底线，并在
    ``reasons`` 里说明。这里不拒绝，因为拒绝会让前端无法展示"你请求了 none，
    实际跑 low_il"这条重要信息。
    """

    level: str = Field(..., description="none | job | low_il | container")
    reason: str = Field("", max_length=500, description="为什么改，写进审计")
    #: "我确实要这个低于安全底线的档位"的显式声明。
    #:
    #: 只对**当前请求的那个档位**有效（见 `iso.resolve_level` 的 ① 段）：
    #: 用户确认一次 `none` 不会让之后配置里任何低档位都被放行。
    acknowledge_below_floor: bool = Field(
        False,
        description="低于安全底线的档位（如 none）必须显式确认为 true 才生效",
    )


@router.get("/isolation")
async def get_isolation(request: Request) -> dict[str, Any]:
    """当前隔离档位的完整快照。

    返回里 ``requested`` 是**配置请求值**，``effective`` 是**本机真能提供的值**。
    两者不同就是发生了降级 —— 前端必须把这件事显示出来，否则就是"静默降级"。

    ``status()`` 里的可用性判定是**端到端冒烟**（真的起一个子进程），首次调用
    可能要数秒（端到端起子进程 / 探测 Kiri socket）。所以它必须放到线程里跑：
    这是一条 ``async`` 端点，同步探测会把整个引擎事件循环堵住 —— 同进程里正在
    流式输出的对话、正在轮询的会话全都会跟着卡住，而用户只是打开了设置页。
    """
    _verify_gateway_key(request)
    payload = await asyncio.to_thread(_status_payload)
    override = iso.runtime_override()
    payload["runtime_override"] = override.value if override is not None else None
    return payload


def _status_payload() -> dict[str, Any]:
    """`iso.status()` 的线程内入口（见各端点里关于事件循环的说明）。"""
    return iso.status()


@router.post("/isolation/probe")
async def reprobe_isolation(request: Request) -> dict[str, Any]:
    """清掉档位探测缓存并**立刻重新实测**，返回最新状态。

    `selectable` 是端到端冒烟结果（真的以受限令牌起子进程），结果缓存在进程内。
    安全软件放行或 Kiri 刚启动之后，旧缓存仍可能把对应档位标成不可用 ——
    设置页看起来"永远灰着"，唯一的出路是重启引擎。这个端点就是那条出路：
    它不改任何档位，只让下一次判定重新实测。
    """
    _verify_gateway_key(request)
    await asyncio.to_thread(iso.reset_probes)
    payload = await asyncio.to_thread(_status_payload)
    override = iso.runtime_override()
    payload["runtime_override"] = override.value if override is not None else None
    return payload


@router.post("/isolation/container/prepare")
async def prepare_container_runtime(request: Request) -> dict[str, Any]:
    """容器挡位的运行时按需获取：下载固定版本的 Kiri 包并校验 SHA-256。

    **不安装、不提权**：Linux 的 `.deb`/`.rpm` 与 macOS 的 `.dmg` 都交给人（或部署
    流水线）执行；响应里的 `container.install_hint` 就是那条命令。这样"按需下载"
    不会变成"引擎替你静默装一个 root 服务"。

    下载失败/哈希不符时 `prepare.error` 给出原因，缓存里不会留下未校验的文件。
    """
    _verify_gateway_key(request)
    from app.isolation import kiri

    result = await asyncio.to_thread(kiri.prepare)
    # 用户可能刚装好运行时：清掉探测缓存，让返回的状态立刻反映"已就绪"。
    kiri.reset_probe()
    payload = await asyncio.to_thread(_status_payload)
    override = iso.runtime_override()
    payload["runtime_override"] = override.value if override is not None else None
    payload["prepare"] = {
        "ok": result.ok,
        "downloaded": result.downloaded,
        "error": result.error,
    }
    return payload


@router.put("/isolation")
async def set_isolation(body: LevelChange, request: Request) -> dict[str, Any]:
    """切换运行时隔离档位。

    响应包含切换**之后**的完整状态，前端不必再拉一次 GET。
    """
    _verify_gateway_key(request)

    try:
        level = iso.parse_level(body.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await asyncio.to_thread(
        _switch_level, level, body.acknowledge_below_floor, _actor(request), body.reason
    )
    return result


def _degrade_audit_reason(state: dict[str, Any]) -> str:
    """档位没生效时，给审计流水一条**非空**的机器原因。

    降级本身是允许的，但**静默**降级会让用户与运维都无从下手：审计里只剩
    ``requested=low_il effective=job reason=""``，看起来一切正常。
    原因直接取自 ``unavailable_reasons[requested]``，那里已经有"缺哪个机制"的细节。
    """
    requested = str(state.get("requested") or "")
    effective = str(state.get("effective") or "")
    if not requested or not effective or requested == effective:
        return ""
    reasons = state.get("unavailable_reasons") or {}
    detail = str(reasons.get(requested) or "").strip()
    head = f"档位未生效：{requested} → {effective}"
    return f"{head}；原因：{detail}" if detail else head


def _switch_level(
    level: iso.Level,
    acknowledge: bool,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """切换档位并返回切换后的完整状态（**在一个线程里**跑完）。

    为什么整段要在线程里：`resolve_level()` / `status()` 都会触发端到端探测，
    同步跑会堵住事件循环（见 `get_isolation`）。把"探测 + 切档"收进同一个线程，
    也保证 ``running_processes_unaffected`` 用的是同一份前后快照。
    """
    # 必须在 set 之前取：configured_level() 会把 runtime_override 算进去，
    # 设置之后再取就拿不到"配置文件原本是什么"了。
    previous = iso.runtime_override()
    config_level = iso.configured_level()
    # 切前的实际档位取 `status()["effective"]` 而不是裸 `resolve_level()`：
    # `status()` 里每个档位的探测各自 try/except（`_level_executable`），
    # 某一档探测出问题只会让那一档不可选，不会把整个切换动作打成 500。
    # 否则"关掉隔离"会被与它无关的前置探测（受限令牌 helper / AppContainer）
    # 挡住 —— 用户明明要的是"不要隔离"，却被要求先修好隔离。
    before = iso.status()

    iso.set_runtime_level(level, acknowledge_below_floor=acknowledge)
    state = iso.status()
    decision = iso.resolve_level()

    iso.record_level_change(
        action="set",
        requested=decision.requested.value,
        effective=decision.effective.value,
        previous=previous.value if previous is not None else None,
        actor=actor,
        # 用户没写理由时，若档位被降级，就把**机器原因**写进审计。
        # 否则这条流水只剩 "requested=low_il effective=job"，事后无解。
        reason=reason or _degrade_audit_reason(state),
    )

    # 切换**立刻**影响后续执行，但已经在跑的进程不受影响（各自的隔离在 spawn 时定了）。
    # 把这件事明说，否则用户会以为改完就能约束正在运行的进程。
    running = before["effective"] != state["effective"]

    return {
        **state,
        "runtime_override": level.value,
        "running_processes_unaffected": running,
        "note": (
            "与配置文件一致"
            if level is config_level
            else "已在进程内生效，重启后回到配置文件的值"
        ),
    }


@router.delete("/isolation")
async def clear_isolation(request: Request) -> dict[str, Any]:
    """撤销运行时覆盖，回到配置文件的值。

    幂等：本来没有覆盖时也返回 200，``cleared`` 为 ``None``。
    """
    _verify_gateway_key(request)

    previous, state = await asyncio.to_thread(_clear_level, _actor(request))
    return {
        **state,
        "runtime_override": None,
        "cleared": previous.value if previous is not None else None,
    }


def _clear_level(actor: str) -> tuple[iso.Level | None, dict[str, Any]]:
    previous = iso.clear_runtime_level()
    decision = iso.resolve_level()
    state = iso.status()

    iso.record_level_change(
        action="clear",
        requested=decision.requested.value,
        effective=decision.effective.value,
        previous=previous.value if previous is not None else None,
        actor=actor,
        reason="撤销运行时覆盖",
    )
    return previous, state


@router.post("/isolation/revoke")
async def revoke_isolation(request: Request) -> dict[str, Any]:
    """撤销**磁盘上的**隔离产物：AppContainer profile、路径授权、工作区完整性标签。

    与 ``DELETE`` 的区别：``DELETE`` 只撤销"我改的设置"，这个撤销"我留下的痕迹"。
    Job Object 与 Low IL 是进程级的，进程退出即消失，不需要也无法在这里撤销。
    """
    _verify_gateway_key(request)

    result = iso.revoke_all()
    iso.record_level_change(
        action="revoke_artifacts",
        requested=iso.configured_level().value,
        effective=iso.resolve_level().effective.value,
        previous=None,
        actor=_actor(request),
        reason="撤销磁盘产物",
    )
    return {"ok": True, **result}
