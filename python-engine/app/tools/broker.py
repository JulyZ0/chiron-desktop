"""Tool Broker 客户端 —— 危险工具（delete / external）的**服务端授权**。

引擎侧的 ToolGuard 与工具执行跑在处理不可信内容的**同一进程**里：判定与执行同处一个信任域。
所以危险工具在真正执行前，必须向服务端的 Broker 要一次授权 —— 服务端按**自己的**策略
（`internal/api/tool_policy.go`）判定级别、决定要不要人工确认与二次校验，并留下审计。

**服务端的判定是权威的**：它可以收紧引擎上报的模式（例如会话选了 yolo，服务端仍要求
"不可逆操作必须确认"），引擎必须照办。

**fail-closed**：Broker 不可用时**拒绝执行**危险工具。这一层存在的意义就是"判定不依赖引擎
自述" —— 它挂掉时按引擎自己的判定放行，等于把这一层抹掉。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单次授权请求的超时（秒）。危险工具执行前的同步等待，不宜长。
AUTHORIZE_TIMEOUT_SECONDS = 5.0


async def authorize(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    tenant_id: int,
    user_id: str,
    session_id: str,
    tool_call_id: str,
    tools_mode: str,
) -> dict[str, Any]:
    """向服务端 Broker 请求授权。

    返回服务端的判定：``{allowed, level, requires_user_approval, requires_second_check,
    rollback, enforced, reason}``。

    **不吞异常**：网络失败、非 200、载荷异常一律抛出，由调用方 fail-closed（拒绝执行）。
    """
    from app.config import settings

    base = (settings.gateway_internal_url or "").rstrip("/")
    token = settings.internal_token or ""
    if not base or not token:
        raise RuntimeError("tool broker not configured (gateway_internal_url / internal_token)")

    import aiohttp

    payload = {
        # **必须是字符串**：Go 侧 `toolAuthorizeRequest.TenantID` 是 `string`（与
        # `internal/api/tenant.go: tenantStr` 的仓内契约一致）。发 JSON number 会让
        # `json.Decode` 直接失败 ⇒ 400 ⇒ 引擎 fail-closed 拒绝执行所有危险工具，
        # 报错还只是"HTTP 400"，看不出是类型问题。
        "tenant_id": str(tenant_id),
        "user_id": user_id,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "arguments": args or {},
        "tools_mode": tools_mode,
    }
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{base}/v1/internal/tool-authorize",
            json=payload,
            headers={"X-Internal-Token": token},
            timeout=aiohttp.ClientTimeout(total=AUTHORIZE_TIMEOUT_SECONDS),
        ) as resp:
            if resp.status != 200:
                # 带上响应体：只报 "HTTP 400" 时运维无从下手（这次就是类型不匹配，
                # 而报错里一个字段名都没有）。
                detail = (await resp.text())[:500]
                raise RuntimeError(f"tool broker HTTP {resp.status}: {detail}".rstrip(": "))
            data = await resp.json()

    # 网关的统一外壳 {"success": true, "data": {...}}
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict) or "allowed" not in body:
        raise RuntimeError("tool broker returned an unexpected payload")
    return body
