"""知识库索引服务地址：**运行时**解析与热切换。

## 为什么需要这一层

引擎侧只有一种 provider 实现（``RemoteProvider``），它把所有索引/检索请求发给一台
**pageindex 服务**（``knowledge_service_url``）。这个地址有两个现实来源：

* **本机内置**：桌面壳 ``electron/local-stack.cjs`` 先拉起本地 pageindex-service，
  再把端口拼成地址注入引擎的 env；
* **外部服务**：部署方自己起的 pageindex-service（另一台机器 / 集群内域名）。

历史上地址**只能在启动时确定**：想换一台服务就得改 env 并重启整个应用 —— 而
"重启"在桌面态意味着用户丢掉所有在途会话。本模块把它变成可随时切换的运行时状态。
``RemoteProvider._base_url()`` 每次请求都重读 ``settings.knowledge_service_url``，
所以写回 ``settings`` 即等效于"立刻生效"，不需要重建 provider。

## 两个必须记住的事实

1. **启动地址只有壳知道**。``bootstrap_url()`` 在 import 时快照一次 —— 切到外部服务
   之后要能切回"本地模式"还原出那个值；引擎自己没有能力重新发现它（端口是壳挑的）。
   不保留快照，用户切走一次就永久失去本地模式，只能重启应用。
2. **配置的权威在网关的 ``system_settings``**，不在引擎进程内。引擎启动时网关往往
   还没起来（桌面态先起引擎），所以 ``hydrate()`` 允许失败并回落到启动地址；等
   Redis 订阅连上后再补拉一次（见 ``listen_for_changes``），把"重启后配置漂移"
   收敛掉。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

#: 本机内置索引服务（地址由桌面壳 / 部署在启动时注入）。
MODE_LOCAL = "local"
#: 由后台指定的 pageindex 服务地址。
MODE_REMOTE = "remote"

MODES: tuple[str, ...] = (MODE_LOCAL, MODE_REMOTE)

#: 启动时的地址快照。**只写一次**，见模块文档第 1 条。
_BOOTSTRAP_URL = str(getattr(settings, "knowledge_service_url", "") or "").rstrip("/")

#: 当前应用的模式。初始按"是否注入了地址"推定为本地 —— 桌面态注入了本地服务端口，
#: 独立部署则可能什么都没给（此时 health 会明确报"未配置"）。
_current_mode = MODE_LOCAL


def normalize_mode(raw: object) -> str:
    """把外部输入归一成合法模式；不认识的一律回落到 ``local``。

    不抛异常是有意的：这是**展示/配置**通道，写错一个词不该让引擎拒收整条配置。
    调用方（``apply``）会据此判定"配置无效、保持现状"。
    """
    value = str(raw or "").strip().lower()
    if value in MODES:
        return value
    return MODE_LOCAL


def is_known_mode(raw: object) -> bool:
    return str(raw or "").strip().lower() in MODES


def bootstrap_url() -> str:
    """进程启动时注入的地址（不可变）：切回「本地模式」时还原用的唯一依据。"""
    return _BOOTSTRAP_URL


def current_url() -> str:
    """当前生效的索引服务地址（``RemoteProvider`` 唯一读取入口）。"""
    return str(getattr(settings, "knowledge_service_url", "") or "").rstrip("/")


def current_mode() -> str:
    return _current_mode


def _resolve(mode: str, service_url: str) -> str:
    """按模式算出应当生效的地址。

    ``local`` 永远还原到启动快照，**不接受**页面同时提交的地址 —— 否则"本地模式"
    会变成"本地模式但用你填的地址"，两个概念混成一个，用户再也说不清自己在用谁。
    """
    if mode == MODE_LOCAL:
        return _BOOTSTRAP_URL
    return service_url.strip().rstrip("/")


def apply(config: dict[str, Any] | None) -> bool:
    """按后台配置切换生效地址；返回是否发生了切换。

    无效配置（模式不认识、``remote`` 却没给地址）**保持现状**并记 warning ——
    一次坏配置不能把正在服务的知识库打掉，那会让"改配置"变成"断服务"。
    """
    global _current_mode

    if not config:
        return False
    raw_mode = config.get("mode")
    if not is_known_mode(raw_mode):
        logger.warning("knowledge runtime: unknown mode %r; keeping current", raw_mode)
        return False

    mode = normalize_mode(raw_mode)
    service_url = str(config.get("service_url") or "")
    if mode == MODE_REMOTE and not service_url.strip():
        logger.warning(
            "knowledge runtime: remote mode without service_url; keeping current url"
        )
        return False

    url = _resolve(mode, service_url)
    previous = current_url()
    _current_mode = mode
    settings.knowledge_service_url = url
    if url != previous:
        logger.info(
            "knowledge runtime: index service switched (%s -> %s) mode=%s",
            previous or "-",
            url or "-",
            mode,
        )
    return True


def state() -> dict[str, Any]:
    """当前事实（供后台页面显示，不让它靠猜）。"""
    url = current_url()
    return {
        "mode": _current_mode,
        "service_url": url,
        "bootstrap_url": _BOOTSTRAP_URL,
        # 生效地址就是启动注入的那个 ⇒ 本地模式；否则来自后台配置。
        # 页面据此区分"你以为在用本地、其实早已切走"。
        "using_bootstrap": bool(_BOOTSTRAP_URL) and url == _BOOTSTRAP_URL,
    }


async def hydrate() -> bool:
    """从网关补拉一次 ``knowledge`` 分类配置；返回是否成功应用。

    失败**不抛**：网关没起来 / 没配内部令牌 / DB 不可用都属正常启动阶段，
    回落到启动地址即可。等 Redis 订阅建立后会再试一次。
    """
    from app.config import settings as _settings

    if not str(getattr(_settings, "internal_token", "") or ""):
        return False

    import json
    import urllib.request

    base = str(getattr(_settings, "gateway_internal_url", "") or "").rstrip("/")
    if not base:
        return False
    url = f"{base}/v1/internal/knowledge-config"
    try:
        req = urllib.request.Request(
            url, headers={"X-Internal-Token": str(_settings.internal_token)}
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - 拉不到就用启动值，不阻断
        logger.debug("knowledge runtime hydrate failed: %s", exc)
        return False

    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not data:
        return False
    return apply(data)


#: ⚠️ 频道名必须与 Go 侧 `internal/api/settings_broadcast.go` 的
#: `settingsChangedChannel()` 逐字相同（两侧各自经 key 前缀函数加 ``REDIS_KEY_PREFIX``）。
SETTINGS_CHANGED_CHANNEL = "chiron:settings:changed"

#: 本模块只处理这个分类，其余与本监听无关。
KNOWLEDGE_CATEGORY = "knowledge"

#: 订阅断开后的重连退避（秒），与 media/reload.py、harness_store 同一范式。
RECONNECT_DELAY_SECONDS = 5.0


async def listen_for_changes() -> None:
    """订阅设置变更广播：``knowledge`` 分类到达即热切换索引服务地址。

    **先 hydrate 再订阅**：订阅只在 Redis 消息到达时生效，而引擎重启时「上一次保存
    的配置」不会有新消息 —— 不主动拉一次，重启就退回启动注入地址，表现为"我明明
    切到了外部服务，重启后自己变回本地"。两条路径的先后顺序不能反：先订阅后 hydrate
    会在两者之间丢一条消息。
    """
    import asyncio
    import json

    await hydrate()

    from app.redis_keys import rkey

    channel = rkey(SETTINGS_CHANGED_CHANNEL)
    while True:
        pubsub = None
        try:
            from app.redis_client import aclose_pubsub, get_redis

            redis = await get_redis()
            if redis is None:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            logger.info("knowledge settings listener subscribed: %s", channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                try:
                    payload = json.loads(raw if isinstance(raw, str) else str(raw))
                except Exception:
                    logger.warning("knowledge settings broadcast unmarshal failed")
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("category") != KNOWLEDGE_CATEGORY:
                    continue
                cfg = payload.get("config")
                apply(cfg if isinstance(cfg, dict) else None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 订阅断了要能自己回来
            logger.warning("knowledge settings listener error, retrying: %s", exc)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            if pubsub is not None:
                try:
                    await aclose_pubsub(pubsub)
                except Exception:  # noqa: BLE001 - 关连接失败不该盖住上面的异常
                    logger.debug("knowledge pubsub close failed", exc_info=True)

