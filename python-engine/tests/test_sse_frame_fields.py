"""SSE 帧字段的回归：引擎发出的字段**不许在路上消失**。

这条链是四段序列化：`AgentEvent` → `_event_payload` → 网关 `PythonEvent` →
`broadcast.Event` → 浏览器。任何一段漏字段都不报错，只在最后表现为"某个字段
莫名其妙没了"：清单面板倒退一格、耗时恒不显示、ask 的候选项永远是空的。

所以这里守两件事：

* `_event_payload` 把 `AgentEvent` 上**每一个**被前端消费的字段都带出去；
* 帧的键名与网关 `PythonEvent` 的 JSON 名一致（那边靠 `json:"..."` 反序列化，
  名字对不上同样是静默丢字段）。
"""

from __future__ import annotations

import pytest

from app.agent.runtime_types import AgentEvent
from app.main import _event_payload


def test_payload_carries_every_field_the_frontend_reads():
    """一帧里前端会读的字段，一个都不能少。"""
    event = AgentEvent(
        type="todo_updated",
        content='[{"content":"跑测试","status":"pending"}]',
        revision=7,
        duration_ms=1234,
        model="m",
        input_tokens=11,
        output_tokens=22,
        cached_tokens=33,
    )

    payload = _event_payload(event)

    assert payload["revision"] == 7, "丢了它，前端的乱序防护退化成永远接受"
    assert payload["duration_ms"] == 1234, "丢了它，本轮耗时恒不显示"
    assert payload["content"].startswith("[")
    assert (payload["input_tokens"], payload["output_tokens"]) == (11, 22)
    assert payload["cached_tokens"] == 33
    assert payload["model"] == "m"


def test_error_text_rides_on_content():
    """失败原因放在 `error` 上，但前端只认 `content` 一条通道。"""
    payload = _event_payload(AgentEvent(type="error", error="budget_exceeded:tokens"))

    assert payload["content"] == "budget_exceeded:tokens"


def test_snapshots_are_forwarded_untouched():
    """`done` 上的三份快照原样转发 —— 网关只负责别弄丢。"""
    budget = {"tokens": {"used": 10, "limit": 100}}
    context = {"used": 5, "limit": 50}
    cache = {"prefix_changed": ["system"]}
    event = AgentEvent(type="done", budget=budget, context=context, cache=cache)

    payload = _event_payload(event)

    assert payload["budget"] == budget
    assert payload["context"] == context
    assert payload["cache"] == cache


def test_ask_options_are_carried():
    event = AgentEvent(type="ask", content="选哪个？", options=["A", "B"])

    assert _event_payload(event)["options"] == ["A", "B"]


def test_payload_keys_match_the_gateway_struct():
    """帧键名必须是网关 `PythonEvent` 认识的 JSON 名。

    名字对不上，反序列化时就静默落到零值 —— 与"引擎没发"在链路上无法区分。
    这份清单与 `internal/engine/python_client.go` 的 `PythonEvent` 一一对应；
    在那边加字段时，这里与 `_event_payload` 要一起加。
    """
    known = {
        "type", "content", "id", "name", "arguments", "options",
        "input_tokens", "output_tokens", "cached_tokens", "model", "message",
        "budget", "context", "cache", "revision", "duration_ms",
        "run_id", "parent_run_id", "depth", "profile", "status", "truncated", "usage",
    }

    keys = set(_event_payload(AgentEvent(type="text")))
    unknown = keys - known

    assert not unknown, f"帧里有网关不认识的键，会在反序列化时静默消失：{sorted(unknown)}"


@pytest.mark.parametrize("field", ["revision", "duration_ms"])
def test_the_two_fields_that_used_to_go_missing(field):
    """这两条是**实际漏过**的字段，单独钉住，免得日后又被"顺手精简"掉。"""
    event = AgentEvent(type="text")
    setattr(event, field, 5)

    assert _event_payload(event)[field] == 5
