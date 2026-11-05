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

    ``level`` 只接受四个档位字面值。低于硬底线的值**允许传**（不在这里拒绝），
    因为 ``resolve_level`` 会把它抬到底线并在 ``reasons`` 里说明 —— 拒绝会让前端
    无法展示"你请求了 none，实际跑 low_il"这条重要信息。
    """

    level: str = Field(..., description="none | job | low_il | appcontainer")
    reason: str = Field("", max_length=500, description="为什么改，写进审计")


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

    iso.set_runtime_level(level)
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
