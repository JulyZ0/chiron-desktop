"""系统管理API - 日志级别热更新等"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/system", tags=["system"])

VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class LogLevelUpdate(BaseModel):
    """日志级别更新请求"""
    level: str


@router.post("/log-level")
async def update_log_level(update: LogLevelUpdate) -> dict[str, Any]:
    """运行时更新日志级别

    Args:
        update: 包含新日志级别的请求体

    Returns:
        更新结果

    Raises:
        HTTPException: 如果日志级别无效
    """
    level = update.level.upper()
    if level not in VALID_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid log level. Must be one of: {', '.join(sorted(VALID_LEVELS))}"
        )

    # 更新根logger和app logger的级别
    logging.getLogger().setLevel(getattr(logging, level))
    logging.getLogger("app").setLevel(getattr(logging, level))

    logger.info(f"Log level updated to {level}")

    return {
        "message": f"Log level updated to {level}",
        "level": level
    }


@router.get("/log-level")
async def get_current_log_level() -> dict[str, Any]:
    """获取当前日志级别

    Returns:
        当前日志级别信息
    """
    root_level = logging.getLevelName(logging.getLogger().getEffectiveLevel())
    app_level = logging.getLevelName(logging.getLogger("app").getEffectiveLevel())

    return {
        "root_level": root_level,
        "app_level": app_level
    }


@router.post("/price-sync")
async def trigger_price_sync() -> dict[str, Any]:
    """立即执行一轮模型价格同步（手动触发）。

    为什么需要它：后台循环是 30min 一轮，用户新增/更换模型后想立刻看到价格，
    不该干等到下一轮。这里直接复用 `sync_once()`，与定时任务**同一套**拉取与
    交叉验证逻辑，不存在"手动走另一条捷径"导致写入未经校验价格的风险。

    返回写入行数与分歧条数。`written == 0` 有多种原因（网络不可达、只有一个源
    应答、库未迁移），因此把分歧数一并回传，便于前端提示而不是假装成功。
    """
    from app.pricing.sync import sync_enabled, sync_once

    if not sync_enabled():
        return {"ok": False, "reason": "price sync disabled (MODEL_PRICE_SYNC=off)", "written": 0}

    written, disagreements = await sync_once()
    return {
        "ok": True,
        "written": written,
        "disagreements": len(disagreements),
        "detail": disagreements[:5],
    }
