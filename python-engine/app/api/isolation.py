"""隔离档位 API —— 查询、切换、撤销（内部端点，共享密钥校验）。

四个端点：

- ``GET    /v1/system/isolation``         当前档位：请求值 / 实际值 / 能力矩阵 / 可选档位
- ``PUT    /v1/system/isolation``         切换档位（**只在进程内生效，不写 .env**）
- ``DELETE /v1/system/isolation``         撤销运行时覆盖，回到配置文件的值
- ``POST   /v1/system/isolation/revoke``  撤销**磁盘产物**（AppContainer profile、目录标签）

## 为什么切换不写 .env

写盘会把"改一下试试"变成持久状态，撤销时要同时回滚文件和内存，还可能因为
进程重启时序不同而出现"文件改了但没生效"或反之。放在内存里：

* 切换立即对**后续**执行生效；
* 重启即回到配置文件的值 —— 撤销是免费的，且必然成功；
* ``DELETE`` 把内存变量清掉，语义与"从没改过"完全一致。

## 为什么需要两种撤销

用户要求"随时能够撤销或更改隔离程度"。这句话拆开是两件事：

1. **改回来**（``DELETE``）：撤销设置，恢复配置档位。
2. **擦掉痕迹**（``POST /revoke``）：AppContainer profile 与工作区完整性标签会
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

    level: str = Field(..., description="none | job | low_il | restricted | appcontainer")
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
    """
    _verify_gateway_key(request)
    payload = iso.status()
    payload["runtime_override"] = (
        iso.runtime_override().value if iso.runtime_override() is not None else None
    )
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

    # 必须在 set 之前取：configured_level() 会把 runtime_override 算进去，
    # 设置之后再取就拿不到"配置文件原本是什么"了。
    previous = iso.runtime_override()
    config_level = iso.configured_level()
    previous_decision = iso.resolve_level()

    iso.set_runtime_level(level, acknowledge_below_floor=body.acknowledge_below_floor)
    decision = iso.resolve_level()

    iso.record_level_change(
        action="set",
        requested=decision.requested.value,
        effective=decision.effective.value,
        previous=previous.value if previous is not None else None,
        actor=_actor(request),
        reason=body.reason,
    )

    # 切换**立刻**影响后续执行，但已经在跑的进程不受影响（各自的隔离在 spawn 时定了）。
    # 把这件事明说，否则用户会以为改完就能约束正在运行的进程。
    running = previous_decision.effective != decision.effective

    return {
        **iso.status(),
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

    previous = iso.clear_runtime_level()
    decision = iso.resolve_level()

    iso.record_level_change(
        action="clear",
        requested=decision.requested.value,
        effective=decision.effective.value,
        previous=previous.value if previous is not None else None,
        actor=_actor(request),
        reason="撤销运行时覆盖",
    )

    return {
        **iso.status(),
        "runtime_override": None,
        "cleared": previous.value if previous is not None else None,
    }


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


# ── 提权 helper（受限令牌的执行前置） ─────────────────────────────
#
# 为什么这必须是一个**用户显式触发**的端点：它弹一次 UAC，而用户同意的是
# "提权执行通道"。引擎自己在启动时静默弹窗是不可接受的 —— 用户不知道自己在
# 同意什么。所以这条路只能由设置页的按钮走到这里。
#
# 为什么不需要额外做"仅本地可调"限制：`_verify_gateway_key` 已经要求网关凭据，
# 而网关本身只听本机回环（桌面端形态）。


@router.get("/isolation/helper")
async def get_helper(request: Request) -> dict[str, Any]:
    """提权 helper 的当前状态。

    单列一个端点而不是塞进 ``GET /isolation``：``status()`` 会被高频调用
    （面板每次刷新、档位每次解析），而 helper 探测要发一次 HTTP ping，
    混进去会让本来常数时间的状态查询多一跳网络。
    """
    _verify_gateway_key(request)
    return await asyncio.to_thread(_helper_status_payload)


def _helper_status_payload() -> dict[str, Any]:
    from app.isolation import win_launch

    return win_launch.helper_status()


@router.post("/isolation/helper")
async def start_helper(request: Request) -> dict[str, Any]:
    """启动提权 helper（**会弹一次 UAC**），等它就绪后返回最新状态。

    失败不抛 500：UAC 被取消是用户的正常选择，不是服务器错误。回
    ``{ok: false, reason}`` 让前端能把"你取消了"与"起不来"分开说。

    启动成功后必须清探测缓存 —— 否则 ``selectable`` 里依旧没有 ``restricted``，
    用户会看到"授权成功了但档位还是灰的"。
    """
    _verify_gateway_key(request)

    from app.isolation import win_launch

    ok, reason, cancelled = await asyncio.to_thread(win_launch.helper_start)
    if ok:
        # 两份缓存（本包的 `_restricted_ok` 与 win_launch 的 `_smoke_ok`）一起清。
        iso.reset_probes()

    iso.record_level_change(
        action="helper_start",
        requested=iso.configured_level().value,
        effective=iso.resolve_level().effective.value,
        previous=None,
        actor=_actor(request),
        reason="" if ok else ("用户取消 UAC" if cancelled else f"失败：{reason}"),
    )
    return {
        "ok": ok,
        # `cancelled` 单独给一个字段而不是靠解析 `reason` 文本：前端要据此选
        # 「你取消了授权」还是「启用失败」两种完全不同的提示，而 `reason` 是
        # 人类可读字符串、措辞会随版本变化。
        "cancelled": cancelled,
        "reason": "" if ok else (reason or "提权助手启动失败"),
        "helper": await asyncio.to_thread(_helper_status_payload),
        **iso.status(),
    }


@router.delete("/isolation/helper")
async def stop_helper(request: Request) -> dict[str, Any]:
    """停用提权 helper（由它自己退出，引擎无权杀 High IL 进程）。

    幂等：本来没在跑也返回 ``ok: true``。
    """
    _verify_gateway_key(request)

    from app.isolation import win_launch

    ok, reason = await asyncio.to_thread(win_launch.helper_stop)
    iso.reset_probes()

    iso.record_level_change(
        action="helper_stop",
        requested=iso.configured_level().value,
        effective=iso.resolve_level().effective.value,
        previous=None,
        actor=_actor(request),
        reason="" if ok else f"失败：{reason}",
    )
    return {
        "ok": ok,
        "reason": "" if ok else (reason or "提权助手未能退出"),
        "helper": await asyncio.to_thread(_helper_status_payload),
        **iso.status(),
    }

