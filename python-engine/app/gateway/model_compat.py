"""模型 ↔ 服务提供商**兼容判定**（路由前的一道硬约束）。

## 为什么需要

`GatewayRouter._find_candidates` 只按 `model_prefixes` 匹配；**匹配不到时退回"全部
provider"**，再由加权选择（成本优先）挑一个。"全部"里包含**订阅制聚合网关**（OpenCode
Go / Go Plus），而它们的套餐只覆盖一份**模型清单**。于是：

    settings.default_model = claude-sonnet-4-20250514（Anthropic 直连模型）
    ⇣ 子 Agent 的 llm_config 没有 provider，只有默认模型
    ⇣ opencode-go 成本 0.4（其它 provider 3~5）→ 加权随机几乎必选它
    ⇣ 上游 400: "claude-sonnet-4-20250514 isn't included in OpenCode Go"

现场后果是**子 Agent 全部 failed**，而主 Agent 正常（主 Agent 的 llm_config 带 provider）。
同一份模型配置在两处走了不同的路由，这是本模块要消掉的那条缝。

## 判定顺序（保守优先）

1. **套餐白名单**（`plan_allowlist`）：该 provider 声明了"只支持这些模型族"时，
   不在清单里的一律判为不支持。清单来源优先级：
   目录项 `model_allowlist` > 设置项 `provider_model_allowlist[provider]` >
   本模块的 `DEFAULT_PLAN_ALLOWLIST`。
2. 目录项 `model_deny_prefixes` / 设置项 `provider_model_deny[provider]`：显式禁用。
3. 其余 provider 视为"可接受任意模型"（直连 provider 由上游自己校验）。

**缺省是保守的**：`opencode-go` / `opencode-go-anthropic` 的默认白名单**不含 `claude`**。
套餐里确实有 `claude-haiku-*` 之类的模型，但那需要用户显式配置才放行 —— 宁可让一次
可配置的路由失败（错误信息会写清怎么配），也不要默认把 Anthropic 直连模型发给订阅网关。

白名单的权威来源是 <https://opencode.ai/inference/go/v1/models>（`GET .../models`）；
套餐会变，所以这里（以及目录/设置）必须保持可配置，而不是把清单硬编码成事实。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 订阅套餐**默认**覆盖的模型族（OpenCode Go，按 2026-11 的套餐清单归纳为前缀）。
#: 用前缀而不是精确 id：`glm-5.3` / `glm-5.3-flash` 这类小版本变化很频繁。
DEFAULT_PLAN_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "opencode-go": (
        "glm",
        "kimi",
        "qwen",
        "minimax",
        "deepseek",
        "grok",
        "gpt-oss",
        "gpt-5",
        "gpt-6",
        "gemini",
        "longcat",
        "mimo",
        "muse",
        "hy",
        "step",
        "beam",
        "iceman",
    ),
}
#: `opencode-go-anthropic` 与 `opencode-go` 是同套餐的两个协议端点（共享一把 key）。
DEFAULT_PLAN_ALLOWLIST["opencode-go-anthropic"] = DEFAULT_PLAN_ALLOWLIST["opencode-go"]

#: 模型族 → 该模型**直连**所需的 provider id（fail-fast 文案用）
DIRECT_PROVIDER_BY_FAMILY: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "deepseek": "deepseek",
    "moonshot": "moonshot",
    "zhipu": "zhipu",
    "dashscope": "dashscope",
    "minimax": "minimax",
    "xai": "xai",
}


def model_family(model: str) -> str:
    """按模型名判族（只用于文案与默认路由，不参与计费/能力判定）。"""
    name = str(model or "").lower()
    if "claude" in name:
        return "anthropic"
    if "gemini" in name or name.startswith("gemma"):
        return "google"
    if "deepseek" in name:
        return "deepseek"
    if name.startswith(("gpt", "o1", "o3", "o4", "text-embedding", "davinci")):
        return "openai"
    if "kimi" in name or "moonshot" in name:
        return "moonshot"
    if "glm" in name or "chatglm" in name:
        return "zhipu"
    if "qwen" in name or "qwq" in name:
        return "dashscope"
    if name.startswith(("minimax", "abab")):
        return "minimax"
    if "grok" in name:
        return "xai"
    return ""


def _settings_list(mapping_name: str, provider: str) -> list[str] | None:
    """读设置的覆盖表（`{provider: [prefix, ...]}`）；未配置返回 `None`。

    未配置与"配置成空列表"语义不同：后者表示**显式禁用该 provider 的所有模型**。
    """
    try:
        from app.config import settings

        raw = getattr(settings, mapping_name, None) or {}
        if not isinstance(raw, dict):
            return None
        value = raw.get(provider)
        if value is None:
            return None
        if isinstance(value, str):
            return [p.strip().lower() for p in value.split(",") if p.strip()]
        return [str(p).strip().lower() for p in value if str(p).strip()]
    except Exception:  # noqa: BLE001 - 配置读取失败不得让路由失败
        return None


def _preset_list(preset: dict[str, Any] | None, key: str) -> list[str] | None:
    if not isinstance(preset, dict):
        return None
    raw = preset.get(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        return [p.strip().lower() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(p).strip().lower() for p in raw if str(p).strip()]
    return None


def plan_allowlist(provider: str, preset: dict[str, Any] | None = None) -> list[str] | None:
    """该 provider 的**套餐模型白名单**（`None` = 不做限制）。"""
    for candidate in (
        _preset_list(preset, "model_allowlist"),
        _settings_list("provider_model_allowlist", provider),
    ):
        if candidate is not None:
            return candidate
    default = DEFAULT_PLAN_ALLOWLIST.get(provider)
    return list(default) if default is not None else None


def deny_prefixes(provider: str, preset: dict[str, Any] | None = None) -> list[str]:
    """显式禁用前缀（命中即不支持，优先于白名单）。"""
    for candidate in (
        _preset_list(preset, "model_deny_prefixes"),
        _settings_list("provider_model_deny", provider),
    ):
        if candidate is not None:
            return candidate
    return []


def provider_supports_model(
    provider: str, preset: dict[str, Any] | None, model: str
) -> bool:
    """该 provider 能否接收这个模型名。

    * 命中显式禁用 → `False`
    * 有套餐白名单 → 必须命中其中一个前缀，否则 `False`
    * 无白名单 → `True`（直连 provider 交给上游校验；这里不猜它的模型清单）
    """
    name = str(model or "").strip().lower()
    if not name:
        return True
    for prefix in deny_prefixes(provider, preset):
        if prefix and prefix in name:
            return False
    allow = plan_allowlist(provider, preset)
    if allow is None:
        return True
    return any(prefix in name for prefix in allow if prefix)


def incompatible_message(
    model: str, provider: str, available: list[str], *, configured: list[str]
) -> str:
    """给出**可行动**的一句话：为什么没路由到某个 provider、以及该配什么。

    刻意点名"模型 X 需要 provider Y" ——用户配的是模型，而失败原因是 provider，
    不把这两个词放进同一句话，用户就只能去猜。
    """
    family = model_family(model)
    needed = DIRECT_PROVIDER_BY_FAMILY.get(family, "")
    allow = plan_allowlist(provider)
    parts = [
        f"model {model} is not supported by provider {provider}"
        + (f" (plan allowlist: {', '.join(allow[:8])}…)" if allow else "")
    ]
    if needed and needed not in configured:
        parts.append(
            f"该模型需要 provider={needed}（直连），当前未配置 —— "
            f"请在「管理后台 → API 密钥」为 {needed} 添加密钥"
        )
    elif needed:
        parts.append(f"该模型需要 provider={needed}，但它当前不可用（熔断/鉴权/额度）")
    if available:
        parts.append(f"可用 provider={', '.join(available)}")
    else:
        parts.append(
            "当前**没有**任何 provider 能接收该模型；请改选套餐内的模型或配置对应 provider"
        )
    return "；".join(parts)
