"""记忆链路的隐私闸门（H44）。

`history_writer` 早在 no_retention 下就不落库了，但记忆链路（条目/摘要/画像/冲突
审计）没有这道判断 —— 隐私模式下说过的内容仍会经蒸馏写进 PG，等于绕开用户自己开的
开关。这里把判断收成一个入口，各写入点统一调用，避免"某条新加的写路径忘了看"。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def memory_writes_allowed() -> bool:
    """当前请求是否允许写记忆存储。

    判定完全委托给隐私中间件（`is_no_retention`），所以：
      · 没有请求上下文（脚本 / 后台任务 / 单测）⇒ 正常模式，允许写；
      · 请求头带 `X-Privacy-Mode: no_retention` ⇒ **拒绝**。

    刻意不做"读不到上下文就当隐私模式"的保守翻转：那会让所有后台任务（补算、
    蒸馏定时任务）静默停止工作，而它们本来就在处理**已获准留存**的数据。
    """
    try:
        from app.middleware.privacy_middleware import is_no_retention
    except Exception:  # noqa: BLE001 - 中间件不可用时按正常模式（与 history_writer 一致）
        return True
    if is_no_retention():
        logger.debug("memory: skip persist under no_retention")
        return False
    return True
