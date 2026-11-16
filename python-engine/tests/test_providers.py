"""LLM Provider 流式解析测试"""
from unittest.mock import AsyncMock, MagicMock

from app.providers.openai import OpenAIProvider

# ── mock OpenAI chunk 结构 ───────────────────────────────


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Delta:
    def __init__(self, reasoning_content=None, content=None, tool_calls=None):
        self.reasoning_content = reasoning_content
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


def _make_provider(chunks):
    """构造一个 chat.completions.create 返回给定 chunk 序列的 OpenAIProvider"""
    provider = OpenAIProvider(api_key="test-key")

    async def fake_chunks():
        for c in chunks:
            yield c

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=fake_chunks())
    provider._client = client
    return provider


async def _collect(provider):
    result = []
    async for c in provider.chat_stream(messages=[], model="deepseek-chat"):
        result.append(c)
    return result


# ── 测试 ─────────────────────────────────────────────────


async def test_usage_yielded_only_once_when_finish_chunk_carries_usage():
    """DeepSeek 标准流：最后一个 chunk 同时携带 finish_reason 与 usage。

    usage 必须只 yield 一次（token 用量/计费依赖累加语义），
    重复 yield 会让调用方把 token 统计翻倍。
    """
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(reasoning_content="思考二"))]),
        _Chunk([_Choice(_Delta(content="最终回答"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    input_total = sum(c.input_tokens for c in chunks)
    output_total = sum(c.output_tokens for c in chunks)
    assert input_total == 10, f"input_tokens 应只累计一次（10），实际 {input_total}"
    assert output_total == 20, f"output_tokens 应只累计一次（20），实际 {output_total}"


async def test_usage_yielded_once_when_usage_in_separate_chunk():
    """usage 单独成 chunk（无 choices 无 finish_reason）时也要恰好 yield 一次。"""
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")]),
        _Chunk([], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    input_total = sum(c.input_tokens for c in chunks)
    output_total = sum(c.output_tokens for c in chunks)
    assert input_total == 10, f"input_tokens 应只累计一次（10），实际 {input_total}"
    assert output_total == 20, f"output_tokens 应只累计一次（20），实际 {output_total}"


async def test_reasoning_content_yielded_as_increments_only():
    """reasoning_content 必须以增量 yield：调用方（runtime.py）按增量累加，
    若末尾再 yield 一次完整累积文本，思考内容会被重复拼接到 2~3 倍。
    """
    provider = _make_provider([
        _Chunk([_Choice(_Delta(reasoning_content="思考一"))]),
        _Chunk([_Choice(_Delta(reasoning_content="思考二"))]),
        _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(prompt_tokens=10, completion_tokens=20)),
    ])

    chunks = await _collect(provider)

    parts = [c.reasoning_content for c in chunks if c.reasoning_content]
    assert parts == ["思考一", "思考二"], (
        f"reasoning_content 应按增量逐段 yield（每段只出现一次），实际 {parts!r}"
    )


# ── 用量请求：`stream_options.include_usage` ──────────────


async def test_stream_requests_usage_explicitly():
    """★★ 流式请求必须显式索要用量，否则 token 统计恒为 0。

    缺陷形态（实测）：`chat_stream` 只发 `stream=True`。OpenAI 兼容族的流式响应
    **默认不返回 usage** —— `chunk.usage` 恒为 None，于是 `input_tokens/output_tokens`
    一路保持 0，`done` 事件带 0/0/0，网关落库成"45 行调用、token 全 0"，
    计费页的输入/输出/缓存三列永远显示 0。

    判据：发给上游的 kwargs 里必须有 `stream_options == {"include_usage": True}`。
    """
    from app.providers.openai import OpenAIProvider  # 局部导入：本文件顶部已导入同名符号

    provider = _make_provider([
        _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(10, 20)),
    ])
    await _collect(provider)

    kwargs = provider._client.chat.completions.create.call_args.kwargs
    assert kwargs.get("stream") is True
    assert kwargs.get("stream_options") == {"include_usage": True}, (
        "流式请求没要 usage —— 上游不会返回 token 计数，统计与计费会永远是 0"
    )
    assert provider is not None and OpenAIProvider is not None  # 保持导入被使用


async def test_stream_usage_degrades_when_upstream_rejects_it():
    """★★ 上游不认 `stream_options` 时**不能**让整轮对话失败。

    部分自建网关/第三方代理会在收到 `stream_options` 时直接 400。少一份用量统计
    远好过用不了 —— 所以要降级重试一次（去掉该参数），并把 (provider, model)
    记进 `_WITHOUT_STREAM_USAGE`，后续请求不再发它（避免每回合都先撞一次 400）。

    两条判据同时成立：请求**最终成功**，且第二次调用不再带 `stream_options`。
    """
    from app.providers import openai as mod

    mod._WITHOUT_STREAM_USAGE.discard(("openai", "rejects-usage-model"))

    async def fake_chunks():
        yield _Chunk([_Choice(_Delta(), finish_reason="stop")], usage=_Usage(10, 20))

    calls: list[dict] = []

    class _BadRequest(Exception):  # noqa: N818 — 镜像 SDK 的 `BadRequestError` 命名，测试内局部类，改成 Error 后缀反而与真实异常名脱节
        """模拟 openai SDK 的 `BadRequestError`：带 `status_code` 与可读文本。"""

        status_code = 400

        def __init__(self, msg: str) -> None:
            super().__init__(msg)

    async def _create(**kwargs):
        calls.append(dict(kwargs))
        if "stream_options" in kwargs:
            raise _BadRequest(
                "Error code: 400 - {'error': 'Unrecognized request argument: stream_options'}"
            )
        return fake_chunks()

    provider = OpenAIProvider(api_key="test-key")
    client = MagicMock()
    client.chat.completions.create = _create
    provider._client = client

    chunks = []
    async for c in provider.chat_stream(messages=[], model="rejects-usage-model"):
        chunks.append(c)

    assert len(calls) == 2, f"应当「先失败、降级后成功」共 2 次调用，实际 {len(calls)}"
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1], "降级后不该再发上游不认的参数"
    assert ("openai", "rejects-usage-model") in mod._WITHOUT_STREAM_USAGE, (
        "降级必须被记住 —— 否则每个回合都要先撞一次 400"
    )
    assert chunks, "降级后必须正常产出 chunk（对话不能失败）"

    mod._WITHOUT_STREAM_USAGE.discard(("openai", "rejects-usage-model"))
