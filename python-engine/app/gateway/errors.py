"""provider 错误归一（C2 段 b：上下文溢出识别）。

**为什么要归一**：上下文溢出是唯一"可自动恢复"的 LLM 错误 —— 压缩后重试通常就过了。
而各家的报法完全不同，不归一就只能靠调用点各写一套字符串匹配，必然漂移。

各家的实际形态：

* **OpenAI 系**：`error.code == "context_length_exceeded"`，或 message 含
  `maximum context length`；
* **Anthropic**：`invalid_request_error` + message 含 `prompt is too long`；
* **DeepSeek 等 OpenAI 兼容**：message 含 `maximum context length` / `too many tokens`。

**只重试一次**（调用方负责）：溢出恢复要重发整段上下文，成本翻倍；再失败说明压缩没救回来
（例如单条消息本身就超窗），继续重试只是烧钱。因此这里只回答"是不是溢出"，
"重试几次"由调用方按成本决定。
"""

from __future__ import annotations

#: 溢出标记表（大小写不敏感）。宁可少报也不要误报 —— 误报会把"限流/鉴权失败"当成溢出，
#: 于是白白重发一次昂贵的请求。
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "prompt is too long",
    "too many tokens",
    "reduce the length",
    "input is too long",
)


def is_context_overflow(exc: BaseException, provider: str = "") -> bool:
    """判断异常是否为"上下文超出窗口"。

    Args:
        exc: provider 抛出的异常（可能是 SDK 的封装异常，错误体挂在属性上）。
        provider: 调用方已知的 provider 标识。当前仅用于日志/未来按家分支；
            判定走统一的标记表 —— 各家标记互不冲突，按 provider 分支反而容易漏。

    Returns:
        是否为可恢复的上下文溢出。
    """
    text = _flatten(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


#: 配额/计费类标记（大小写不敏感）。
#:
#: 这类错误**必须和 429 限流分开**：两者同为 429，处置却相反 —— 限流换一把 key 就有救，
#: 而配额耗尽换多少把都是同一个账号的空额度，重试只是白烧一次往返，还会把"该充值/该提额"
#: 误诊成"这条链路抖了一下"。
#:
#: 刻意**不用**裸 `quota`：部分 OpenAI 兼容厂商（Together / Groq 等）的限流文案里也带
#: 这个词，用它会把"该换 key 的限流"判成配额耗尽 —— 那个方向的误报更贵（用户看到一次
#: 本可避免的失败）。这里只收明确表达"额度/计费"的措辞。
_QUOTA_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "billing hard limit",
    "credit balance is too low",
    "insufficient_balance",
    "insufficient balance",
    "account_deactivated",
    "account is deactivated",
    "额度不足",
    "余额不足",
    "配额已用尽",
)


def is_quota_exhausted(exc: BaseException) -> bool:
    """判断异常是否为"账号额度/计费已用尽"（而非这一把 key 的瞬时限流）。

    402（Payment Required）本身就是配额类，不走文案匹配。

    与 `is_context_overflow` 同一取舍：只回答"是不是这一种"，重试几次、换不换 key
    由调用方按成本决定。
    """
    for attr in ("status_code", "http_status", "status"):
        if getattr(exc, attr, None) == 402:
            return True
    text = _flatten(exc).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


#: 「模型不在套餐 / 不存在」的文案标记（大小写不敏感）。
#:
#: **实测现场**（2026-11，子 Agent 全部 failed）：
#: `400 claude-sonnet-4-20250514 isn't included in OpenCode Go. See the models it
#: includes at https://opencode.ai/console/go.` —— 这条是**确定性**的配置错：
#: 换 key、换时间、重试一万次都是同一个结果。把它当成"瞬时错误"处理的代价是
#: 密钥被冷却（之后报 `no_usable_key`，真实原因被掩盖）+ 每个请求都白打一次上游。
_NOT_INCLUDED_MARKERS: tuple[str, ...] = (
    "isn't included in",
    "is not included in",
    "not included in",
    "isn't available in",
    "is not available in",
    "not available in your plan",
    "not available on your plan",
    "isn't supported by this plan",
)

#: 「模型不存在 / 名字非法」的文案标记。
_MODEL_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "model_not_found",
    "model not found",
    "no such model",
    "unknown model",
    "invalid model",
    "unsupported model",
    "model does not exist",
    "model not supported",
    "model is not supported",
    "does not exist or you do not have access",
)


def status_of(exc: BaseException) -> int | None:
    """从异常里取 HTTP 状态码（各家 SDK 字段名不同，逐级尝试）。

    与 `app.gateway.key_retry._status_of` 同一口径，这里公开出来供错误分类复用
    （两处各写一份判断会漂移）。
    """
    for attr in ("status_code", "http_status", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_model_unavailable(exc: BaseException) -> bool:
    """是否"这个模型在当前 provider 上根本不可用"（不在套餐内 / 不存在）。

    这类错误**必须**与限流、鉴权、上游抖动分开：
    * 换 key 无用（同一账号的套餐）；
    * 重试无用（同一份配置）；
    * 冷却密钥是有害的 —— 它会让下一次请求直接报 `no_usable_key`，把真实原因埋掉。

    判定以文案为主、状态码为辅：OpenCode 用 400，OpenAI 用 404 `model_not_found`，
    各家不统一；但只要文案命中"not included / model not found"就是确定性的。
    """
    status = status_of(exc)
    if status is not None and status not in (400, 403, 404, 422):
        # 403 也算进来：部分网关用 403 + "not available in your plan" 表达订阅范围。
        return False
    text = _flatten(exc).lower()
    return any(m in text for m in _NOT_INCLUDED_MARKERS) or any(
        m in text for m in _MODEL_NOT_FOUND_MARKERS
    )


#: 确定性 4xx：请求本身错了，换 key / 重试都不会变好（不冷却密钥、不重试、
#: 也不该记成 provider 故障 —— 它不说明这个 provider 坏了）。
#: 刻意**不含** 409（部分厂商用它表达冲突/重放，`key_retry` 里按可换 key 处理）。
_DETERMINISTIC_STATUS: frozenset[int] = frozenset({400, 404, 405, 413, 422})


def is_deterministic_client_error(exc: BaseException) -> bool:
    """是否是"重试没有意义"的确定性客户端错误（400/404/405/413/422 或模型不可用）。

    配额耗尽（402 / insufficient_quota）虽也不该重试，但它归 `is_quota_exhausted`
    管（文案与处置不同），这里不重复归类。
    """
    if is_model_unavailable(exc):
        return True
    status = status_of(exc)
    return status in _DETERMINISTIC_STATUS


def _flatten(exc: BaseException) -> str:
    """把异常压成一段可搜索的文本（含嵌套错误体与 cause 链）。

    不只看 `str(exc)`：OpenAI/Anthropic 的 SDK 把真正的错误码放在
    `exc.body` / `exc.code` / `exc.response` 上，`str(exc)` 往往只有一句笼统的提示。
    """
    parts: list[str] = [type(exc).__name__, str(exc)]
    for attr in ("body", "code", "error", "message", "response", "status_code"):
        value = getattr(exc, attr, None)
        if value is not None:
            parts.append(str(value))
    cause = exc.__cause__
    if cause is not None:
        parts.append(_flatten(cause))
    return " ".join(parts)
