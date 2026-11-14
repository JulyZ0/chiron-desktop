"""use_capability — 单一能力代理工具（P7）。

对照实现：Reasonix `internal/runtime/usecap`（"the one fixed tool through which…"），
其测试**锁死该工具的 schema 字节不随 MCP 增删而变**。

## 为什么需要它

Chiron 已有 `tool_search`（按需激活），但它解决的是"**少暴露**"而非"**稳定暴露**"：
MCP / 插件工具仍是普通注册项，一旦被激活就进入请求体。于是同一份请求的工具清单会随
"这个用户连了几个 MCP、每个 MCP 有几个工具"而**漂移** —— 既贵（每个 schema 都占 token），
又不稳定（同名工具被不同服务器抢注册，上游 provider 看到的工具集每次不同）。

本工具把这条通道收成一个：**MCP / 插件（隐藏来源）不直接可见，只能经它访问**，
而它的参数 schema 是**固定的两个字面字段**，与后端连了什么 MCP 无关。

## 发现与调用

* 无参调用 ⇒ 返回**可用能力清单**（名字 + 描述），模型据此知道代理背后有什么；
* 带参调用 ⇒ 转发给对应能力。

内置工具**不**经它调用（它们本就在清单里，经代理只会多一层并掩盖来源）—— 直接拒绝。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from app.tools.registry import HIDDEN_SOURCES, SOURCE_BUILTIN, registry

logger = logging.getLogger(__name__)

USE_CAPABILITY_NAME = "use_capability"
#: ⚠️ **固定 schema** —— 这正是本工具的全部意义（对齐 Reasonix usecap 的
#: "schema 字节不随 MCP 增删而变"）。**不要**动态改它，尤其不要往 properties 里
#: 加入 MCP 的工具名 —— 那会让请求体重新随后端漂移，把这个问题原样带回来。
_CAPABILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capability": {
            "type": "string",
            "description": (
                "Name of the external capability to invoke. Omit to list what is available."
            ),
        },
        "arguments": {
            "type": "object",
            "description": "Arguments object for the capability (matches its parameters).",
        },
    },
    "required": ["capability"],
}


def capability_schema() -> dict[str, Any]:
    """返回代理工具参数的**独立副本**（供测试断言其不随 MCP 增删改变）。"""
    return copy.deepcopy(_CAPABILITY_SCHEMA)


def _available() -> list[str]:
    return [c["name"] for c in registry.list_capabilities()]


def _selected_plugins() -> list[str]:
    """本次对话选中的插件（MCP server 名）；空列表 = 不限制。

    这里**必须**读工具上下文而不是只看 registry：MCP / 插件工具是隐藏来源，不进
    模型可见的工具清单，因此 runtime 的 `_restrict_tools_to_plugins` 对它们无效
    （它筛的是可见清单）。代理层是唯一还能落实"用户挑了哪几个插件"的地方；
    它不读，用户在对话里挑插件就是个纯 UI 动作。
    """
    try:
        from app.tools.context import get_plugin_names

        return get_plugin_names()
    except ImportError:  # pragma: no cover — 上下文模块不可用时视为未选择
        return []


def _allowed(name: str, selected: list[str]) -> bool:
    """能力是否在当前对话的插件选择范围内。

    `selected` 为空 ⇒ 不限制（用户没挑 = 全都要）。非空时按 `{server}_{tool}`
    的命名前缀归属判定 —— 与 `_restrict_tools_to_plugins` 同一口径，避免两处
    对"这个工具属于哪个插件"给出不同答案。
    """
    if not selected:
        return True
    return any(name.startswith(f"{plugin}_") for plugin in selected)


async def use_capability(
    capability: str = "", arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """调用一个**隐藏来源**（MCP / 插件）的能力；无参时列出可用能力。"""
    name = (capability or "").strip()
    selected = _selected_plugins()
    if not name:
        # 只列当前对话选中的那几个：列出用户明确排除的插件，模型会去调用并撞墙。
        caps = [c for c in registry.list_capabilities() if _allowed(c["name"], selected)]
        out: dict[str, Any] = {
            "capabilities": caps,
            "count": len(caps),
            "note": (
                "Pass {'capability': '<name>', 'arguments': {...}} to invoke one. "
                "These are external integrations; built-in tools are called directly."
            ),
        }
        if selected:
            out["scoped_to_plugins"] = selected
            out["note"] += (
                f" This conversation is scoped to plugins: {', '.join(selected)}."
            )
        return out

    tool = registry.get(name)
    if tool is None:
        return {"error": f"unknown capability '{name}'", "available": _available()}
    if tool.source not in HIDDEN_SOURCES:
        # 内置工具本就在工具清单里：经代理调用只会多一层、还掩盖调用来源。
        return {
            "error": (
                f"'{name}' is a built-in tool; call it directly instead of via "
                f"{USE_CAPABILITY_NAME}"
            )
        }
    if not _allowed(name, selected):
        # 明确拒绝而不是静默执行：用户没选这个插件，调用它属于越出本次对话的授权范围。
        return {
            "error": (
                f"'{name}' belongs to a plugin that is not enabled for this conversation"
            ),
            "scoped_to_plugins": selected,
            "available": [
                c["name"] for c in registry.list_capabilities() if _allowed(c["name"], selected)
            ],
        }
    return await registry.execute(name, dict(arguments or {}))


registry.register(
    name=USE_CAPABILITY_NAME,
    description=(
        "Invoke an external capability (MCP server / plugin tool) by name. Call it with no "
        "arguments to list the available capabilities, then call it again with the capability "
        "name and an arguments object matching that capability's parameters. Built-in tools "
        "are called directly, not through this tool."
    ),
    parameters=_CAPABILITY_SCHEMA,
    handler=use_capability,
    source=SOURCE_BUILTIN,
)
