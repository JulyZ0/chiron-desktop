"""Go 与 Python 共享的版本化线上契约（H16）。

与 `internal/contract/contract.go` 是**同一份判据的两个实现**：两侧都跑
`contracts/fixtures/conformance.json` 里的同一组用例。任何一侧改判据而另一侧没跟上，
测试就会红 —— 这正是"契约"与"文档"的区别。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 契约版本。删/改字段必须同时升版本并改两端；只加可选字段不用升。
VERSION = 1

FAMILY_ERROR = "error"
FAMILY_TERMINAL = "terminal"

_TERMINAL_TYPES = {"done", "error", "cancelled"}
_MAX_MESSAGE_BYTES = 4096

_FIXTURES = (
    Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "conformance.json"
)


@dataclass(frozen=True)
class Violation:
    """一处违约（带字段路径，便于定位）。"""

    family: str
    field: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于报错可读
        return f"{self.family}.{self.field}: {self.reason}" if self.field else f"{self.family}: {self.reason}"


def validate_error(payload: Mapping[str, Any]) -> list[Violation]:
    """校验 error 族载荷（规则见 `contracts/versioned.json`）。"""
    out: list[Violation] = []
    if "message" not in payload:
        out.append(Violation(FAMILY_ERROR, "message", "必填"))
    else:
        message = payload["message"]
        if not isinstance(message, str):
            out.append(Violation(FAMILY_ERROR, "message", "必须是字符串"))
        elif len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
            out.append(Violation(FAMILY_ERROR, "message", "超过 4096 字节"))

    if "code" in payload and not isinstance(payload["code"], str):
        out.append(Violation(FAMILY_ERROR, "code", "必须是字符串"))
    if "retryable" in payload and not isinstance(payload["retryable"], bool):
        out.append(Violation(FAMILY_ERROR, "retryable", "必须是布尔"))
    if "params" in payload and not isinstance(payload["params"], Mapping):
        out.append(Violation(FAMILY_ERROR, "params", "必须是对象"))
    if "trace_id" in payload and not isinstance(payload["trace_id"], str):
        out.append(Violation(FAMILY_ERROR, "trace_id", "必须是字符串"))
    return out


def validate_terminal(payload: Mapping[str, Any]) -> list[Violation]:
    """校验 terminal 族载荷。

    硬规则：type 只能是 done/error/cancelled；type=error 必须带 error 对象
    （且它本身通过 error 族校验）；其余类型不得带 error。
    """
    out: list[Violation] = []
    raw_type = payload.get("type")
    if raw_type is None:
        return [Violation(FAMILY_TERMINAL, "type", "必填")]
    if not isinstance(raw_type, str):
        return [Violation(FAMILY_TERMINAL, "type", "必须是字符串")]
    kind = raw_type
    if kind not in _TERMINAL_TYPES:
        return [Violation(FAMILY_TERMINAL, "type", f"未知取值 {kind!r}")]

    has_error = "error" in payload
    if kind == "error":
        if not has_error:
            out.append(Violation(FAMILY_TERMINAL, "error", "type=error 时必须提供"))
        else:
            body = payload["error"]
            if not isinstance(body, Mapping):
                out.append(Violation(FAMILY_TERMINAL, "error", "必须是对象"))
            else:
                for item in validate_error(body):
                    out.append(
                        Violation(FAMILY_TERMINAL, f"error.{item.field}", item.reason)
                    )
    elif has_error:
        out.append(Violation(FAMILY_TERMINAL, "error", f"type={kind} 时不得提供"))

    if "finish_reason" in payload and not isinstance(payload["finish_reason"], str):
        out.append(Violation(FAMILY_TERMINAL, "finish_reason", "必须是字符串"))
    return out


def validate(family: str, payload: Mapping[str, Any]) -> list[Violation]:
    """按族名分派；未知族名抛 `ValueError`（不允许静默通过）。"""
    key = (family or "").strip().lower()
    if key == FAMILY_ERROR:
        return validate_error(payload)
    if key == FAMILY_TERMINAL:
        return validate_terminal(payload)
    raise ValueError(f"未知契约族：{family!r}")


def terminal_sequence(events: list[Mapping[str, Any]]) -> list[Violation]:
    """检查一个流里出现的终态事件：**最多一个**，且自身合法。

    前端靠终态事件收尾；出现两个意味着"已经收尾了又收一次"，出现零个意味着
    "界面永远在转" —— 两者都是违约。
    """
    out: list[Violation] = []
    seen = 0
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind not in _TERMINAL_TYPES:
            continue
        seen += 1
        if seen > 1:
            out.append(
                Violation(FAMILY_TERMINAL, "", f"第 {index} 个事件：终态事件重复出现")
            )
        for item in validate_terminal(event):
            out.append(Violation(FAMILY_TERMINAL, f"[{index}] {item.field}", item.reason))
    return out


def load_fixtures() -> dict[str, Any]:
    """读 conformance 夹具（Go 侧读的是同一份文件）。"""
    return json.loads(_FIXTURES.read_text(encoding="utf-8"))
