"""本地工具注册表（Python 端）。

职责：
- 提供统一的工具注册/查找接口
- 支持将工具导出为 OpenAI function-calling schema
- 支持直接执行本地工具（替代 Go /v1/tools/execute）

用户隔离（S 安全修复）：MCP 等用户级工具注册时携带 ``owner``（user_id 集合，
空集合 = 全局工具）。``to_openai_tools`` 与 ``execute`` 按当前上下文用户过滤/校验，
避免用户 A 的插件工具被用户 B 列出或调用。注册表为进程内可重建状态：
实例重启后由插件配置重新注册（幂等覆盖）。

来源标注：``source`` 说明工具从哪来（``builtin`` 内置 / ``mcp`` MCP 或插件注入），
由注入点显式声明而不是从 ``owner`` 推断 —— ``app/mcp/registry.py`` 注册的是全局
MCP 工具（无 owner），只按 owner 判断会漏标。前端据此把"看不见的 MCP"展示出来。

并发安全（G15）：``execute`` 是全部入口的**唯一执行点**，因此它同时也是**唯一出口**。
出口做两件与具体工具无关的事：

1. **结果 spill 统一收口**（G14）：修前"大结果怎么处理"散在各工具实现里（有的自己截、
   有的落盘、有的整段回吐）。现在统一过 ``spill.apply_spill`` —— 小结果原样、大结果
   落盘换头尾 + locator、落盘失败保留原文（见 ``app/tools/spill.py``）。
2. **并发安全由工具显式声明**（G15）：``is_concurrency_safe`` 读注册时的声明，
   **默认 False**。修前 runtime 是按"动作分级 = read"**推断**并发的，而分级回答的是
   "要不要人确认"，不是"能不能并发" —— 推断错一个分级就等于放错一个并发。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.observability.tool_spans import finish_tool_span, start_tool_span
from app.tools.arg_validation import format_errors, prepare_params
from app.tools.context import get_session_id, get_tenant_id_int
from app.tools.file_queue import serialized
from app.tools.sandbox_slot import (
    SANDBOX_TOOL_NAMES,
    acquire_sandbox_slot,
    sandbox_slot_held,
)
from app.tools.spill import apply_spill

logger = logging.getLogger(__name__)

# 工具来源；新增来源时同步前端的 ToolInfo.source 与展示文案
SOURCE_BUILTIN = "builtin"
SOURCE_MCP = "mcp"

# 隐藏来源：这些工具**不进入模型可见清单**（也不被 tool_search 搜到/激活），
# 只能经 `use_capability` 代理工具调用（P7，对齐 Reasonix usecap）。
#
# 理由：MCP / 插件工具的数量与 schema 由外部服务器决定，直接暴露会让请求体随
# "这个用户连了几个 MCP、每个 MCP 有几个工具"而漂移 —— 既贵（每个 schema 都占 token）
# 又不稳定（同名工具被不同服务器抢注册）。代理工具的 schema 是**固定的**，不随 MCP 增删改变。
HIDDEN_SOURCES: frozenset[str] = frozenset({SOURCE_MCP})


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    owners: set[str] = field(default_factory=set)  # 空 = 全局工具
    source: str = SOURCE_BUILTIN
    #: G15：**工具自己的声明** —— 同批并发执行不会互相干扰。
    #: 默认 False：没声明就串行（与 `tool_policy` 的 fail-closed 同一取向）。
    concurrency_safe: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Awaitable[Any]],
        owner: str = "",
        source: str = SOURCE_BUILTIN,
        concurrency_safe: bool = False,
    ) -> None:
        """注册工具。owner 非空时该工具仅对 owner（user_id）可见/可调用；
        同名重复注册幂等覆盖；已存在且带 owner 时合并归属用户集合
        （共享连接去重场景：多用户引用同一 MCP 服务器）。

        source 由调用方声明（内置工具用默认值，MCP/插件注入点传 SOURCE_MCP）。

        ``concurrency_safe``（G15）是**工具自己**对"能不能与同批其它调用并发"的表态，
        默认 False。重注册时按**本次**声明覆盖：声明与 handler 属于同一份定义，
        分开保留会出现"handler 换了、并发声明还是旧的"这种更难排查的错位。
        """
        existing = self._tools.get(name)
        if existing is not None:
            owners: set[str] = set(existing.owners)
            if owner:
                owners.add(owner)
            existing.owners = owners
            existing.description = description
            existing.parameters = parameters
            existing.handler = handler
            existing.source = source
            existing.concurrency_safe = concurrency_safe
            return
        owners = set()
        if owner:
            owners.add(owner)
        self._tools[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            owners=owners,
            source=source,
            concurrency_safe=concurrency_safe,
        )

    def is_concurrency_safe(self, name: str) -> bool:
        """该工具是否声明了"可并发执行"。

        **默认 False**（未注册 / 未声明都一样）：并发是"多个调用同时动同一份状态"，
        猜错的代价是不可复现的结果；串行猜错的代价只是慢一点。所以这里不做任何
        "看起来是只读就放行"的推断 —— 声明归工具自己，见各工具注册处的说明。
        """
        tool = self._tools.get(name)
        return bool(tool is not None and tool.concurrency_safe)

    def set_owners(self, name: str, owners: set[str]) -> None:
        """更新工具的归属用户集合（共享连接去重场景：多用户引用同一配置）。"""
        tool = self._tools.get(name)
        if tool is not None:
            tool.owners = set(owners or ())

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_prefix(self, prefix: str) -> int:
        """移除所有以 prefix 开头的工具（MCP 重载前清理），返回移除数量。"""
        removed = [n for n in self._tools if n.startswith(prefix)]
        for n in removed:
            del self._tools[n]
        return len(removed)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_capabilities(self, user_id: str = "") -> list[dict[str, str]]:
        """列出**隐藏来源**的能力（供 use_capability 的按需发现使用）。

        与 ``to_openai_tools`` 互补：那些**不进模型可见清单**的工具（MCP / 插件），
        从这里能被列出来，模型据此知道代理背后有哪些能力。
        """
        user_id = self._resolve_user(user_id)
        out: list[dict[str, str]] = []
        for tool in self._tools.values():
            if tool.source not in HIDDEN_SOURCES or not self._visible(tool, user_id):
                continue
            out.append({"name": tool.name, "description": tool.description})
        return sorted(out, key=lambda t: t["name"])

    def _visible(self, tool: ToolDef, user_id: str) -> bool:
        return not tool.owners or user_id in tool.owners

    @staticmethod
    def _resolve_user(user_id: str) -> str:
        """显式传入优先；否则从工具上下文（contextvars）取当前用户。"""
        if user_id:
            return user_id
        try:
            from app.tools.context import get_user_id

            return get_user_id() or ""
        except ImportError:  # pragma: no cover — 上下文模块不可用时视为未认证
            return ""

    def to_openai_tools(self, user_id: str = "") -> list[dict[str, Any]]:
        """导出工具列表；按当前用户过滤掉其他用户的工具，并**跳过隐藏来源**。

        source 放在 function 外层：它不属于 OpenAI 的 function schema，
        混进去会被上游 provider 拒绝。

        P7：``HIDDEN_SOURCES``（MCP / 插件）**不进入可见清单** —— 它们只能经
        ``use_capability`` 代理调用（见 app/tools/use_capability.py）。这样请求体里的
        工具 schema **不随"用户连了几个 MCP"而漂移**，且 tool_search 也搜不到它们。
        """
        user_id = self._resolve_user(user_id)
        converted: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if not self._visible(tool, user_id) or tool.source in HIDDEN_SOURCES:
                continue
            converted.append(
                {
                    "type": "function",
                    "source": tool.source,
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        return converted

    async def execute(
        self, name: str, params: dict[str, Any], user_id: str = ""
    ) -> dict[str, Any]:
        """执行工具。owner 工具要求当前用户匹配（无上下文/未认证时拒绝 owner 工具，
        防止未认证直连越权调用他人插件工具）。

        G-T1：执行前按工具自己的 JSON Schema 做**归一 + 校验**（`arg_validation`）。
        参数不合 schema 时**不执行** handler，而是回一条点名到字段的错误
        ——``handler(**params)`` 抛出的 `TypeError` 对模型毫无指向性，它只能原样
        重试；而 `limit: expected integer, got string "abc"` 能让它当场改对。
        """
        user_id = self._resolve_user(user_id)
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found"}
        if not self._visible(tool, user_id):
            return {"error": f"Tool '{name}' is not available for this user"}

        prepared, errors = prepare_params(name, params, tool.parameters)
        if errors:
            logger.warning("tool argument validation failed (%s): %s", name, errors)
            return {
                "error": format_errors(name, errors),
                # 结构化字段：前端/执行图据此展示，模型侧只读 `error` 那句话。
                "invalid_arguments": errors,
            }

        # R7：租户级沙箱并发槽。只在"当前没有持有槽位"时获取 —— run_code 内部再调
        # shell_exec 时沿用外层槽位，避免把配额变成"限制嵌套次数"。Redis 故障时
        # acquire 返回空租约（放行），不让配额组件拖垮代码执行。
        lease = None
        if name in SANDBOX_TOOL_NAMES and not sandbox_slot_held():
            lease = await acquire_sandbox_slot()
            if lease is None:
                return {
                    "error": "sandbox concurrency limit reached for this tenant; "
                    "wait for a running execution to finish and retry",
                    "code": "sandbox_concurrency_limited",
                }

        span = None
        started = 0.0
        error_text = ""
        try:
            async def _invoke() -> Any:
                async with serialized(name, prepared):
                    return await tool.handler(**prepared)

            span = start_tool_span(
                name=name,
                source=tool.source,
                tenant_id=get_tenant_id_int(),
                user_id=user_id,
            )
            started = time.monotonic()
            if lease is not None:
                with lease.hold():
                    result = await _invoke()
            else:
                result = await _invoke()
            # G14：**唯一出口**统一处理"结果过大" —— 不再让每个工具各自截断/落盘。
            # 放在 span 结束之前（`error_text` 已从原始结果里取过），且 spill 自身
            # 失败会回退到原文，不会把 handler 的结果变成一条错误。
            if isinstance(result, dict) and result.get("error"):
                error_text = str(result.get("error"))
            try:
                return await apply_spill(
                    result, tool_name=name, session_id=get_session_id()
                )
            except Exception as spill_error:  # noqa: BLE001 - 出口兜底：旁路故障不得吞掉结果
                # spill 内部已自吞异常；这一层是最后保险 —— 真走到这里也**原样回吐**
                # handler 的结果，而不是变成 `{"error": ...}`（那等于工具没执行成功）。
                logger.warning(
                    "tool result spill failed at exit (tool=%s): %s", name, spill_error
                )
                return result if isinstance(result, dict) else {"output": result}
        except Exception as e:
            error_text = str(e)
            return {"error": str(e)}
        finally:
            if span is not None:
                finish_tool_span(span, started_at=started, error=error_text)
            if lease is not None:
                await lease.release()


# 全局注册表（进程内单例）
registry = ToolRegistry()


def is_concurrency_safe(name: str) -> bool:
    """全局注册表上的并发安全查询（runtime 侧的接线入口）。

    ``runtime._split_tool_batch`` 目前按"动作分级 = read"**推断**并发；接线后应改为
    问这里。本函数只是 `registry.is_concurrency_safe` 的模块级别名 —— 保留两个形状是
    为了让调用方（以及测试）不必先取到 registry 实例，同时保证**只有一份判定**。
    """
    return registry.is_concurrency_safe(name)
