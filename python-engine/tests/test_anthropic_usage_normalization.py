"""Anthropic 用量归一化（计费准确性的上游）。

## 修的是什么

Anthropic 的 ``usage.input_tokens`` **只算未命中缓存的部分**，命中缓存与写入缓存
分别在 ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` 里 —— 与 OpenAI
「``prompt_tokens`` 已含 ``cached_tokens``」的口径不同。

此前 ``app/providers/anthropic.py`` 直接把 ``usage.input_tokens`` 交给 ChatResponse，
于是走 Anthropic 的长会话：

  * **总输入被少报**（缓存命中常占输入大半）⇒「输入 token」偏低；
  * 成本按 input/output 折算 ⇒ **费用同样偏低**；
  * ``cached_tokens`` 从来没被填过 ⇒ 会话统计的**缓存命中率恒为 0**。

``app/gateway/provider.py`` 里其实早有 ``cached_tokens_from_usage`` 的 Anthropic 分支，
但那个函数是 provider 各自调用的，Anthropic 这侧一直没调。这里补上归一化，
并顺带把「总量」也补对（只补 cached 不补总量，成本仍然偏低）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.providers.anthropic import _anthropic_usage


def test_cached_and_creation_tokens_are_added_back_to_the_total():
    """命中缓存与写入缓存的 token 都要并回总量 —— 否则输入量与成本双双偏低。"""
    usage = SimpleNamespace(
        input_tokens=100,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=200,
        output_tokens=50,
    )

    total_input, cached = _anthropic_usage(usage)

    assert total_input == 1200, "总量必须是 未命中 + 命中 + 写入"
    assert cached == 900, "命中量单独给出（缓存命中率的分子）"


def test_plain_usage_without_cache_fields_is_unchanged():
    """没有缓存字段（或全为 0）时行为不变：不会把 0 加错。"""
    usage = SimpleNamespace(input_tokens=321, output_tokens=7)

    assert _anthropic_usage(usage) == (321, 0)


def test_missing_usage_returns_zeros():
    """取不到 usage 就回 0：统计宁可少算，也不能让整条流式响应失败。"""
    assert _anthropic_usage(None) == (0, 0)


def test_nested_prompt_tokens_details_is_accepted():
    """兼容把命中量放在嵌套 details 里的 SDK 版本（与 OpenAI 口径同形）。"""
    usage = SimpleNamespace(
        input_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=400),
    )

    total_input, cached = _anthropic_usage(usage)

    assert (total_input, cached) == (500, 400)


def test_cache_creation_alone_does_not_count_as_a_cache_hit():
    """写入缓存 ≠ 命中缓存：命中率分子只认 read，不能把 creation 算进去。

    把两者混为一谈会让「缓存命中率」虚高到接近 100%，用户会误以为缓存策略
    非常有效 —— 而实际上每次都是新写入、一次也没读到。
    """
    usage = SimpleNamespace(input_tokens=10, cache_creation_input_tokens=5000)

    total_input, cached = _anthropic_usage(usage)

    assert total_input == 5010
    assert cached == 0, "只写不读不是命中"


def test_negative_or_non_int_values_are_ignored():
    """脏数据不该让总量变负或抛异常。"""
    usage = SimpleNamespace(
        input_tokens=100,
        cache_read_input_tokens=-5,
        cache_creation_input_tokens=None,
    )

    assert _anthropic_usage(usage) == (100, 0)
