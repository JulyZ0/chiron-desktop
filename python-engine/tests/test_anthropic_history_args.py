"""历史里的 `tool_calls[].arguments` 不是合法 JSON 时，Anthropic 路径**不能整轮失败**。

**这个文件钉住的缺陷（修复前为红）**

用户侧现象：

    provider opencode-go-anthropic stream failed: JSONDecodeError:
    Unterminated string starting at: line 1 column 1150 (char 1149)

`Unterminated string` 是**半截 JSON** 的典型错误。半截 arguments 有两条现实来源：

1. `finish_reason=length` —— 模型的输出被 `max_tokens` 截断，`arguments` 写到一半就没了；
2. 压缩链（`media_offload.truncate_tool_call_arguments`）截断历史参数。

它们都会被写进历史，而**历史每轮都会重发**。`AnthropicProvider._convert_messages`
原先对每条历史 `tool_call` 无条件 `json.loads(tc.arguments)`，于是：

* 请求在**发出之前**就抛 `JSONDecodeError`（所以错误里没有任何 HTTP 迹象）；
* 没有任何自愈路径 —— 坏参数留在历史里，此后**每一轮**都再炸一次，
  会话直接不可用（用户只能新开会话）。

修法（`providers/anthropic.py`）：

* `_tool_arguments_to_input`：合法对象原样；数组/标量包 `_raw`（协议要求 input 是 object）；
  空串 → `{}`；**坏 JSON → 带 `_raw`/`_note` 的占位 object**（不抛、也不丢 —— 丢了会让
  `tool_use` / `tool_result` 配对断裂，同样 400）。
* `_convert_tools`：`func.get("parameters", json.loads(...))` 的默认值是**提前求值**的，
  哪怕 `parameters` 存在，坏的 `parameters_json` 也会炸。改用 `_schema_to_input_schema`。
"""

from __future__ import annotations

import json

from app.gateway.provider import ChatMessage, ToolCall
from app.providers.anthropic import AnthropicProvider

convert_messages = AnthropicProvider._convert_messages
convert_tools = AnthropicProvider._convert_tools

#: 被 max_tokens 截断的真实形态（`Unterminated string` 就是这么来的）
TRUNCATED = '{"path": "a.txt", "content": "hel'


def _history(arguments: str) -> list[ChatMessage]:
    """assistant(tool_use) + 配对的 tool_result —— 一条合法的历史往返。"""
    return [
        ChatMessage(role="user", content="write a file"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="write_file", arguments=arguments)],
        ),
        ChatMessage(role="tool", content="error: truncated", tool_call_id="call_1"),
    ]


def _tool_use_input(messages: list[ChatMessage]) -> dict:
    _system, converted = convert_messages(messages)
    assistant = next(m for m in converted if m["role"] == "assistant")
    return next(block for block in assistant["content"] if block["type"] == "tool_use")["input"]


def test_truncated_arguments_do_not_raise():
    """核心回归：修复前这里抛 `JSONDecodeError: Unterminated string`。"""
    value = _tool_use_input(_history(TRUNCATED))

    assert isinstance(value, dict), "Anthropic 要求 tool_use.input 是 object"
    assert "a.txt" in json.dumps(value, ensure_ascii=False), "原文头部要保留，模型才认得出"


def test_truncated_arguments_are_marked_as_not_executed():
    value = _tool_use_input(_history(TRUNCATED))

    assert value.get("_note"), "必须标注这不是一份可用的参数"


def test_tool_use_stays_paired_with_tool_result():
    """不能靠"丢掉坏的那条"来止血 —— 配断裂同样是 400，且丢上下文。"""
    _system, converted = convert_messages(_history(TRUNCATED))

    tool_use_ids = [
        block["id"]
        for m in converted
        if m["role"] == "assistant"
        for block in m["content"]
        if block["type"] == "tool_use"
    ]
    tool_result_ids = [
        block["tool_use_id"]
        for m in converted
        if m["role"] == "user" and isinstance(m["content"], list)
        for block in m["content"]
        if block["type"] == "tool_result"
    ]
    assert tool_use_ids == ["call_1"] == tool_result_ids


def test_valid_object_arguments_pass_through_unchanged():
    """正对照：正常参数必须**原样**进 input（别把修复写成一律包一层）。"""
    args = json.dumps({"path": "a.txt", "content": "hello"})
    assert _tool_use_input(_history(args)) == {"path": "a.txt", "content": "hello"}


def test_empty_arguments_become_empty_object():
    assert _tool_use_input(_history("")) == {}


def test_non_object_json_is_wrapped_not_dropped():
    """合法但不是 object（数组）：协议要 object，包一层 `_raw` 保住信息。"""
    value = _tool_use_input(_history("[1, 2]"))

    assert isinstance(value, dict)
    assert "_raw" in value


def test_skeleton_output_roundtrips_through_anthropic(monkeypatch):
    """压缩链的产出直接喂给 provider —— 端到端不再炸。

    这是两条链路真正相撞的地方：截断写历史（`media_offload`）→ 下一轮重发
    （`_convert_messages`）。单独修任一侧都还不够。
    """
    from app.agent.media_offload import truncate_tool_call_arguments
    from app.agent.runtime import CompactionConfig

    long_args = json.dumps({"path": "a.txt", "content": "y" * 5000})
    history = truncate_tool_call_arguments(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "write_file", "arguments": long_args}],
            }
        ],
        CompactionConfig(arg_max_chars=200),
    )
    truncated_args = history[0]["tool_calls"][0]["arguments"]

    # 截断后的参数本身就必须是合法 JSON（否则它进不了任何 provider）
    json.loads(truncated_args)
    # 且经 Anthropic 转换仍然可用
    value = _tool_use_input(_history(truncated_args))
    assert isinstance(value, dict)


# ── 工具定义侧：坏 `parameters_json` 同样不能整轮失败 ────────────────────


def test_bad_parameters_json_does_not_raise():
    """`func.get("parameters", json.loads(parameters_json))` 的默认值提前求值 —— 会炸。"""
    converted = convert_tools(
        [{"function": {"name": "w", "parameters": {"type": "object"}, "parameters_json": '{"a":'}}]
    )

    # `parameters` 存在 ⇒ 用它，坏掉的 `parameters_json` 不参与
    assert converted[0]["input_schema"] == {"type": "object"}


def test_bad_parameters_json_without_parameters_falls_back():
    converted = convert_tools([{"name": "w", "parameters_json": "not json"}])

    assert converted[0]["input_schema"] == {"type": "object", "properties": {}}


def test_valid_parameters_json_is_parsed():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    converted = convert_tools([{"name": "w", "parameters_json": json.dumps(schema)}])

    assert converted[0]["input_schema"] == schema
