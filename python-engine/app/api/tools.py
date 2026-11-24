"""Read-only tool catalog; execution stays inside the Agent runtime."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.tools.registry import registry

router = APIRouter(tags=["tools"])


@router.get("/v1/tools")
async def list_tools() -> dict[str, Any]:
    # 按工具上下文用户过滤（MCP 等用户级工具只对自己可见）
    tools = registry.to_openai_tools()
    return {"tools": tools}
