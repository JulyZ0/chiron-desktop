"""外部市场源的**唯一出网口**（内网端点，`X-Internal-Token` 保护）。

网关（Go）不直接访问第三方 URL：它把 `url` 交给本端点，由 Python 侧用
`app.tools.ssrf` 逐跳校验后抓取，并施加**流式体积上限**。

为什么要绕这一道：本仓纪律是"所有出站走 `fetch_url_safe`"（9 个既有调用点都在
Python 侧）。在 Go 里另写一套 IP/端口/重定向校验必然与之漂移 —— 两处策略一旦不同，
绕过就是"选更宽松的那一处"。把出网口收敛到这里，SSRF 面只剩一处。

安全约束（逐条对应实现）：

* **只接受 https**：市场源是第三方内容，明文 HTTP 无法保证内容未被中间人改写；
* `max_bytes` 由服务端**夹取**（硬上限 1 MiB），不接受调用方放大；
* **逐跳**重定向复检（防 `302 → 内网 / 云元数据`）；
* 体积边收边计，越限**立即中止**并关闭响应（不把整个响应读进内存）。

响应恒为 200 + `ok` 字段：域名错误（不可达/被拦/超体积）属于**业务结果**，
网关据此映射成 `source_unreachable` / `source_timeout` 等稳定原因码；
用 4xx/5xx 表达会让网关只能从错误串里猜原因。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])

#: 单个文档的体积硬上限（1 MiB）。
#:
#: 市场目录与 manifest 都是**小 JSON**；1 MiB 远超正常需要，同时把"一个源
#: 拖垮网关内存"的成本定死。调用方只能要得更小，不能要得更大。
MAX_DOCUMENT_BYTES = 1_048_576

#: 重定向跳数上限（与 `fetch_url_safe` 保持一致）。
_MAX_REDIRECTS = 5

_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class MarketFetchRequest(BaseModel):
    """抓取请求。`url` 完全由网关控制，但仍按不可信输入处理。"""

    url: str
    max_bytes: int = MAX_DOCUMENT_BYTES


def _classify(exc: Exception) -> str:
    """把异常映射成**稳定原因码**（网关据此回 `source_*`，细节进日志）。

    不直接把异常串当 code：`dial tcp 10.0.0.5:443: connect: connection refused`
    既不稳定，也会把内网拓扑漏到前端。
    """
    if isinstance(exc, (ValueError, httpx.InvalidURL)):
        msg = str(exc).lower()
        if "blocked scheme" in msg:
            return "insecure_scheme"
        if "cannot resolve" in msg:
            return "source_unreachable"
        if "blocked" in msg:
            return "blocked_url"
        if "too many redirects" in msg:
            return "too_many_redirects"
        return "invalid_url"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "source_timeout"
    if isinstance(exc, PayloadTooLargeError):
        return "payload_too_large"
    if isinstance(exc, httpx.HTTPStatusError):
        return "bad_status"
    return "source_unreachable"


class PayloadTooLargeError(RuntimeError):
    """响应在**读取过程中**越过体积上限。"""


def _require_https(url: str) -> None:
    """市场源必须 https。

    唯一的例外留给显式放行的内网主机（`SSRF_ALLOW_PRIVATE_HOSTS`）——
    那是"自建 registry"的部署形态，由运维显式声明，不构成默认放宽。
    """
    import os
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    host = (parsed.hostname or "").lower()
    allowed = {
        h.strip().lower()
        for h in os.getenv("SSRF_ALLOW_PRIVATE_HOSTS", "").split(",")
        if h.strip()
    }
    if parsed.scheme == "http" and host and host in allowed:
        return
    raise ValueError(f"blocked scheme: {parsed.scheme} (market sources must use https)")


async def _fetch_document(url: str, max_bytes: int) -> tuple[bytes, str]:
    """抓取一个市场文档，返回 `(原始字节, content-type)`。

    Raises:
        ValueError: URL 被 SSRF 策略拒绝（scheme/端口/内网地址）。
        PayloadTooLargeError: 读取中越过体积上限。
        httpx.HTTPStatusError / httpx.TimeoutException: 传输层失败。
    """
    from app.config import settings
    from app.http_client import shared_client
    from app.tools.ssrf import assert_safe_url

    _require_https(url)

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        # 逐跳复检：攻击者可用公开 URL 302 到内网，只在首跳校验形同无校验。
        assert_safe_url(current)
        async with shared_client(timeout=settings.http_timeout_default) as client:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.status_code in _REDIRECT_STATUS:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("redirect without location header")
                    current = str(httpx.URL(current).join(location))
                    continue
                resp.raise_for_status()
                # content-length 只当作"提前拒绝"的提示，真正的判据是下面的流式计数：
                # 声明值与实际字节都由对方控制，二者都可能撒谎。
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PayloadTooLargeError(
                        f"declared size {declared} exceeds limit {max_bytes}"
                    )
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        # 抛出即退出 `async with`：响应被关闭，不再继续拉取剩余字节。
                        raise PayloadTooLargeError(f"payload exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks), resp.headers.get("content-type", "")
    raise ValueError(f"too many redirects: {url}")


@router.post("/v1/internal/market/fetch")
async def internal_market_fetch(body: MarketFetchRequest) -> dict[str, Any]:
    """抓取外部市场文档（目录或 manifest）。"""
    url = (body.url or "").strip()
    if not url:
        return {"ok": False, "error": "invalid_url", "detail": "url is empty"}

    # 夹取：调用方只能要得更小。
    try:
        requested = int(body.max_bytes or MAX_DOCUMENT_BYTES)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_url", "detail": "max_bytes must be an integer"}
    max_bytes = max(1, min(requested, MAX_DOCUMENT_BYTES))

    try:
        raw, content_type = await _fetch_document(url, max_bytes)
    except Exception as exc:  # noqa: BLE001 — 失败原因分类后回给网关，不抛栈
        code = _classify(exc)
        level = logging.WARNING if code != "blocked_url" else logging.INFO
        logger.log(level, "market fetch failed (%s): %s", code, str(exc)[:200])
        return {
            "ok": False,
            "error": code,
            "detail": str(exc)[:300],
            "max_bytes": max_bytes,
        }

    return {
        "ok": True,
        # base64：文档是文本，但"文本"由对方声明，按二进制回传才不会在
        # 编码边界上出错（网关按字节做 sha256 与签名校验）。
        "body": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_type": content_type,
        "bytes": len(raw),
    }
