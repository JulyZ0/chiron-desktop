"""Tests for message_codec — 中立消息格式与 provider 转换（SaaS 多提供商切换）。"""
from __future__ import annotations

from app.agent.message_codec import (
    auto_normalize,
    detect_format,
    from_openai,
    is_neutral,
    make_message,
    to_chat_messages,
    to_openai,
)


def _neutral_sample():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": '{"path":"a.txt"}'}]},
        {"role": "tool", "tool_call_id": "c1", "content": "data"},
    ]


class TestNeutralFormat:
    def test_make_message_normalizes_openai_wrapped_tool_calls(self):
        """OpenAI 包装 {type,function:{...}} 自动中立化（历史缓存兼容）。"""
        msg = make_message(
            role="assistant", content="",
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "shell_exec", "arguments": '{"command":"ls"}'}}],
        )
        assert msg["tool_calls"] == [{"id": "c1", "name": "shell_exec", "arguments": '{"command":"ls"}'}]
        assert "function" not in msg["tool_calls"][0]

    def test_neutral_has_no_provider_wrapping(self):
        """中立格式不含 type/function 包装（provider-agnostic 保证）。"""
        for m in _neutral_sample():
            for tc in (m.get("tool_calls") or []):
                assert "function" not in tc
                assert "type" not in tc

    def test_is_neutral(self):
        assert is_neutral(_neutral_sample()) is True
        legacy = [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {}}]}]
        assert is_neutral(legacy) is False


class TestOpenAIAdapter:
    def test_to_openai_adds_wrapping(self):
        out = to_openai(_neutral_sample())
        tc = next(m for m in out if m.get("role") == "assistant")["tool_calls"][0]
        assert tc == {"id": "c1", "type": "function",
                      "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}

    def test_round_trip_openai(self):
        """OpenAI → 中立 → OpenAI 无损（provider 切换可逆）。"""
        openai_msgs = to_openai(_neutral_sample())
        back = to_openai(from_openai(openai_msgs))
        assert back == openai_msgs

    def test_tool_message_preserves_tool_call_id(self):
        out = to_openai(_neutral_sample())
        tool_msg = next(m for m in out if m.get("role") == "tool")
        assert tool_msg["tool_call_id"] == "c1"
        assert tool_msg["content"] == "data"


class TestGatewayAdapter:
    def test_to_chat_messages_builds_chatmessage(self):
        msgs = to_chat_messages(_neutral_sample())
        assert msgs[0].role == "system" and msgs[0].content == "sys"
        asst = msgs[2]
        assert asst.tool_calls is not None
        assert asst.tool_calls[0].name == "read_file"
        assert asst.tool_calls[0].arguments == '{"path":"a.txt"}'
        assert msgs[3].tool_call_id == "c1"


class TestAutoDetect:
    """自动推断市场格式并统一中立（SaaS：多提供商来源）。"""

    def _openai_sample(self):
        return [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "data"},
        ]

    def _anthropic_sample(self):
        return [
            {"role": "assistant", "content": [
                {"type": "text", "text": "调用工具"},
                {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "a.txt"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "data"},
            ]},
        ]

    def _gemini_sample(self):
        return [
            {"role": "model", "content": [
                {"text": "调用工具"},
                {"functionCall": {"id": "c1", "name": "read_file", "args": {"path": "a.txt"}}},
            ]},
            {"role": "user", "content": [
                {"functionResponse": {"id": "c1", "response": {"ok": True}}},
            ]},
        ]

    def test_detect_openai(self):
        assert detect_format(self._openai_sample()) == "openai"

    def test_detect_anthropic(self):
        assert detect_format(self._anthropic_sample()) == "anthropic"

    def test_detect_gemini(self):
        assert detect_format(self._gemini_sample()) == "gemini"

    def test_detect_neutral(self):
        assert detect_format(_neutral_sample()) == "neutral"

    def test_auto_normalize_anthropic(self):
        """Anthropic blocks → 中立：tool_use 拆为 tool_calls，tool_result 拆为独立 tool 消息。"""
        out = auto_normalize(self._anthropic_sample())
        asst = next(m for m in out if m.get("role") == "assistant")
        assert asst["tool_calls"] == [{"id": "c1", "name": "read_file", "arguments": '{"path": "a.txt"}'}]
        tool = next(m for m in out if m.get("role") == "tool")
        assert tool["tool_call_id"] == "c1" and tool["content"] == "data"

    def test_auto_normalize_openai_roundtrip(self):
        """OpenAI → 中立 → OpenAI 无损（自动推断入口）。"""
        neutral = auto_normalize(self._openai_sample())
        assert to_openai(neutral) == self._openai_sample()

    def test_auto_normalize_gemini(self):
        """Gemini functionCall/functionResponse → 中立。"""
        out = auto_normalize(self._gemini_sample())
        asst = next(m for m in out if m.get("role") == "assistant")
        assert asst["tool_calls"][0]["name"] == "read_file"
        tool = next(m for m in out if m.get("role") == "tool")
        assert '"ok": true' in tool["content"]
