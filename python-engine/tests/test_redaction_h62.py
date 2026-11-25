"""H62：实时输出、错误与日志的脱敏（PRD 9.9）。

缺陷形态：
  * 事件出口（SSE）把工具输出、工具参数、错误正文**原样**发出 —— 一次读 `.env`
    或跑一条带 token 的命令，密钥就会同时出现在界面上、Redis 回放里、导出里；
  * Provider 的原生错误常整段回显请求上下文（`Authorization: Bearer …`、`sk-…`）；
  * 顶层异常文本里带路径/URL/凭据片段。

治法：在**出口**统一走已在用的 `app.subagent.redact.redact_text`（密钥形态 + 运行时
登记凭据双层），而不是各个消费方各自遮掩。脱敏幂等，多处调用安全；脱敏失败原样返回
（不能把"能报错"变成"报不出来"）。
"""

from __future__ import annotations

import pytest

from app.agent.event_sink import SubagentEvent
from app.subagent.redact import redact_text


SECRETS = [
    "sk-abcdefghijklmnopqrstuvwxyz012345",
    "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]


def _event(**kwargs) -> SubagentEvent:
    base = dict(type="subagent.progress", run_id="run-1", depth=0)
    base.update(kwargs)
    return SubagentEvent(**base)


@pytest.mark.parametrize("secret", SECRETS)
def test_event_content_is_redacted(secret: str) -> None:
    payload = _event(content=f"reading .env: OPENAI_API_KEY={secret}").to_payload()
    assert secret not in payload["content"], "工具输出里的密钥必须脱敏后才发出"
    assert "[REDACTED:" in payload["content"]


@pytest.mark.parametrize("secret", SECRETS)
def test_event_error_is_redacted(secret: str) -> None:
    payload = _event(
        type="subagent.tool_result",
        error=f"provider error: 401 invalid key Bearer {secret}",
    ).to_payload()
    assert secret not in payload["error"]


def test_event_tool_arguments_are_redacted() -> None:
    payload = _event(
        type="subagent.approval",
        tool_name="shell_exec",
        tool_arguments='{"cmd": "curl -H \'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345\'"}',
    ).to_payload()
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in payload["tool_arguments"]


def test_redaction_is_idempotent() -> None:
    once, hits = redact_text("key=sk-abcdefghijklmnopqrstuvwxyz012345")
    twice, hits2 = redact_text(once)
    assert once == twice
    assert hits2 == 0
    assert hits >= 1


def test_short_or_clean_text_untouched() -> None:
    clean = _event(content="compiled 3 files successfully").to_payload()
    assert clean["content"] == "compiled 3 files successfully"
    assert "[REDACTED:" not in clean["content"]


def test_empty_fields_are_not_emitted() -> None:
    payload = _event().to_payload()
    assert "content" not in payload
    assert "error" not in payload


def test_runtime_helper_redacts_provider_message() -> None:
    """runtime 的出口助手必须与 sink 同口径（两边不能各脱一半）。"""
    from app.agent.runtime import _redact_error_text

    text = "LLM error: 401 Unauthorized (Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345)"
    out = _redact_error_text(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "401" in out, "脱敏不该吃掉定位问题所需的 status/code"


def test_runtime_helper_never_raises() -> None:
    from app.agent.runtime import _redact_error_text

    assert _redact_error_text("") == ""
    assert _redact_error_text("plain") == "plain"
