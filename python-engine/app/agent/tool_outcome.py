"""工具调用结果的**可判定状态**（G9）。

为什么需要它：`content` 是给模型读的自然语言，而"这次调用到底发生了什么"是
给机器（前端、评测、审计、恢复逻辑）用的判定。两者混在一起时，唯一能做的判断
就是"字符串里有没有'未知'两个字" —— 而工具的正常输出里完全可能出现这句话。

词表只有五个值，语义互斥：

| 值 | 含义 | 能重试吗 |
|---|---|---|
| `ok` | 成功且有结果 | — |
| `error` | 失败且有明确的错误 | 视错误而定 |
| `cancelled` | 只读调用被取消，**没有副作用** | 可以 |
| `unknown` | **已开始**、可能已产生副作用、结果未拿到 | **不可以**（会重复副作用） |
| `not_started` | 调用**没有开始**，可以安全重试 | 可以 |

`unknown` 与 `not_started` 的区分是整个文件的重点：恢复时把这两者混为一谈，
要么白白放弃一次安全的调用，要么重复一次已经生效的写操作。
"""

from __future__ import annotations

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_NOT_STARTED = "not_started"

OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_OK, OUTCOME_ERROR, OUTCOME_CANCELLED, OUTCOME_UNKNOWN, OUTCOME_NOT_STARTED}
)

#: 消息里的稳定标签前缀。放在文案**最前面**：任何按行首解析的消费方都不必全文搜索。
TAG_PREFIX = "[tool-outcome:"
TAG_SUFFIX = "]"


def tag(outcome: str) -> str:
    """给定状态返回标签；非法状态直接报错（拼错的状态比没有状态更危险）。"""
    if outcome not in OUTCOMES:
        raise ValueError(f"未知的工具结果状态：{outcome!r}（合法值 {sorted(OUTCOMES)}）")
    return f"{TAG_PREFIX}{outcome}{TAG_SUFFIX}"


def with_tag(content: str, outcome: str) -> str:
    """给一段文案加状态标签（幂等：已有标签则替换）。"""
    body = strip_tag(content)
    return f"{tag(outcome)} {body}".strip()


def strip_tag(content: str) -> str:
    """去掉行首标签，返回给模型读的正文。"""
    text = str(content or "")
    if text.startswith(TAG_PREFIX):
        end = text.find(TAG_SUFFIX)
        if end != -1:
            return text[end + len(TAG_SUFFIX) :].lstrip()
    return text


def classify(content: object) -> str:
    """从工具结果内容判定状态。

    判定顺序：显式标签 > 结构化字段（调用方已给 `outcome`）> 保守默认。
    没有标签的历史消息一律归 `unknown` 而不是 `ok` —— 宁可"不知道"，
    也不要把"没拿到结果"当成"成功了"。
    """
    text = str(content or "")
    if text.startswith(TAG_PREFIX):
        end = text.find(TAG_SUFFIX)
        if end != -1:
            value = text[len(TAG_PREFIX) : end]
            if value in OUTCOMES:
                return value
    return OUTCOME_UNKNOWN


def classify_result(result: object) -> str:
    """从工具结果**对象**判定状态（优先结构化 `outcome` 字段）。"""
    if isinstance(result, dict):
        explicit = str(result.get("outcome") or "")
        if explicit in OUTCOMES:
            return explicit
        if result.get("cancelled"):
            return OUTCOME_CANCELLED
        if result.get("error"):
            return OUTCOME_ERROR
        if "output" in result or "content" in result:
            return OUTCOME_OK
    return classify(result)


def retryable(outcome: str) -> bool:
    """这个状态能不能安全重试（`unknown` 不行 —— 它可能已经生效）。"""
    return outcome in {OUTCOME_CANCELLED, OUTCOME_NOT_STARTED}


def needs_human_review(outcome: str) -> bool:
    """要不要人来看一眼（`unknown` 是唯一需要的那种）。"""
    return outcome == OUTCOME_UNKNOWN
