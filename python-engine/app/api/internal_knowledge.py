"""知识库索引服务的**运行时事实**（内网端点，`X-Internal-Token` 保护）。

后台「知识库」页需要回答两个问题，而这两个问题都不该由页面猜：

1. **引擎此刻在打哪台服务？** 已保存的配置是"下次启动会用什么"，不等于"现在在用什么"：
   保存成功但广播丢了（Redis 断开 / 引擎未启动 / 消息过期）时，页面若按自己保存的值
   渲染，就会显示一个引擎从未采纳的地址 —— 这正是"改了没反应"最难自查的一类现象。
   这里直接回 ``app/knowledge/runtime.py`` 的 ``state()``，那是引擎的真实持有值。
2. **那台服务真的活着吗？** 连通性探测必须在**引擎侧**做：引擎才是真正的调用方，
   它的网络位置、内网出口、`INTERNAL_TOKEN` 都是判定的一部分。网关自己探一遍会得出
   与引擎不同的答案（典型差异：网关在宿主机、引擎在容器里）。

为什么 ``url`` 可选：页面要能在**保存之前**测一个候选地址（否则用户得先保存一个
可能连不上的地址、再把服务切过去）。传了 ``url`` 就只测它，**不改变任何生效状态**。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


async def _probe(url: str) -> dict[str, Any]:
    """探一次索引服务的 ``/healthz``；**不抛**（连不上就是一次业务结果）。"""
    import httpx

    try:
        async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
            resp = await client.get("/healthz")
        try:
            body = dict(resp.json() or {})
        except Exception:  # noqa: BLE001 - 非 JSON 也照样报状态码
            body = {}
        ok = resp.status_code < 400 and bool(body.get("ok", True))
        detail = str(body.get("detail") or "") if not ok else ""
        return {"ok": ok, "status_code": resp.status_code, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - 不可达属于业务结果，不是 500
        return {"ok": False, "status_code": 0, "detail": f"服务不可达: {exc}"}


@router.get("/v1/internal/knowledge-runtime")
async def internal_knowledge_runtime(
    url: str = Query("", description="可选：只测这个地址（不改变生效状态）"),
    probe: bool = Query(True, description="是否联网探测；页面刷新这类高频读取传 false"),
) -> dict[str, Any]:
    """当前生效的索引服务 + （可选）连通性探测结果。

    ``probe=false`` 是给"打开页面"用的：那一刻用户只是想知道引擎在打哪台服务，
    不该为此等一次最长 10 秒的网络请求 —— 而且探测失败会把页面渲染成"出错"，
    掩盖"配置本身是好的、只是那台机器暂时没起"这个区别。
    """
    from app.knowledge import runtime

    state = runtime.state()
    if not probe:
        return state

    target = (url or "").strip().rstrip("/") or state["service_url"]
    if target:
        state["probe"] = await _probe(target)
    else:
        # 没配地址：明确说"未配置"，而不是给一个看起来失败的探测结果。
        state["probe"] = {
            "ok": False,
            "status_code": 0,
            "detail": "未配置索引服务地址",
        }
    return state
