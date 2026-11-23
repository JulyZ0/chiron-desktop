"""工具执行上下文 — contextvars 传播当前运行的 agent 上下文。

deepseek-harness 的工具经 exec context（ToolExecution）携带调用方信息；
chiron 的 registry.execute 只有 (name, params)，工具无法感知 session/网关。
此处用 contextvars 在 AgentRuntime.run() 内设置，工具通过 get_* 读取，
在 async 环境中自动沿任务传播（无需改 registry 签名）。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# default 用 None 哨兵而不是 {}：ContextVar 的默认值是**所有未设置该变量的上下文
# 共享的同一个对象**，可变默认会让某处就地修改泄漏到其它请求/任务（B039）。
_current_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "chiron_tool_context", default=None
)


def set_tool_context(**kwargs: Any) -> None:
    """在 agent 循环内设置当前上下文（session_id/user_id/tenant_id/gateway）。"""
    merged = dict(_current_context.get() or {})
    merged.update(kwargs)
    _current_context.set(merged)


def get_tool_context(key: str, default: Any = None) -> Any:
    return (_current_context.get() or {}).get(key, default)


def get_session_id() -> str:
    return str(get_tool_context("session_id", ""))


def get_user_id() -> str:
    return str(get_tool_context("user_id", ""))


def get_tenant_id() -> str:
    """当前租户 ID 的**字符串形式**（未绑定 / 无法解析时返回 ``""``）。

    为什么默认是 str：绝大多数调用点是**格式化**场景 —— 沙箱路径拼接
    （``sandbox_root() / tenant``）、审计条目的 ``tenant`` 字段、
    ``kb_access_allowed`` 的租户比较、``SkillStore`` 的目录段。
    需要整数（SQL 参数 / Pydantic 请求体）的地方用 ``get_tenant_id_int()``。

    与 Go 的契约：网关与 JWT 里的 tenant_id 已是整数，py 侧只在**边界**把它
    规约到整数域（见 ``to_tenant_int``），对外仍以字符串形式供格式化使用。
    """
    tid = get_tenant_id_int()
    return str(tid) if tid else ""


def get_tenant_id_int() -> int:
    """当前租户 ID 的**整数形式**（SQL 参数 / Pydantic 请求体用）；未绑定为 0。"""
    return to_tenant_int(get_tool_context("tenant_id", 0))


def to_tenant_int(value: Any) -> int:
    """把租户 ID 宽容解析为 int16 域内的整数；无法解析时返回 0。

    用于 SQL 参数：`tenant_id` 列已改为 smallint，asyncpg 传 str 会直接报错。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n < 0 or n > 32767:
        return 0
    return n


def get_project() -> str:
    """当前工作台选中的**项目**（C3）。

    记忆（L2 条目）按 `tenant + user + project` 隔离 —— **空串 = 未分组**（缺省）。
    由 runtime 在 run 开始时从 `task.workbench_context["project"]` 写入，工具侧只读。
    """
    return str(get_tool_context("project", ""))


def get_project_locator() -> dict[str, str]:
    """当前项目的身份快照（S0：id/provider/device_id/root/root_key）。

    与 `get_project()` 的区别：`project` 是**记忆隔离键**（字符串 workspace id），
    这里描述的是**文件身份**（哪个 provider、哪台设备、规范化路径键）。远程/多设备
    模型下工具应依赖它，而不是把 `project_root` 当成跨机器通用路径。
    形状不对或未设置时返回空 dict，调用方按"无项目身份"处理。
    """
    raw = get_tool_context("project_locator", {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def get_plugin_names() -> list[str]:
    """本次对话选中的插件名（MCP server 名）；未选时为**空列表**。

    空列表的语义是"不限制"，不是"一个都不许" —— 用户没在对话里挑插件时，全部已连接
    的插件都可用；只有明确挑过，才收窄到这几个。

    为什么工具层需要它：MCP / 插件工具是**隐藏来源**，不进模型可见的工具清单，
    只能经 `use_capability` 代理调用。于是 runtime 里那句
    `_restrict_tools_to_plugins(...)` 对它们完全无效（它筛的是可见清单，而它们
    压根不在里面），代理层若也不读这个选择，用户在对话里挑插件就等于没挑。
    """
    raw = get_tool_context("plugin_names", ())
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def get_gateway() -> Any:
    """当前运行的 GatewayRouter 引用（子 agent 委派需要）。"""
    return get_tool_context("gateway", None)


def get_activated_tools() -> set[str]:
    """本会话**按需激活**的工具名集合（Token Economy 的实现点）。

    为什么返回一个**可变的 set 引用**，而不是像其它 get_* 那样返回值：

        contextvar 的值是**不可变传播**的 —— 工具内部再 `set_tool_context(...)`
        只会改当前任务的副本，**runtime 那一侧看不到**。而"按需激活"要求
        `tool_search` 工具内的一次 `add`，能被 runtime 的 `_get_core_tools` 观察到。

    所以这里把同一个 set 放进 context，工具与 runtime 共享这一个引用：
    工具 `add` → runtime 下一轮过滤时立刻可见。懒创建，缺失时自动补空集合。
    """
    ctx = _current_context.get() or {}
    acts = ctx.get("activated_tools")
    if not isinstance(acts, set):
        acts = set()
        merged = dict(ctx)
        merged["activated_tools"] = acts
        _current_context.set(merged)
    return acts


def activate_tools(names: list[str]) -> set[str]:
    """把工具加入本会话的已激活集合，返回激活后的完整集合（便于工具回报状态）。"""
    acts = get_activated_tools()
    acts.update(n for n in names if n)
    return acts


def get_all() -> dict[str, Any]:
    """完整快照当前上下文（子 agent 委派前保存、完成后恢复）。"""
    return dict(_current_context.get() or {})


def restore_context(snapshot: dict[str, Any]) -> None:
    """恢复上下文快照（子 agent 执行会改写 context，父任务需要还原）。"""
    _current_context.set(snapshot)


# ── G-T4：当前工具调用的身份（供嵌套调用挂父指针）─────────────


def get_tool_call_id() -> str:
    """当前正在执行的工具调用 id（模型给出的 `tool_call.id`）；不在工具内时为空串。"""
    return str(get_tool_context("tool_call_id", "") or "")


def get_tool_span_id() -> str:
    """当前工具节点在执行图里的 span_id；取不到时为空串。

    与 `AgentRuntime.run` 记 `record_span` 时用的那一个**逐字相同**
    （`{turn_span}:tool:{tool_call_id}`），所以子节点挂上去之后，
    从图里点父节点就能看到它，反过来也一样。
    """
    return str(get_tool_context("tool_span_id", "") or "")


@contextmanager
def tool_call_scope(tool_call_id: str, *, span_id: str = "") -> Iterator[None]:
    """把"这一层是哪个工具调用"压进上下文，退出时**精确恢复**上一层。

    为什么需要它（G-T4）：`run_code` 在子进程里跑的嵌套工具调用，在主循环看来
    **是不存在的** —— 它不走 `_dispatch_tool`，所以主循环不会为它记 span，
    `loop_guard` 也观察不到它。嵌套调用要知道"我是谁发起的"，只能靠上下文，
    而 `registry.execute` 的签名是 `(name, params)`，没有位置传这个。

    为什么用 `reset(token)` 而不是恢复一份快照：嵌套是**可能多层**的
    （`run_code` 里再调 `run_code`），快照式恢复在有并发兄弟调用时会把别人的
    写入一起回滚；token 只撤销自己那一次 `set`。

    `span_id` 是**本次调用自己的节点**，进入作用域即覆盖上一层 —— 上层那个
    id 已经从调用方手里传下来了（父指针由调用方自己拼），这里不需要保留。
    """
    merged = dict(_current_context.get() or {})
    if tool_call_id:
        merged["tool_call_id"] = tool_call_id
    if span_id:
        merged["tool_span_id"] = span_id
    token = _current_context.set(merged)
    try:
        yield
    finally:
        _current_context.reset(token)
