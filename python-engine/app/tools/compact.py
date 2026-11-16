"""``compact_conversation`` 工具 —— 让模型在**任务中途**主动整理上下文。

为什么模型自己需要这个入口：自动压缩只在"快满了"时才触发（阈值 0.85）。但在
长任务里存在另一种时机 —— 一个阶段刚做完（例如"读代码"结束、准备"动手改"），
此时上下文里全是已经用不上的中间输出。人做这件事是自然的（合上草稿纸再开始写），
模型不行：它只能眼看着那些字节每轮重发，直到撞上阈值被强制折叠，那时丢什么已不由它决定。

对位实现：deepagents 的 ``SummarizationToolMiddleware`` 暴露的 ``compact_conversation``
（见 ``vendor/deepagents/libs/deepagents/deepagents/middleware/summarization.py``）——
它同样是一个"由模型决定何时整理"的工具，而不仅是自动阈值。

工具本身**不执行任何副作用**：真正的压缩在 ``agent/runtime.py`` 的工具循环里特判执行
（与 ``ask_user`` 同一模式），因为压缩要改的是 runtime 手里的那份 ``messages``，
而工具 handler 拿不到它。这里的存在只是让模型在工具列表里看见这个能力。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import registry

COMPACT_TOOL = "compact_conversation"


async def compact_conversation(reason: str = "") -> dict[str, Any]:
    """占位实现：正常路径由 runtime 拦截并就地压缩，不会走到这里。"""
    return {
        "error": (
            "compact_conversation is handled by the agent runtime, not called directly"
        )
    }


registry.register(
    name=COMPACT_TOOL,
    description=(
        "把较早的对话历史整理成摘要，腾出上下文空间。"
        "适合在一个阶段刚结束、准备进入下一阶段时主动调用"
        "（例如「读代码/排查」已得出结论、即将开始「改代码」）。"
        "已裁掉的原文仍可按引用召回，所以不必担心彻底丢失；"
        "不要在任务中途的普通步骤里频繁调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "为什么现在压缩（一句话，会记进摘要供后续参考）",
            },
        },
        "required": [],
    },
    handler=compact_conversation,
)
