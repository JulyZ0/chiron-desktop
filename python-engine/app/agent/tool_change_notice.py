"""工具表变更声明（差距 G-P3）：把"这一轮工具表变了什么"作为独立 system 消息告知模型。

## 为什么需要

模型对"我有哪些工具"的认知来自**它看到的那份工具表**。工具表在 `run()` 开头算一次
（模式白名单 + `tool_search` 激活集 + 插件限定），所以**跨轮会变**：上一轮用
`tool_search` 激活了新工具、或用户在对话里换了插件，下一轮的表就不一样了。

不告知的代价是双向的：

* **少**：模型按旧记忆去调一个已经不在表里的工具 —— 每次消耗一次往返，换回一条
  失败结果，还得再花一次往返纠正。
* **多**：新出现的工具它不知道，于是继续用更笨的办法绕（明明激活了 git 工具，
  还是手搓 `shell_exec` 里的 git 命令）。

pi 的对位做法是每个请求前跑 `declareToolChanges`（`agent-loop.ts:342`，调用点
:119 与 :220）；`prepareLoadout`（`agent-session.ts:1694`）还能按会话改描述。

## 为什么只声明增删、不声明 schema 变化

schema 变了名字没变时，新 schema 已经**在请求里**（模型读的就是它），再声明一遍是
噪音；而增删改变的是"我能做什么"这个判断本身。所以本模块只比名字集合。

## 为什么独立成 system 消息

与记忆 / hook 上下文 / 子 Agent 汇报同一处置（见 `runtime._apply_system_prefix`）：
拼进 `system_prompt` 会让逐字稳定的前缀缓存整段失效。而这条消息只在**变化时**出现，
变化本身已经打破了缓存，所以多这一条是零边际成本。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 一次最多列出多少个工具名。`tool_search` 一次激活十几二十个是常态，全列出来会把
#: 一条"声明"变成半屏目录 —— 那反而把真正有用的那半句（还有别的工具可用）淹掉。
#: 超出时只报数量，让模型自己用 `tool_search` 去找。
_MAX_LISTED = 12


def _fmt(names: list[str]) -> str:
    if len(names) <= _MAX_LISTED:
        return ", ".join(f"`{n}`" for n in names)
    head = ", ".join(f"`{n}`" for n in names[:_MAX_LISTED])
    return f"{head} … (+{len(names) - _MAX_LISTED} more)"


def render_tool_change_notice(
    *,
    previous: Iterable[str],
    current: Iterable[str],
) -> str:
    """把"工具表从 `previous` 变成了 `current`"渲染成一段 system 文本。

    没有增删时返回**空串** —— 调用方据此不插消息，因此工具表不变的那些轮次
    system 段与引入本机制之前**逐字相同**（前缀缓存照常命中）。
    """
    prev = {str(n) for n in previous if str(n)}
    cur = {str(n) for n in current if str(n)}
    added = sorted(cur - prev)
    removed = sorted(prev - cur)
    if not added and not removed:
        return ""

    lines = [
        "## Tool set changed",
        "",
        "The set of tools available in this conversation changed since your previous "
        f"turn (now {len(cur)} tool(s)). Anything you remember about the previous set "
        "may be stale:",
    ]
    if added:
        lines.append(f"- **Now available:** {_fmt(added)}")
    if removed:
        lines.append(f"- **No longer available:** {_fmt(removed)}")
    lines += [
        "",
        "The tool list in this request is the truth. For something you cannot see but "
        "suspect exists, call `tool_search`; do not call a tool from memory.",
    ]
    return "\n".join(lines)
