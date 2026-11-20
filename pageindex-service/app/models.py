"""检索/索引用的**模型来源**解析（env → 网关 → 无）。

## 为什么需要它

pageindex-service 自身的 ``Config`` 只从环境变量读 ``KNOWLEDGE_SELFHOSTED_CHAT_MODEL`` /
``_INDEX_MODEL``。桌面单机与「本机直跑」两种形态下，这两项经常是空的 —— 而用户
**明明配过模型**：权威来源是网关 PG 的 ``llm_models``（管理员在「模型服务」页添加，
key 在 ``llm_provider_keys``，端点覆盖在 ``system_settings``）。两条来源互不相通，
表现就是实测过的那一幕：

    服务起来了、端口也通 → /healthz 回 ``status=disabled``
    → 引擎「测试连接」显示"未配置自托管模型，也未配置 Cloud 问答模型"
    → 而 /models 页里有一堆可用模型。

## 解析顺序

1. **显式 env**（``KNOWLEDGE_SELFHOSTED_*``）：管理员在「知识库」页钉死的值，最高优先；
2. **网关** ``GET /v1/internal/knowledge-model``：唯一权威来源（含 provider 端点的
   目录默认值、key 的解密）—— 这里只消费、不复制那套目录，否则两边迟早漂移；
3. 无：``reason`` 给出**可操作**说明（"去 /models 配置"），而不是一句"未配置"。

## 为什么是**惰性 + 不缓存失败**

Electron 的启动顺序是「检索服务 → 引擎 → 网关」（引擎的 ``KNOWLEDGE_SERVICE_URL``
只能在 spawn 时给全，所以检索服务必须先起），首次 ``/healthz`` 时网关可能还没就绪。
因此解析放在**首次真正需要模型时**，失败只做**短时**负缓存（几秒），
网关就绪后的下一次探测立刻恢复 —— 而不是把一个启动期的瞬时状态固化成"永久不可用"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 网关未就绪时的负缓存时长（秒）。够短：网关通常几秒内起来，不值得每次 healthz 都打它；
#: 也够长：引擎的 probe 是并发的，不该把未就绪放大成请求风暴。
NEGATIVE_TTL_SECONDS = 5.0
#: 正向缓存时长（秒）：模型配置是低频变更，但没有重启就很难感知变更，所以不给太长。
POSITIVE_TTL_SECONDS = 300.0
#: 取模型配置的 HTTP 超时（秒）：这是**内网回环**调用，超过这个数说明网关有问题。
FETCH_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class ResolvedModels:
    """一次解析的结果（不可变，可安全跨协程共享）。"""

    chat_model: str = ""
    index_model: str = ""
    base_url: str = ""
    api_key: str = field(default="", repr=False)  # 明文不进 repr/日志
    #: env | gateway | none —— 排障时先看这个：它决定"该改哪一处"
    source: str = "none"
    #: source=none 时的可操作原因（中文，会显示在「测试连接」上）
    reason: str = ""

    def ready(self) -> bool:
        return bool(self.chat_model and self.index_model)

    def llm_backend(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.api_key:
            out["api_key"] = self.api_key
        if self.base_url:
            out["base_url"] = self.base_url
        return out


def from_env(cfg: Any) -> ResolvedModels:
    """只读 env 的解析（同步、无 I/O）。

    ``selfhosted_ready()`` 要求 index/chat 两个都给；这里按同样口径判定，
    但**只填一半也给出来** —— 半配置的现场比"完全没配"更难查，原因里要说清楚缺哪个。
    """
    chat = str(getattr(cfg, "selfhosted_chat_model", "") or "").strip()
    index = str(getattr(cfg, "selfhosted_index_model", "") or "").strip()
    base_url = str(getattr(cfg, "selfhosted_llm_base_url", "") or "").strip()
    api_key = str(getattr(cfg, "selfhosted_llm_api_key", "") or "").strip()
    if chat and index:
        return ResolvedModels(
            chat_model=chat, index_model=index, base_url=base_url, api_key=api_key, source="env"
        )
    if chat or index:
        missing = "INDEX_MODEL" if chat else "CHAT_MODEL"
        return ResolvedModels(
            chat_model=chat,
            index_model=index,
            base_url=base_url,
            api_key=api_key,
            source="none",
            reason=f"KNOWLEDGE_SELFHOSTED_* 只配了一半（缺 {missing}）",
        )
    return ResolvedModels()


def _gateway_base_url() -> str:
    import os

    return (os.getenv("GATEWAY_INTERNAL_URL") or "").strip().rstrip("/")


def _fetch_from_gateway(cfg: Any) -> ResolvedModels:
    """同步取网关的模型配置（在 to_thread 里跑，避免阻塞事件循环）。"""
    base = _gateway_base_url()
    if not base:
        return ResolvedModels(
            reason="网关地址未注入（GATEWAY_INTERNAL_URL 为空），无法从 /models 读取模型配置"
        )
    token = str(getattr(cfg, "internal_token", "") or "").strip()
    if not token:
        return ResolvedModels(reason="INTERNAL_TOKEN 未配置，无法向网关索取模型配置")
    req = urllib.request.Request(
        f"{base}/v1/internal/knowledge-model",
        headers={"X-Internal-Token": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return ResolvedModels(reason=f"网关返回 HTTP {exc.code}（{base}/v1/internal/knowledge-model）")
    except Exception as exc:  # noqa: BLE001 — 不可达 = 还没起来，不是配置错误
        return ResolvedModels(reason=f"网关不可达（{base}）：{exc}")

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return ResolvedModels(reason="网关返回的不是合法 JSON")
    # 网关包了一层 {code,data}（见 Go 的 OK 辅助函数）；也兼容裸对象。
    payload = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if not isinstance(payload, dict):
        return ResolvedModels(reason="网关返回结构无法识别")
    if not payload.get("available"):
        return ResolvedModels(reason=str(payload.get("reason") or "网关侧没有可用模型"))
    chat = str(payload.get("chat_model") or "").strip()
    if not chat:
        return ResolvedModels(reason="网关返回的 chat_model 为空")
    return ResolvedModels(
        chat_model=chat,
        # 索引模型与问答模型同源（单机只有一份模型配置）：网关只给一个名字时两边都用它。
        index_model=str(payload.get("index_model") or chat).strip(),
        base_url=str(payload.get("base_url") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
        source="gateway",
    )


_state: dict[str, Any] = {"models": None, "at": 0.0}
_lock: asyncio.Lock | None = None


def _lock_of() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def reset_cache() -> None:
    """清缓存（测试与"管理员刚改了模型"的显式刷新用）。"""
    _state["models"] = None
    _state["at"] = 0.0


async def resolve(cfg: Any, *, force: bool = False) -> ResolvedModels:
    """按 env → 网关 的顺序解析，并做（正/负）缓存。

    env 每次都重算：它是进程内可变的（运营可能临时 export 一下），成本是几次字符串比较。
    """
    env_models = from_env(cfg)
    if env_models.ready():
        return env_models

    now = time.monotonic()
    cached: ResolvedModels | None = _state["models"]
    ttl = POSITIVE_TTL_SECONDS if (cached is not None and cached.ready()) else NEGATIVE_TTL_SECONDS
    if cached is not None and not force and (now - float(_state["at"])) < ttl:
        return cached

    async with _lock_of():
        cached = _state["models"]
        if cached is not None and not force and (time.monotonic() - float(_state["at"])) < ttl:
            return cached
        fetched = await asyncio.to_thread(_fetch_from_gateway, cfg)
        if not fetched.ready() and env_models.reason:
            # env 半配置的原因比"网关也没给"更贴近用户的改动，优先展示
            fetched = ResolvedModels(reason=env_models.reason)
        _state["models"] = fetched
        _state["at"] = time.monotonic()
        if not fetched.ready():
            logger.warning("knowledge models unresolved: %s", fetched.reason)
        else:
            logger.info(
                "knowledge models resolved (source=%s provider-model=%s)", fetched.source, fetched.chat_model
            )
        return fetched


def effective(cfg: Any) -> ResolvedModels:
    """当前生效的模型（**不发网络请求**）：给同步构造路径（如 Adapter）用。"""
    cached: ResolvedModels | None = _state["models"]
    if cached is not None and cached.ready():
        return cached
    env_models = from_env(cfg)
    if env_models.ready():
        return env_models
    return cached or env_models


def unavailable_detail(models: ResolvedModels) -> str:
    """把"没有可用模型"翻译成**可操作**的中文说明。"""
    base = "这台机器还没有可用模型，请先在 /models 配置（模型服务 → 添加服务商与模型）"
    if models.reason:
        return f"{base}；当前原因：{models.reason}"
    return base
