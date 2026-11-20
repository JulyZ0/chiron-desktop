"""工作台上下文（workbench context）的读取与合并口径。

前端 `WorkbenchQuickStart` / `ChatSidePanel` 允许多选（`?kb=a&kb=b&...`），组装成
`kb_ids` / `agent_ids` / `workflow_ids` 数组。本模块把这套读取收敛成一处，
避免三条链路（SSE submit / chat submit / quick-execute）各写一遍"先看复数、再回退
单数"的逻辑而出现行为分歧 —— 那是"在首页多选生效、在侧栏多选不生效"这类
最难查的 bug 的根源。
"""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")

#: 一次对话最多注入的知识库数。与前端 ``components/chat/contextChips.ts`` 的
#: ``MAX_CONTEXT_KBS`` **逐值对齐**：每个库都是一次分钟级 LLM 推理、一次独立索引读取，
#: 多选上限就是首字节延迟的上限。前端负责不让用户选出第 6 个，后端负责
#: 手写 URL（``?kb=a&kb=b&...``）时也守住底线。
MAX_CONTEXT_KBS = 5


def _as_str_list(raw: Any) -> list[str]:
    """把任意形态的字段收敛成去空、去重、保序的字符串列表。"""
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            # P1-6：多库权重（`[{id, weight}]`）与纯字符串并存。这里**只取 id**，
            # 权重走 `context_weights` —— 两个函数读同一份解析规则，调用方按需取。
            if isinstance(item, dict):
                item = item.get("id")
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value not in out:
                out.append(value)
        return out
    return []


#: 权重缺省值与上限。1.0 = 与引入权重之前的行为逐值相同（不改变老前端的结果）；
#: 上限 10 是"别把某一个库权重调到吃掉全部配额"的防线（权重只影响排序，不做硬截断）。
DEFAULT_CONTEXT_WEIGHT = 1.0
MAX_CONTEXT_WEIGHT = 10.0


def _coerce_weight(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_WEIGHT
    if value <= 0 or value != value:  # <=0 或 NaN
        return DEFAULT_CONTEXT_WEIGHT
    return min(value, MAX_CONTEXT_WEIGHT)


def context_weights(workbench_context: Any, name: str) -> dict[str, float]:
    """读取 ``<name>_ids`` 里每个 id 的权重（缺省 1.0）。

    兼容三种形态：``["a","b"]``（纯字符串，全部 1.0）、
    ``[{"id":"a","weight":2}]``、以及两者混用。非法权重（0/负数/NaN/非数字）**回落 1.0**，
    而不是抛错或当成 0 —— 用户手写 URL 时一个笔误不该让整个库从结果里消失。
    """
    ctx = workbench_context if isinstance(workbench_context, dict) else {}
    raw = ctx.get(f"{name}_ids")
    if not isinstance(raw, (list, tuple)):
        # 单值回退（老前端 `?kb=xxx`）：只有 id，没有权重
        single = _as_str_list(ctx.get(f"{name}_id"))
        return {value: DEFAULT_CONTEXT_WEIGHT for value in single}
    out: dict[str, float] = {}
    for item in raw:
        if isinstance(item, dict):
            item_id = item.get("id")
            weight = _coerce_weight(item.get("weight", DEFAULT_CONTEXT_WEIGHT))
        else:
            item_id, weight = item, DEFAULT_CONTEXT_WEIGHT
        if not isinstance(item_id, str):
            continue
        key = item_id.strip()
        if not key:
            continue
        # 重复 id：保留**首次**出现的权重（与 `_as_str_list` 的"去重保序"同口径）
        out.setdefault(key, weight)
    return out


def merge_weighted(groups: list[tuple[float, list[T]]], limit: int) -> list[T]:
    """按 ``weight × score`` 合并多来源结果（P1-6：多库权重）。

    与 :func:`merge_by_quota` 的差别：配额合并**刻意不看分数**，因为不同知识库的
    embedding 与索引不同、分数不可比；一旦用户显式给了权重，他要的就是"这个库更重要"
    —— 此时不看权重反而违背意图。

    口径：每个来源内部按**名次**折算分数 ``1/(rank+1)``（名次内可比，跨库不可比，
    所以用名次而不是原始分数），再乘以该来源的权重排序。权重相等时退化为
    "各来源首条轮流、再第二条……"，与 :func:`merge_by_quota` 结果一致 —— 老前端
    （不传权重）看到的东西不变。

    ``limit <= 0`` 返回空。
    """
    if limit <= 0:
        return []
    scored: list[tuple[float, int, T]] = []
    for weight, items in groups:
        effective = _coerce_weight(weight)
        for rank, item in enumerate(items):
            scored.append((effective / (rank + 1), rank, item))
    # 稳定排序：分数相同则按来源顺序、组内名次 —— 结果可复现，测试不会因实现抖动
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _score, _rank, item in scored[:limit]]


def context_ids(workbench_context: Any, name: str) -> list[str]:
    """读取多值字段 ``<name>_ids``，缺失时回退单值 ``<name>_id``。

    单值回退是**向后兼容**：老前端与手写 URL（``?kb=xxx``）只发单值。回退只在
    复数完全缺失时发生，不合并两者 —— 两者同时出现时以复数（更新的契约）为准。
    """
    ctx = workbench_context if isinstance(workbench_context, dict) else {}
    ids = _as_str_list(ctx.get(f"{name}_ids"))
    if ids:
        return ids
    return _as_str_list(ctx.get(f"{name}_id"))


def merge_by_quota(groups: list[list[T]], limit: int) -> list[T]:
    """多来源检索结果的轮询配额合并。

    多选知识库的意图是"这几个库都要用"，所以每轮从各来源取一条（保证每个来源都有
    代表），而不是把各来源结果按分数混排后截断 —— 后者会让高分来源占满配额、低分
    来源形同没选。何况不同知识库的 embedding 与索引不同，**分数本就不可比**，
    混排排序本身就没有意义。

    ``limit`` 是合并后的硬上限（保护首字节延迟）；``limit <= 0`` 返回空。
    """
    if limit <= 0:
        return []
    merged: list[T] = []
    index = 0
    while len(merged) < limit:
        progressed = False
        for group in groups:
            if index < len(group):
                merged.append(group[index])
                progressed = True
                if len(merged) >= limit:
                    break
        if not progressed:  # 所有来源都已取空
            break
        index += 1
    return merged


def selected_memory_slots(workbench_context: Any) -> list[str]:
    """本次对话要注入的长期记忆分类（``memory_slots``）。

    空列表 = **未指定** = 沿用默认（注入全部）—— 缺省不收窄，是为了不改变既有行为：
    记忆注入本来就是一直生效的，前端没传就把它关掉属于静默的功能回退。

    ``all`` 折叠成空列表：服务端只需要「全量」与「按分类」两种状态，多一个
    显式全量值只会让下游到处判断它。
    """
    slots = _as_str_list((workbench_context or {}).get("memory_slots"))
    if "all" in slots:
        return []
    return slots
