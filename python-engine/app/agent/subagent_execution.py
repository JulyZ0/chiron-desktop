"""H38（切片1）：子 Agent 的工具执行复用主 Agent 的完整执行链。

缺陷形态：生产子 Agent 在 `multi_agent.py::_execute_tool` 里直接
`await registry.execute(name, args)` —— **绕过** ToolGuard、服务端授权（Tool Broker）、
审批与副作用账本（undo / side-effect ledger）。后果不是"少一层日志"，而是：

  · 主 Agent 里"删文件要先确认"的规则，在子 Agent 里**不生效**；
  · 危险工具在主 Agent 走人手确认、在子 Agent 直接执行；
  · 子 Agent 产生的副作用不进账本 ⇒ 那一轮的整体回滚会漏掉这些文件。

修法：把执行入口收成一个可注入的函数。子 Agent 默认仍走注册表（测试/独立使用不受
影响），但**生产调度器会注入走 guard 链的执行器**；后台子 Agent 无法交互，所以
"需要确认"降级为**显式拒绝**（而不是静默放行，也不是把审批事件丢掉）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class _SubAgentTask:
    """`guarded_execute_tool` 只需要从 task 上读 tenant/user 两个字段。"""

    __slots__ = ("tenant_id", "user_id")

    def __init__(self, tenant_id: int = 0, user_id: str = "") -> None:
        self.tenant_id = int(tenant_id or 0)
        self.user_id = str(user_id or "")


def make_guarded_subagent_executor(
    runtime: Any,
    *,
    tenant_id: int = 0,
    user_id: str = "",
) -> ToolExecutor:
    """构造"走 guard 链"的子 Agent 执行器。

    与主 Agent 的差别只有一处：`allow_approval=False`。后台子 Agent 没有人盯着，
    "需要确认"必须变成**明确失败**，让模型知道这条路走不通（它会换一种做法或回报
    给上层），而不是让它以为执行成功了。
    """

    async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
        from app.agent.tool_execution import guarded_execute_tool

        name = str(tool_call.get("name", "") or "")
        result, approval = await guarded_execute_tool(
            runtime,
            {"name": name, "arguments": tool_call.get("arguments", "{}")},
            _SubAgentTask(tenant_id, user_id),
            allow_approval=False,
        )
        if approval is not None:
            # 理论上 allow_approval=False 不会产出审批事件；真出现也不能吞掉。
            logger.warning("subagent tool %s needs approval in a background run; denied", name)
            return {
                "tool": name,
                "error": (
                    f"Tool '{name}' requires user confirmation, but a sub-agent runs in the "
                    "background and cannot ask. Not executed — report back instead."
                ),
                "denied": True,
            }
        if not isinstance(result, dict):
            return {"tool": name, "error": "tool returned no result", "denied": True}
        return {**result, "tool": name}

    return execute


def default_executor() -> ToolExecutor:
    """默认执行器：直接走注册表（保持子 Agent 可独立使用/可测）。"""

    async def execute(tool_call: dict[str, Any]) -> dict[str, Any]:
        from app.tools.registry import registry

        name = str(tool_call.get("name", "") or "")
        return {"tool": name, **await registry.execute(name, tool_call.get("arguments") or {})}

    return execute
