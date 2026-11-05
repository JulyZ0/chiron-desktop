"""M28：按模型的公开价格 —— 解析、归一化、交叉验证。

三条不变量：

1. **单位统一到 USD / 1000 tokens**：各源单位不同（portkey = 美分/token；llmrates = 每百万
   token + 原生货币），换算只在各自的解析函数里做一次；
2. **至少两个源一致（相对差 ≤ 5%）才收**：单源不写入、分歧不写入 —— 宁可缺数据，也不要
   一个来源不明或自相矛盾的数字；
3. **非 USD 跳过**（本仓不做汇率换算，价格表也不参与计费，没有必须换算的压力）。

payload 片段取自两个源的真实响应（2026-10-22 实测）。
"""
from __future__ import annotations

import pytest

from app.pricing import sync as s

# ── 解析：真实 payload 的形状 ──


def test_portkey_cents_per_token_becomes_usd_per_1k() -> None:
    """portkey 的单位是**美分 / token**（其文档自陈）⇒ USD/1k = 美分 × 10。"""
    payload = {
        "gpt-4o": {
            "pricing_config": {
                "pay_as_you_go": {
                    "request_token": {"price": 0.00025},   # 美分/token ⇒ $2.5/1M
                    "response_token": {"price": 0.001},
                }
            }
        }
    }

    prices = s.parse_portkey(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_per_million_becomes_usd_per_1k() -> None:
    payload = {
        "models": [
            {
                "name": "gpt-4o",
                "provider": {"slug": "openai"},
                "prices": [{"inputPricePerMillion": 2.5, "outputPricePerMillion": 10, "priceUnit": "USD"}],
            }
        ]
    }

    prices = s.parse_llmrates(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_skips_non_usd() -> None:
    """非 USD 一律跳过（不做汇率换算）—— 否则价格表里会混进"看起来是美元"的人民币。"""
    payload = {
        "models": [
            {
                "name": "glm-4v",
                "provider": {"slug": "zhipu"},
                "prices": [{"inputPricePerMillion": 10, "outputPricePerMillion": 10, "priceUnit": "CNY"}],
            }
        ]
    }

    assert s.parse_llmrates(payload) == {}


def test_model_name_normalization_drops_date_suffix() -> None:
    assert s.normalize_model("GPT-4o-2024-11-20") == "gpt-4o"
    assert s.normalize_model("claude_3.5_sonnet") == "claude-3-5-sonnet"


# ── 交叉验证 ──


def _price(model: str, inp: float, outp: float) -> s.ModelPrice:
    return s.ModelPrice(model=model, provider="openai", input_per_1k=inp, output_per_1k=outp)


def test_two_sources_within_tolerance_agree() -> None:
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.0026, 0.0101)}}
    )

    assert [p.model for p in agreed] == ["gpt-4o"]
    assert disagreements == []


def test_disagreement_is_not_written_but_is_reported() -> None:
    """分歧**不写入**，但**不静默** —— 否则"价格表一直是空的"没人知道为什么。"""
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.05, 0.01)}}
    )

    assert agreed == []
    assert len(disagreements) == 1 and "gpt-4o" in disagreements[0]


def test_output_axis_alone_is_enough_to_disagree() -> None:
    """两轴都要比：只比输入会让"输出价差一倍"溜过去。"""
    agreed, _ = s.cross_validate(
        {"a": {"m": _price("m", 0.0025, 0.01)}, "b": {"m": _price("m", 0.0025, 0.5)}}
    )

    assert agreed == []


def test_single_source_is_never_written() -> None:
    """只有一个源 ⇒ 判未验证、不写入（单源无法交叉验证）。"""
    agreed, disagreements = s.cross_validate({"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}})

    assert agreed == []
    assert disagreements == []


def test_model_names_that_do_not_align_become_single_source() -> None:
    """两个源对同一个模型的叫法差得远 ⇒ 对齐不上 ⇒ 各自单源 ⇒ 都不写入。

    这是**有意的保守**：拿两个不同模型的价格互相"验证"比没有数据更糟。
    """
    agreed, _ = s.cross_validate(
        {"a": {"glm-4v": _price("glm-4v", 0.01, 0.01)}, "b": {"glm-4-vision": _price("glm-4-vision", 0.01, 0.01)}}
    )

    assert agreed == []


# ── 开关 ──


def test_sync_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICE_SYNC", raising=False)

    assert s.sync_enabled() is False


def test_interval_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "abc")
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS

    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "5")  # 太小 ⇒ 回落
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS


async def test_start_price_sync_returns_immediately_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认关 ⇒ 调用方可以无条件 await 它，不必自己判开关。"""
    monkeypatch.delenv("MODEL_PRICE_SYNC", raising=False)

    await s.start_price_sync()  # 不会挂起


async def test_sync_once_writes_nothing_when_no_source_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有源都拉不到 ⇒ 一行都不写，且**不抛**（一轮失败不该让循环死掉）。"""

    async def _dead(_name: str, _url: str, **_kw: object) -> None:
        return None

    monkeypatch.setattr(s, "_fetch_source", _dead)
    written, disagreements = await s.sync_once()

    assert written == 0 and disagreements == []


# ── PriceToken（2026-11-06 实测响应形状）──


def test_pricetoken_per_million_becomes_usd_per_1k() -> None:
    """字段名是 ``*PerMTok`` ⇒ USD/1k = per_million / 1000。"""
    payload = {
        "data": [
            {
                "modelId": "claude-haiku-4-5-20251001",
                "provider": "anthropic",
                "inputPerMTok": 1,
                "outputPerMTok": 5,
                "status": "active",
            }
        ]
    }

    prices = s.parse_pricetoken(payload)

    key = s.normalize_model("claude-haiku-4-5-20251001")
    assert prices[key].input_per_1k == pytest.approx(0.001)
    assert prices[key].output_per_1k == pytest.approx(0.005)
    assert prices[key].provider == "anthropic"


def test_pricetoken_skips_non_active_models() -> None:
    """退役/预览模型不写进价格表：否则界面会显示一个已经不能用的模型的价格。"""
    payload = {
        "data": [
            {"modelId": "old-model", "provider": "openai", "inputPerMTok": 1,
             "outputPerMTok": 2, "status": "deprecated"},
            {"modelId": "new-model", "provider": "openai", "inputPerMTok": 1,
             "outputPerMTok": 2, "status": "active"},
        ]
    }

    prices = s.parse_pricetoken(payload)

    assert s.normalize_model("old-model") not in prices
    assert s.normalize_model("new-model") in prices


# ── Portkey 多厂商文件：provider 必须跟着文件走 ──


def test_portkey_provider_follows_the_file() -> None:
    """同一个解析器跑 4 份厂商文件；provider 若写死 "openai"，
    写进 `model_prices` 的 anthropic 模型会全部被标成 openai。"""
    payload = {
        "claude-3-5-sonnet-20241022": {
            "pricing_config": {
                "pay_as_you_go": {
                    "request_token": {"price": 0.0003},
                    "response_token": {"price": 0.0015},
                }
            }
        }
    }

    prices = s.parse_portkey(payload, provider="anthropic")

    key = s.normalize_model("claude-3-5-sonnet-20241022")
    assert prices[key].provider == "anthropic"


def test_portkey_skips_all_zero_placeholder() -> None:
    """各厂商文件都有一个 ``"default"`` 全零占位键，必须被跳过。"""
    payload = {
        "default": {
            "pricing_config": {
                "pay_as_you_go": {"request_token": {"price": 0}, "response_token": {"price": 0}}
            }
        }
    }

    assert s.parse_portkey(payload) == {}


# ── 源目录：同一解析器的多份文件不能互相覆盖 ──


async def test_sync_once_keeps_every_portkey_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`portkey` 在源目录里出现多次（按厂商分文件）。

    若用解析器名做 `fetched` 的键，四次拉取会互相覆盖 —— 表现是"价格表里只剩最后一个
    厂商"。这里断言各厂商的模型都进到了交叉验证的输入里。
    """
    seen_urls: list[str] = []

    async def _fake(name: str, url: str, *, provider: str = "", **_kw: object):
        seen_urls.append(url)
        if name != "portkey" or not provider:
            return None
        model = f"{provider}-only"
        return {model: _price(model, 0.001, 0.002)}

    captured: dict[str, object] = {}

    def _fake_cross(per_source, **_kw):
        captured["keys"] = sorted(per_source)
        return [], []

    monkeypatch.setattr(s, "_fetch_source", _fake)
    monkeypatch.setattr(s, "cross_validate", _fake_cross)

    await s.sync_once()

    keys = captured["keys"]
    # 4 个 portkey 文件各自一个唯一键（解析器名:provider）
    assert keys == ["portkey:anthropic", "portkey:deepseek", "portkey:google", "portkey:openai"]
