"""只读诊断（内网端点，`X-Internal-Token` 保护）。

后台「自诊断」页要把「配了但不生效」聚合成一份清单，其中一项是**必需表在不在**。
这份清单的权威定义在引擎侧（``app/db.py::REQUIRED_TABLES``），启动自检
（``ensure_tables``）用的就是它；网关**不复制**这份清单 —— 复制的下场是两边
漂移：迁移改了、引擎改了，而网关那副本还是旧的，页面于是对着一张"表都在"的
报告排查一个真实的 ``relation does not exist``。

所以这里把启动自检的结论单独开一个只读端点回给网关：

- ``GET /internal/diagnostics/tables``

三件事刻意分开说清：

1. **只读**：只查 ``information_schema``，不建表、不跑迁移。建表唯一权威是
   Alembic（``migrations/versions/``）。
2. **不复用 ``ensure_tables()``**：那个函数只回一个 bool，日志里有缺失表名但
   端点拿不到 —— 页面就只能说"缺表"而不能说"缺哪几张"。
3. **DB 连不上不是 500**：``checked=false`` + ``detail`` 表达"没法判定"。
   ""表都不在"与"看不见"在排障时结论相反，用 5xx 表达会让页面把前者读成后者。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require_internal_token
from app.db import REQUIRED_TABLES, get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


@router.get(
    "/internal/diagnostics/tables",
    dependencies=[Depends(require_internal_token)],
)
async def diagnostics_tables() -> dict[str, Any]:
    """必需表存在性（与引擎启动自检同源清单，只读 information_schema）。"""
    required = list(REQUIRED_TABLES)
    try:
        pool = get_pool()
        rows = await pool.fetch(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
            """
        )
        existing = {str(row["table_name"]) for row in rows}
    except Exception as exc:  # noqa: BLE001 - 数据库不可达属于"无法判定"，不是 500
        logger.warning("diagnostics: read information_schema failed: %s", exc)
        return {
            "checked": False,
            "required_count": len(required),
            "present": [],
            "missing": [],
            "missing_count": 0,
            "detail": f"无法读取 information_schema: {exc}",
        }

    missing = [t for t in required if t not in existing]
    return {
        "checked": True,
        "required_count": len(required),
        "present_count": len(required) - len(missing),
        "missing": missing,
        "missing_count": len(missing),
        "detail": "" if not missing else "缺少必需表，请执行 alembic upgrade head",
    }
