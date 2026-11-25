"""工具结果的**统一 spill 出口**（G14）。

## 修前的问题

"大结果怎么处理"散落在各工具实现里：`read_file` 自己截行、`grep_files` 自己卡命中数、
`shell_exec` 整段回吐、若干工具什么都不做。同一个"结果过大"在不同工具上有不同行为 ——
模型无从预期（它必须逐个工具去试"这次会不会被截"），测试也没法统一断言"大结果被怎么处理了"。

## 现在的做法

`ToolRegistry.execute` 是**唯一出口**（所有入口都汇聚到那里：主循环、`run_code` 的嵌套
调用、`use_capability` 代理、`api/tools.py` 直连 —— 与 `file_queue` 选同一处的理由相同），
结果在回给调用方之前统一过一遍本模块的 :func:`apply_spill`：

1. **小结果原样返回，不落盘** —— 存储不是免费的，没必要为"可能用得上"付这个成本；
2. **超过阈值 → 落盘 + 只留头尾 + locator**：原文写进**既有**
   ``app/agent/result_store``（与 runtime 的 ``_compact_tool_result``、``read_tool_result``
   同一套 key/引用格式），**不另造一套存储** —— 否则同一份"大结果"会有两条取回路径，
   两条都会漂移；
3. **落盘失败就保留原文**：存储是锦上添花，它挂了不能让工具结果变成空或半截。

## 为什么头尾而不是摘要

摘要（``result_compactor``）需要知道内容形态，且要额外解释自己"漏了什么"；出口这一层
刻意只做**无损可查**的最小动作：头 + 尾 + 一个 ref。要更聪明的摘要，由 runtime 那层
按需再做 —— 出口只保证"细节没有丢，且取回方式唯一"。

## 尺寸口径

``SPILL_THRESHOLD_CHARS`` 与 ``result_compactor.INLINE_LIMIT``（16KB）同量级：低于它的结果
本来就会被 runtime 原样放进上下文，出口再压一次纯属多此一举。而头尾合计 3000 **字符**
（最坏算 UTF-8 中文 9KB）仍在 16KB 内联上限之下 ⇒ runtime 不会对**已经 spill 过**的结果
二次压缩，不会出现"一个结果两个 ref"。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: 超过这个**字符**数就落盘。与 `result_compactor.INLINE_LIMIT` 同量级（见模块说明）。
SPILL_THRESHOLD_CHARS = 16 * 1024
#: 头尾各保留多少字符。合计 3000 字符 < 内联上限，见模块说明。
HEAD_CHARS = 1500
TAIL_CHARS = 1500
#: 递归下钻的层数上限：只处理"结果里的长字符串"，不做无限树遍历。
MAX_DEPTH = 4

#: 豁免工具：
#:
#: * ``read_tool_result`` —— 它本身就是取回通道，再 spill 一次会让模型为了读 A 先读 B，
#:   而 B 又是 ref（无限套娃）；
#: * ``read_image`` —— 它的"内容"是给多模态模型的数据 URL，裁掉等于把图撕了，
#:   裁了也没有"头尾"可解释。
EXEMPT_TOOLS: frozenset[str] = frozenset({"read_tool_result", "read_image"})


@dataclass(frozen=True)
class SpillOutcome:
    """一次 spill 的裁决（机器可读：调用方不必去正文里找"省略"两个字）。"""

    text: str
    spilled: bool = False
    result_ref: str = ""
    original_chars: int = 0


def new_result_ref() -> str:
    """生成 locator。前缀 ``sp_`` 与 runtime 自己用的 ``tr_`` 区分开，便于排查来源。"""
    return f"sp_{uuid.uuid4().hex[:12]}"


def clip_around(
    text: str,
    result_ref: str,
    *,
    head_chars: int = HEAD_CHARS,
    tail_chars: int = TAIL_CHARS,
) -> str:
    """把原文裁成"头 + 省略标记 + 尾 + locator"。"""
    head = text[:head_chars]
    tail = text[-tail_chars:]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n"
        f"…（已省略 {dropped} 字符，原文共 {len(text)} 字符）…\n"
        f"{tail}\n"
        f'[结果过大已落盘] result_ref="{result_ref}"；需要细节时用 '
        f'read_tool_result(result_ref="{result_ref}", offset=0, limit=8000) 分段取，'
        f"不要重复调用原工具去拿同样的内容。"
    )


async def _store(session_id: str, ref: str, text: str) -> bool:
    """落盘。**任何失败都返回 False**（不抛），由调用方回退到原文。"""
    try:
        from app.agent import result_store

        return bool(await result_store.store(session_id, ref, text))
    except Exception as e:  # noqa: BLE001 - 落盘是旁路，不能把工具结果一起带走
        logger.warning("tool result spill store failed (ref=%s): %s", ref, e)
        return False


async def spill_text(
    text: str,
    *,
    tool_name: str = "",
    session_id: str = "",
    threshold: int = SPILL_THRESHOLD_CHARS,
) -> SpillOutcome:
    """单段文本的 spill：不超阈值原样返回，超了就落盘换头尾 + locator。"""
    if len(text) <= threshold:
        return SpillOutcome(text)

    ref = new_result_ref()
    if not await _store(session_id, ref, text):
        # 存储不可用 ⇒ **保留原文**。模型拿到完整内容永远比拿到"半截 + 取不回"好。
        logger.warning(
            "tool result spill skipped (store unavailable): tool=%s chars=%d",
            tool_name,
            len(text),
        )
        return SpillOutcome(text, original_chars=len(text))

    logger.info(
        "tool result spilled: tool=%s chars=%d ref=%s", tool_name, len(text), ref
    )
    return SpillOutcome(
        text=clip_around(text, ref),
        spilled=True,
        result_ref=ref,
        original_chars=len(text),
    )


async def _spill_node(
    node: Any,
    *,
    path: str,
    refs: list[dict[str, Any]],
    tool_name: str,
    session_id: str,
    threshold: int,
    depth: int,
) -> tuple[Any, bool]:
    """递归处理一个节点，返回 ``(新节点, 是否发生了替换)``。

    **只在真的换了内容时**才重建容器（未命中的子树原对象返回），这样"小结果"能做到
    返回同一个对象 —— 出口不该悄悄改掉一个没超阈值的结果。
    """
    if depth > MAX_DEPTH:
        return node, False

    if isinstance(node, str):
        outcome = await spill_text(
            node,
            tool_name=tool_name,
            session_id=session_id,
            threshold=threshold,
        )
        if not outcome.spilled:
            return node, False
        refs.append(
            {
                "field": path or "output",
                "original_chars": outcome.original_chars,
                "result_ref": outcome.result_ref,
            }
        )
        return outcome.text, True

    if isinstance(node, dict):
        rebuilt: dict[Any, Any] = {}
        changed = False
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            new_value, child_changed = await _spill_node(
                value,
                path=child_path,
                refs=refs,
                tool_name=tool_name,
                session_id=session_id,
                threshold=threshold,
                depth=depth + 1,
            )
            rebuilt[key] = new_value
            changed = changed or child_changed
        return (rebuilt if changed else node), changed

    if isinstance(node, (list, tuple)):
        rebuilt_items: list[Any] = []
        changed = False
        for index, item in enumerate(node):
            new_item, child_changed = await _spill_node(
                item,
                path=f"{path}[{index}]",
                refs=refs,
                tool_name=tool_name,
                session_id=session_id,
                threshold=threshold,
                depth=depth + 1,
            )
            rebuilt_items.append(new_item)
            changed = changed or child_changed
        if not changed:
            return node, False
        return (rebuilt_items if isinstance(node, list) else tuple(rebuilt_items)), True

    return node, False


async def apply_spill(
    result: Any,
    *,
    tool_name: str = "",
    session_id: str = "",
    threshold: int = SPILL_THRESHOLD_CHARS,
) -> Any:
    """``registry.execute`` 的唯一出口调用点。

    * 非 dict 的 handler 返回值按既有约定包成 ``{"output": ...}``；
    * 带 ``error`` 的结果**不裁**：错误文案是给模型的指令，裁掉中段等于让它照着
      半句话去改参数；
    * ``EXEMPT_TOOLS`` 里的工具不裁（理由见该常量的说明）；
    * 没发生替换时返回**原对象**（调用方/测试可据此断言"这篇没被动过"）。
    """
    if not isinstance(result, dict):
        return await apply_spill(
            {"output": result},
            tool_name=tool_name,
            session_id=session_id,
            threshold=threshold,
        )
    if result.get("error") or tool_name in EXEMPT_TOOLS:
        return result

    refs: list[dict[str, Any]] = []
    try:
        rebuilt, changed = await _spill_node(
            result,
            path="",
            refs=refs,
            tool_name=tool_name,
            session_id=session_id,
            threshold=threshold,
            depth=0,
        )
    except Exception as e:  # noqa: BLE001 - 出口兜底：宁可原样回吐，也不能丢结果
        logger.warning("tool result spill failed (tool=%s): %s", tool_name, e)
        return result

    if not changed or not refs:
        return result

    # 主要 locator 取"被裁掉最多的那个字段"—— 重新执行时最可能需要的正是它。
    main = max(refs, key=lambda item: item["original_chars"])
    out = dict(rebuilt)
    # 顶部 `result_ref` 是**既有约定**的名字，但有些工具自己就用它（`subagent` 的
    # result_ref 是 run_id）。已经被占用时不覆盖，locator 仍完整存在于 `spill` 字段与
    # 被裁正文的尾注里 —— 覆盖等于把另一个工具的引用悄悄改坏。
    out.setdefault("result_ref", main["result_ref"])
    out["spill"] = {
        "result_ref": main["result_ref"],
        "count": len(refs),
        "fields": refs,
        "note": "被裁字段的原文已落盘；用 read_tool_result(result_ref=...) 分段取回。",
    }
    return out


__all__ = [
    "EXEMPT_TOOLS",
    "HEAD_CHARS",
    "MAX_DEPTH",
    "SPILL_THRESHOLD_CHARS",
    "TAIL_CHARS",
    "SpillOutcome",
    "apply_spill",
    "clip_around",
    "spill_text",
]
