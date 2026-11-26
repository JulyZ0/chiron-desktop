"""隔离档位 API —— 查询、切换、撤销（内部端点，共享密钥校验）。

六个端点：

- ``GET    /v1/system/isolation``         当前档位：请求值 / 实际值 / 能力矩阵 / 可选档位
- ``PUT    /v1/system/isolation``         切换档位（**只在进程内生效，不写 .env**）
- ``DELETE /v1/system/isolation``         撤销运行时覆盖，回到配置文件的值
- ``POST   /v1/system/isolation/revoke``  撤销**磁盘产物**（AppContainer profile、目录标签）
- ``GET    /v1/system/isolation/helper``  提权助手状态（``restricted`` 档的执行前置）
- ``POST   /v1/system/isolation/helper``  启用提权助手（**弹一次 UAC**）
- ``DELETE /v1/system/isolation/helper``  停用提权助手

## 关于 `/isolation/helper` 的三条

网关（`internal/api/routes_proxy.go`）**必须**同时注册这三条。缺 GET，前端
``helperStatus`` 恒为 null，界面既不提示"助手挡住了 restricted"，也不会出现
「启用助手」按钮；缺 POST，点了只会拿到前端兜底文案
（真机事故：界面显示「启用助手失败：本机无法启用此档位」，而那不是引擎说的
任何一句话，是网关 404 被前端兜底文案盖住了）；缺 DELETE，用户无法撤销这条
提权通道。

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

    ``status()`` 里的可用性判定是**端到端冒烟**（真的起一个子进程），首次调用
    可能要数秒（`restricted` 走 helper 时要多一跳 HTTP）。所以它必须放到线程里跑：
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
    用户启用提权助手或安全软件放行之后，旧缓存仍可能把 `restricted` 标成不可用 ——
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
    # 受限令牌：探活缓存可能是"助手启动前"的负结果（引擎启动门禁先探过一次）。
    # 用户点切档时若助手现在可用，必须重探一次再判定 —— 否则会把一次成功切换
    # 报成 requested=restricted / effective=low_il。
    if level is iso.Level.RESTRICTED:
        from app.isolation import win_launch

        helper_ok, _helper_reason = win_launch.helper_available()
        if helper_ok:
            iso.reset_probes()
    state = iso.status()
    decision = iso.resolve_level()

    iso.record_level_change(
        action="set",
        requested=decision.requested.value,
        effective=decision.effective.value,
        previous=previous.value if previous is not None else None,
        actor=actor,
        # 用户没写理由时，若档位被降级，就把**机器原因**写进审计。
        # 否则这条流水只剩 "requested=restricted effective=low_il"，事后无解。
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


# ── 提权 helper（受限令牌的执行前置） ─────────────────────────────
#
# 为什么这必须是一个**用户显式触发**的端点：它弹一次 UAC，而用户同意的是
# "提权执行通道"。引擎自己在启动时静默弹窗是不可接受的 —— 用户不知道自己在
# 同意什么。所以这条路只能由设置页的按钮走到这里。
#
# 为什么不需要额外做"仅本地可调"限制：`_verify_gateway_key` 已经要求网关凭据，
# 而网关本身只听本机回环（桌面端形态）。

#: 启用助手失败时的**兜底**原因。
#:
#: 必须是一句能让用户动手的话，而不是"本机无法启用此档位"那种把三种事故
#: （用户取消 / 真的起不来 / 网关不通）混成一句的空话 —— 那句话此前还会因为
#: 网关缺路由而在**任何**机器上稳定出现。
HELPER_START_FALLBACK_REASON = "未能启动提权助手（原因未上报）"

#: 与失败原因并列给前端的**可执行指引**，前端单独渲染，不与 reason 混排。
#:
#: 放在后端而不是前端文案里：这条指引与"实际要发生什么"强绑定（弹一次 UAC、
#: 用哪个账户批准），改动它们的是引擎侧的启动逻辑。
HELPER_START_HINT = (
    "启用「受限令牌」档需要在系统授权（UAC）窗口点一次「是」。"
    "若窗口没有出现，请检查安全软件是否拦住了提权进程；"
    "若你用非管理员账户登录，请在窗口里输入任意管理员账户的凭据。"
)


def _degrade_audit_reason(state: dict[str, Any]) -> str:
    """档位没生效时，给审计流水一条**非空**的机器原因。

    ## 为什么必须补这一条

    真机审计里出现过这样的行：``action=helper_start requested=restricted
    effective=low_il reason=""`` —— 助手启用成功、档位却仍在 `low_il`，而
    `reason` 为空，事后只剩"看起来正常"这一条线索。降级本身是允许的
    （fail-closed 执行仍然成立），但**静默**降级会让用户与运维都无从下手。

    原因直接取自 `status()["unavailable_reasons"][requested]`：那里已经有
    "缺哪个特权 / 哪个 API 名"这一层细节（如 `CreateProcessAsUserW 失败 err=5`
    或 `无法启用 … SeIncreaseQuotaPrivilege`），不必在这里重新拼一遍。
    """
    requested = str(state.get("requested") or "")
    effective = str(state.get("effective") or "")
    if not requested or not effective or requested == effective:
        return ""
    reasons = state.get("unavailable_reasons") or {}
    detail = str(reasons.get(requested) or "").strip()
    head = f"档位未生效：{requested} → {effective}"
    return f"{head}；原因：{detail}" if detail else head


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

    # **一次**探测同时供三处使用：返回给前端的档位状态、审计里的 effective、
    # 以及 helper 最新状态。探测是端到端冒烟（真起一个受限进程），重复探测
    # 既慢又可能给出前后不一致的结论（"界面说可用、审计说降级"）。
    state = await asyncio.to_thread(_status_payload)

    # 审计的 `reason` 三态互斥：
    #   * 成功但档位没生效（降级）⇒ 写机器原因，别让审计出现 reason=""；
    #   * 用户取消 UAC ⇒ 如实记，不需要排障；
    #   * 真的失败 ⇒ 带 `reason`，缺省也要有兜底文案。
    if ok:
        audit_reason = _degrade_audit_reason(state)
    elif cancelled:
        audit_reason = "用户取消 UAC"
    else:
        audit_reason = f"失败：{reason or HELPER_START_FALLBACK_REASON}"

    iso.record_level_change(
        action="helper_start",
        requested=state["requested"],
        effective=state["effective"],
        previous=None,
        actor=_actor(request),
        reason=audit_reason,
    )
    return {
        "ok": ok,
        # `cancelled` 单独给一个字段而不是靠解析 `reason` 文本：前端要据此选
        # 「你取消了授权」还是「启用失败」两种完全不同的提示，而 `reason` 是
        # 人类可读字符串、措辞会随版本变化。
        "cancelled": cancelled,
        "reason": "" if ok else (reason or HELPER_START_FALLBACK_REASON),
        "hint": "" if ok else HELPER_START_HINT,
        "helper": await asyncio.to_thread(_helper_status_payload),
        **state,
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

    state = await asyncio.to_thread(_status_payload)
    iso.record_level_change(
        action="helper_stop",
        requested=state["requested"],
        effective=state["effective"],
        previous=None,
        actor=_actor(request),
        reason="" if ok else f"失败：{reason}",
    )
    return {
        "ok": ok,
        "reason": "" if ok else (reason or "提权助手未能退出"),
        "helper": await asyncio.to_thread(_helper_status_payload),
        **state,
    }

