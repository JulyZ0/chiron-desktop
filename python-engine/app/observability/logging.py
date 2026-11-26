# 结构化日志 — structlog + trace_id 注入
#
# **必须同时配置 stdlib root**：全仓模块用的是 `logging.getLogger(__name__)`，
# 只调 `structlog.configure()` 不会给这些 logger 装任何 handler —— info 被丢弃、
# warning/error 落到 logging.lastResort（纯文本、无时间戳、无 trace_id）。
# 这里用 `ProcessorFormatter` 把 stdlib 记录统一送进 structlog 的 JSON 渲染管线。
from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# 请求上下文变量（middleware 层注入）
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
tenant_id_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "tenant_id", default=0
)
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="")


def _add_request_context(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """注入 request_id / tenant_id / trace_id 到每条日志"""
    req_id = request_id_var.get("")
    if req_id:
        event_dict["request_id"] = req_id

    tid = tenant_id_var.get("")
    if tid:
        event_dict["tenant_id"] = tid

    trace = trace_id_var.get("")
    if trace:
        event_dict["trace_id"] = trace

    span = span_id_var.get("")
    if span:
        event_dict["span_id"] = span

    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """配置 structlog + stdlib root，输出 JSON 到 stdout。

    幂等：重复调用会替换 root handler，不会叠加多份输出。
    """
    level_no = getattr(logging, level.upper(), logging.INFO)

    # 进入渲染器之前的公共处理（stdlib 记录也走 foreign_pre_chain）。
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _add_request_context,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # 交给下面 ProcessorFormatter 的处理器链统一渲染；不在这里直接
            # JSONRenderer，否则 stdlib 与 structlog 两条路径的字段会不一致。
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # 非 structlog 记录（app.* / uvicorn.*）先补齐 level/时间/上下文。
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_no)

    # uvicorn 默认给自身的 logger 装 handler 且 propagate=false；不摘掉就会
    # 出现"同一条请求两份日志、一份非 JSON"。统一交给 root。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
