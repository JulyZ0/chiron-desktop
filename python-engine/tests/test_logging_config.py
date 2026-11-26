"""L04：stdlib logging 必须真的输出结构化 JSON（并带上 trace_id）。

修前 `configure_logging()` 只调 `structlog.configure()`，而全仓用的是
`logging.getLogger(__name__)` —— 没有 handler，info 被丢弃、warning 走 lastResort。
这个用例直接对 root 打一条 stdlib 日志，断言能解析出 JSON 与上下文。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.observability import logging as obs


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


def test_stdlib_logging_is_json_with_context(capsys) -> None:
    obs.configure_logging("INFO")
    obs.trace_id_var.set("trace-123")
    obs.tenant_id_var.set(7)

    logging.getLogger("app.test").info("hello %s", "world")

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "stdlib 日志没有输出到 stdout"
    record = json.loads(out[-1])
    assert record["event"] == "hello world"
    assert record["level"] == "info"
    assert record["trace_id"] == "trace-123"
    assert record["tenant_id"] == 7
    assert "timestamp" in record


def test_reconfigure_does_not_duplicate_handlers(capsys) -> None:
    obs.configure_logging("INFO")
    obs.configure_logging("INFO")
    logging.getLogger("app.test").info("once")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, f"重复 configure 产生了多份输出: {lines}"
