"""结构化提问工具：让模型向用户要一个明确的回答。

工具本身不执行任何副作用：它在 runtime 里被特判（见 ``agent/runtime.py`` 的 ask 分支），
真正执行时会向前端发 ``ask`` 事件并等待 ``/v1/agent/answer`` 回填答案。
因此这里的 handler 只是**占位**——它的存在是为了让模型在工具列表里看到这个能力；
若它被直接调用（例如未接入 runtime 的旧路径），返回明确错误而不是静默成功。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry

ASK_USER_TOOL = "ask_user"


async def ask_user(
    question: str, options: list[str] | None = None
) -> dict[str, Any]:
    """占位实现：正常路径由 runtime 拦截并等待用户回答，不会走到这里。"""
    return {"error": "ask_user is handled by the agent runtime, not called directly"}


registry.register(
    name=ASK_USER_TOOL,
    description=(
        "向用户提出一个问题并等待其明确回答（可选答案见 options）。"
        "仅在确实需要用户决策、且无法从上下文推断时使用；"
        "已能自行判断的事情不要用它打断用户。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题，一句话说清"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "建议答案列表；省略则用户自由输入",
            },
        },
        "required": ["question"],
    },
    handler=ask_user,
)
