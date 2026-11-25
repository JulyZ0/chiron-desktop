"""Go 与 Python 共享的版本化线上契约（H16 / H16b）。

与 `internal/contract/contract.go` 是**同一份判据的两个实现**：两侧都跑
`contracts/fixtures/conformance.json` 里的同一组用例。任何一侧改判据而另一侧没跟上，
测试就会红 —— 这正是"契约"与"文档"的区别。

v2（H16b）把 v1 里 deferred 的四族（`tool_approval` / `checkpoint` / `queue` / `mcp`）
纳入：纳入口径是"跨语言或跨进程、消费方不止一个"，每族排除掉的字段写在
`contracts/versioned.json` 的 `excluded` 段里（带理由）。
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 契约版本。删/改字段必须同时升版本并改两端；只加可选字段不用升。
VERSION = 2

FAMILY_ERROR = "error"
FAMILY_TERMINAL = "terminal"
FAMILY_RUN_EVENT = "run_event"
#: v2 新增四族。
FAMILY_TOOL_APPROVAL = "tool_approval"
FAMILY_CHECKPOINT = "checkpoint"
FAMILY_QUEUE = "queue"
FAMILY_MCP = "mcp"

_TERMINAL_TYPES = {"done", "error", "cancelled"}
_MAX_MESSAGE_BYTES = 4096

#: 网关的未跟踪清单硬上限（internal/api/project_checkpoints.go 的 ingestUntrackedMax）。
_MAX_UNTRACKED_ENTRIES = 5000

#: 队列消息 deadline 的形状；消费方按同一布局解析
#: （python-engine/app/queue/worker.py 的 strptime("%Y-%m-%dT%H:%M:%SZ")）。
_QUEUE_DEADLINE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_QUEUE_DEADLINE_FMT = "%Y-%m-%dT%H:%M:%SZ"

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


_RUN_EVENT_TYPES = {
    "text",
    "thinking",
    "tool_call",
    "tool_result",
    "approval",
    "todo_updated",
    "usage",
    "trace_span",
    "knowledge_retrieval",
    "guardrail_blocked",
    "compaction",
    "output_text",
    "cancelled",
    "done",
    "error",
}


def validate_run_event(payload: Mapping[str, Any]) -> list[Violation]:
    """校验 run event 的**骨架**（type/seq/id/data），不管各 type 的业务字段。

    业务字段还在演进，冻结进契约只会天天升版本号；消费方必须忽略不认识的 data 字段
    （forward-compat），夹具里有对应用例。
    """
    if "type" not in payload:
        return [Violation(FAMILY_RUN_EVENT, "type", "必填")]
    kind = payload["type"]
    if not isinstance(kind, str):
        return [Violation(FAMILY_RUN_EVENT, "type", "必须是字符串")]
    if kind not in _RUN_EVENT_TYPES:
        return [Violation(FAMILY_RUN_EVENT, "type", f"未知取值 {kind!r}")]

    out: list[Violation] = []
    if "seq" in payload:
        seq = payload["seq"]
        # bool 是 int 的子类，必须单独排掉
        if isinstance(seq, bool) or not isinstance(seq, int):
            out.append(Violation(FAMILY_RUN_EVENT, "seq", "必须是整数"))
        elif seq < 0:
            out.append(Violation(FAMILY_RUN_EVENT, "seq", "必须是非负整数"))
    if "id" in payload and not isinstance(payload["id"], str):
        out.append(Violation(FAMILY_RUN_EVENT, "id", "必须是字符串"))
    if "data" in payload and not isinstance(payload["data"], Mapping):
        out.append(Violation(FAMILY_RUN_EVENT, "data", "必须是对象"))
    return out


def validate(family: str, payload: Mapping[str, Any]) -> list[Violation]:
    """按族名分派；未知族名抛 `ValueError`（不允许静默通过）。"""
    key = (family or "").strip().lower()
    if key == FAMILY_ERROR:
        return validate_error(payload)
    if key == FAMILY_TERMINAL:
        return validate_terminal(payload)
    if key == FAMILY_RUN_EVENT:
        return validate_run_event(payload)
    if key == FAMILY_TOOL_APPROVAL:
        return validate_tool_approval(payload)
    if key == FAMILY_CHECKPOINT:
        return validate_checkpoint(payload)
    if key == FAMILY_QUEUE:
        return validate_queue(payload)
    if key == FAMILY_MCP:
        return validate_mcp(payload)
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


# ── v2（H16b）：tool_approval / checkpoint / queue / mcp ──────────────────────
#
# 判据与 `contracts/versioned.json` 的 rules 一一对应：rules 写了什么就得真校验，
# rules 明确说"不判罚 / 不设枚举"的（queue.task_type 的未知类型、approval.reason 的
# 长度）这里也真的不管 —— 契约过严与过松一样会让两端漂移。
#
# 下面的小工具与 Go 侧同名函数逐字对齐（类型判定必须一致，否则同一份夹具会两端给出
# 不同结论）。

#: 审批三态（C5）：approve / reject / edit。**不是** approved/denied/edited ——
#: 网关（internal/api/submit_handler.go）与引擎（runtime.parse_approval_payload）用的
#: 就是这三个字面值。
_APPROVAL_DECISIONS = {"approve", "reject", "edit"}
_CHECKPOINT_STATES = {"ok", "failed"}
_CHECKPOINT_ERROR_CODES = {"git", "io", "unknown"}
_MCP_STATUSES = {"active", "inactive"}


def _as_int(value: Any) -> int | None:
    """收整数与"整值浮点"（JSON 数字）；bool 不算整数（它是 int 的子类，必须排掉）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else None
    return None


def _as_loose_int(value: Any) -> int | None:
    """额外收十进制数字串：Redis Stream 的字段**一律是字符串**，消费方也按 int() 解析。

    放宽的是传输形态，不是判据。
    """
    parsed = _as_int(value)
    if parsed is not None:
        return parsed
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _queue_deadline_ok(raw: str) -> bool:
    """deadline 必须先符合固定形状、再真能解析（挡住 2026-13-45 这类）。"""
    if not _QUEUE_DEADLINE_RE.fullmatch(raw):
        return False
    try:
        datetime.datetime.strptime(raw, _QUEUE_DEADLINE_FMT)
    except ValueError:
        return False
    return True


def _edited_arguments(raw: Any) -> dict[str, Any] | None:
    """归一 edit 的参数：既接受 JSON 字符串，也接受已解析的对象。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_tool_approval(payload: Mapping[str, Any]) -> list[Violation]:
    """校验审批决定载荷（前端确认卡 → 网关 → 引擎 → pending_interactions.decision）。

    三条硬规则：`session_id`/`tool_call_id` 必填；`decision` 与 `approved` 至少给一个
    （缺字段不得等同放行）；`decision` 只能是 approve/reject/edit，且 `edit` 必须带能
    解析成 JSON 对象的 `arguments`。
    """
    out: list[Violation] = []

    for field in ("session_id", "tool_call_id"):
        if field not in payload:
            out.append(Violation(FAMILY_TOOL_APPROVAL, field, "必填"))
            continue
        value = payload[field]
        if not isinstance(value, str):
            out.append(Violation(FAMILY_TOOL_APPROVAL, field, "必须是字符串"))
            continue
        if not value.strip():
            out.append(
                Violation(FAMILY_TOOL_APPROVAL, field, "不得为空：缺了决定无法路由、票据也对不上")
            )

    decision = ""
    has_decision = False
    if "decision" in payload:
        raw_decision = payload["decision"]
        if not isinstance(raw_decision, str):
            out.append(Violation(FAMILY_TOOL_APPROVAL, "decision", "必须是字符串"))
        else:
            trimmed = raw_decision.strip().lower()
            if trimmed not in _APPROVAL_DECISIONS:
                out.append(
                    Violation(
                        FAMILY_TOOL_APPROVAL,
                        "decision",
                        f"未知取值 {raw_decision!r}（只允许 approve/reject/edit）",
                    )
                )
            else:
                decision = trimmed
                has_decision = True

    has_approved = "approved" in payload
    if has_approved and not isinstance(payload["approved"], bool):
        out.append(Violation(FAMILY_TOOL_APPROVAL, "approved", "必须是布尔"))
    if not has_decision and not has_approved:
        # 默认拒绝，而不是默认批准：漏发字段变成放行是安全事故，不是兼容性。
        out.append(
            Violation(FAMILY_TOOL_APPROVAL, "decision", "decision 与 approved 至少要有一个")
        )

    if decision == "edit":
        if "arguments" not in payload:
            out.append(
                Violation(FAMILY_TOOL_APPROVAL, "arguments", "decision=edit 时必须提供")
            )
        elif _edited_arguments(payload["arguments"]) is None:
            out.append(
                Violation(FAMILY_TOOL_APPROVAL, "arguments", "必须是 JSON 对象的字符串编码")
            )
    # 非 edit 时 arguments 被引擎忽略（parse_approval_payload 不看它），这里也不判罚。
    return out


def validate_checkpoint(payload: Mapping[str, Any]) -> list[Violation]:
    """校验检查点**上报**载荷（引擎 → 网关 /v1/internal/checkpoints）。

    `state=ok` 时 commit/tree/ref 必须齐全（缺了就没有可回滚的目标）；`state=failed`
    时不要求它们（为空正是失败原因，而失败记录同样必须能上报）。
    """
    out: list[Violation] = []

    if "session_id" not in payload:
        out.append(Violation(FAMILY_CHECKPOINT, "session_id", "必填"))
    else:
        value = payload["session_id"]
        if not isinstance(value, str):
            out.append(Violation(FAMILY_CHECKPOINT, "session_id", "必须是字符串"))
        elif not value.strip():
            out.append(
                Violation(FAMILY_CHECKPOINT, "session_id", "不得为空：网关按它反查会话所属项目")
            )

    if "seq" not in payload:
        out.append(Violation(FAMILY_CHECKPOINT, "seq", "必填"))
    else:
        seq = _as_int(payload["seq"])
        if seq is None:
            out.append(Violation(FAMILY_CHECKPOINT, "seq", "必须是整数"))
        elif seq <= 0:
            out.append(
                Violation(
                    FAMILY_CHECKPOINT,
                    "seq",
                    "必须是正整数：主键由 (session_id, seq, kind) 派生",
                )
            )

    if "kind" not in payload:
        out.append(Violation(FAMILY_CHECKPOINT, "kind", "必填"))
    else:
        kind = payload["kind"]
        if not isinstance(kind, str):
            out.append(Violation(FAMILY_CHECKPOINT, "kind", "必须是字符串"))
        elif not kind.strip():
            out.append(Violation(FAMILY_CHECKPOINT, "kind", "不得为空"))

    state = "ok"
    if "state" in payload:
        raw_state = payload["state"]
        if not isinstance(raw_state, str):
            out.append(Violation(FAMILY_CHECKPOINT, "state", "必须是字符串"))
            state = ""
        else:
            trimmed = raw_state.strip().lower()
            if trimmed not in _CHECKPOINT_STATES:
                out.append(
                    Violation(
                        FAMILY_CHECKPOINT,
                        "state",
                        f"未知取值 {raw_state!r}（只允许 ok/failed）",
                    )
                )
                state = ""
            else:
                state = trimmed

    # 成功态必须有可回滚的目标；失败态不要求（那正是失败的表现）。
    for field in ("commit_sha", "tree_sha", "ref_name"):
        if field not in payload:
            if state == "ok":
                out.append(
                    Violation(
                        FAMILY_CHECKPOINT,
                        field,
                        "state=ok 时必填（否则这份快照没有可回滚的目标）",
                    )
                )
            continue
        value = payload[field]
        if not isinstance(value, str):
            out.append(Violation(FAMILY_CHECKPOINT, field, "必须是字符串"))
            continue
        if state == "ok" and not value.strip():
            out.append(Violation(FAMILY_CHECKPOINT, field, "state=ok 时不得为空"))

    for field in ("parent_sha", "head_sha", "error"):
        if field in payload and not isinstance(payload[field], str):
            out.append(Violation(FAMILY_CHECKPOINT, field, "必须是字符串"))

    if "error_code" in payload:
        raw_code = payload["error_code"]
        if not isinstance(raw_code, str):
            out.append(Violation(FAMILY_CHECKPOINT, "error_code", "必须是字符串"))
        else:
            trimmed = raw_code.strip().lower()
            if trimmed and trimmed not in _CHECKPOINT_ERROR_CODES:
                out.append(
                    Violation(
                        FAMILY_CHECKPOINT,
                        "error_code",
                        f"未知取值 {raw_code!r}（只允许 git/io/unknown）",
                    )
                )

    for field in ("file_count", "skipped_count", "byte_size"):
        if field not in payload:
            continue
        count = _as_int(payload[field])
        if count is None:
            out.append(Violation(FAMILY_CHECKPOINT, field, "必须是整数"))
        elif count < 0:
            out.append(Violation(FAMILY_CHECKPOINT, field, "必须是非负整数"))

    if "untracked" in payload:
        out.extend(_validate_untracked(payload["untracked"]))
    return out


def _validate_untracked(raw: Any) -> list[Violation]:
    """未跟踪清单：网关硬上限之外，每条必须带非空 path 与 sha256。

    清单不全时回滚会"成功"但文件回不来（网关按 sha256 从 CAS 取内容）。
    """
    out: list[Violation] = []
    if not isinstance(raw, list):
        return [Violation(FAMILY_CHECKPOINT, "untracked", "必须是数组")]
    if len(raw) > _MAX_UNTRACKED_ENTRIES:
        out.append(
            Violation(
                FAMILY_CHECKPOINT,
                "untracked",
                f"条目数 {len(raw)} 超过上限 {_MAX_UNTRACKED_ENTRIES}",
            )
        )
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            out.append(Violation(FAMILY_CHECKPOINT, f"untracked[{index}]", "必须是对象"))
            continue
        for field in ("path", "sha256"):
            if field not in item:
                out.append(Violation(FAMILY_CHECKPOINT, f"untracked[{index}].{field}", "必填"))
                continue
            value = item[field]
            if not isinstance(value, str):
                out.append(
                    Violation(FAMILY_CHECKPOINT, f"untracked[{index}].{field}", "必须是字符串")
                )
                continue
            if not value.strip():
                out.append(
                    Violation(
                        FAMILY_CHECKPOINT,
                        f"untracked[{index}].{field}",
                        "不得为空：回滚按 sha256 从 CAS 取内容",
                    )
                )
        if "size" in item:
            size = _as_int(item["size"])
            if size is None:
                out.append(Violation(FAMILY_CHECKPOINT, f"untracked[{index}].size", "必须是整数"))
            elif size < 0:
                out.append(
                    Violation(FAMILY_CHECKPOINT, f"untracked[{index}].size", "必须是非负整数")
                )
    return out


def validate_queue(payload: Mapping[str, Any]) -> list[Violation]:
    """校验队列任务消息（Redis Stream 载荷）。

    两件事要记住：(1) Redis 里字段一律是字符串，所以 `tenant_id` / `retry_count` 收
    数字串 —— 放宽的是传输形态，不是判据；(2) `task_type` **不设枚举**：新增任务类型
    是 additive 的，未知类型由消费方转发/送 DLQ，不在这里判违约。
    """
    out: list[Violation] = []

    for field in ("task_id", "task_type"):
        if field not in payload:
            out.append(Violation(FAMILY_QUEUE, field, "必填"))
            continue
        value = payload[field]
        if not isinstance(value, str):
            out.append(Violation(FAMILY_QUEUE, field, "必须是字符串"))
            continue
        if not value.strip():
            out.append(Violation(FAMILY_QUEUE, field, "不得为空"))

    if "tenant_id" not in payload:
        out.append(Violation(FAMILY_QUEUE, "tenant_id", "必填"))
    else:
        tenant = _as_loose_int(payload["tenant_id"])
        if tenant is None:
            out.append(
                Violation(
                    FAMILY_QUEUE,
                    "tenant_id",
                    "必须是整数或十进制数字串（消费方会静默回落 0 = 无租户任务）",
                )
            )
        elif tenant < 0:
            out.append(Violation(FAMILY_QUEUE, "tenant_id", "必须是非负整数"))

    if "payload" not in payload:
        out.append(Violation(FAMILY_QUEUE, "payload", "必填"))
    else:
        raw = payload["payload"]
        if not isinstance(raw, str):
            out.append(Violation(FAMILY_QUEUE, "payload", "必须是 JSON 对象的字符串编码"))
        else:
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                decoded = None
            if not isinstance(decoded, dict):
                out.append(
                    Violation(FAMILY_QUEUE, "payload", "必须是 JSON 对象的字符串编码")
                )

    if "retry_count" in payload:
        retries = _as_loose_int(payload["retry_count"])
        if retries is None:
            out.append(Violation(FAMILY_QUEUE, "retry_count", "必须是整数或十进制数字串"))
        elif retries < 0:
            out.append(
                Violation(
                    FAMILY_QUEUE, "retry_count", "必须是非负整数：它是进 DLQ 的唯一依据"
                )
            )

    if "idempotency_key" in payload and not isinstance(payload["idempotency_key"], str):
        # 空串等价于未提供（消费方回落 {task_type}:{task_id}），不判罚。
        out.append(Violation(FAMILY_QUEUE, "idempotency_key", "必须是字符串"))

    if "deadline" in payload:
        raw_deadline = payload["deadline"]
        if not isinstance(raw_deadline, str):
            out.append(Violation(FAMILY_QUEUE, "deadline", "必须是字符串"))
        elif not _queue_deadline_ok(raw_deadline.strip()):
            out.append(
                Violation(
                    FAMILY_QUEUE,
                    "deadline",
                    "必须是 YYYY-MM-DDTHH:MM:SSZ（解析失败会被当作没有截止时间直接执行）",
                )
            )
    return out


def validate_mcp(payload: Mapping[str, Any]) -> list[Violation]:
    """校验 MCP server 配置条目（plugins.json 的 `mcp_servers[i]`）。

    关键判据是**授权两件套**：`approved=true` 而 `approval_digest` 为空，等于"不知道
    批了什么"，两端都按未授权处理（plugin_approval.go / store.py 同口径）。
    """
    out: list[Violation] = []

    for field in ("name", "command"):
        if field not in payload:
            out.append(Violation(FAMILY_MCP, field, "必填"))
            continue
        value = payload[field]
        if not isinstance(value, str):
            out.append(Violation(FAMILY_MCP, field, "必须是字符串"))
            continue
        if not value.strip():
            out.append(
                Violation(FAMILY_MCP, field, "不得为空：引擎按 name 分流、按 command 起子进程")
            )

    if "args" in payload:
        raw_args = payload["args"]
        if not isinstance(raw_args, list):
            out.append(Violation(FAMILY_MCP, "args", "必须是字符串数组"))
        else:
            for index, item in enumerate(raw_args):
                if not isinstance(item, str):
                    out.append(Violation(FAMILY_MCP, f"args[{index}]", "必须是字符串"))

    if "env" in payload:
        raw_env = payload["env"]
        if not isinstance(raw_env, Mapping):
            out.append(Violation(FAMILY_MCP, "env", "必须是 {字符串: 字符串}"))
        else:
            for key, value in raw_env.items():
                if not isinstance(value, str):
                    out.append(
                        Violation(FAMILY_MCP, f"env.{key}", "必须是字符串（env 里装的是凭据/端点）")
                    )

    if "status" in payload:
        raw_status = payload["status"]
        if not isinstance(raw_status, str):
            out.append(Violation(FAMILY_MCP, "status", "必须是字符串"))
        else:
            trimmed = raw_status.strip().lower()
            if trimmed not in _MCP_STATUSES:
                out.append(
                    Violation(
                        FAMILY_MCP, "status", f"未知取值 {raw_status!r}（只允许 active/inactive）"
                    )
                )

    approved = False
    if "approved" in payload:
        raw_approved = payload["approved"]
        if not isinstance(raw_approved, bool):
            out.append(Violation(FAMILY_MCP, "approved", "必须是布尔"))
        else:
            approved = raw_approved

    digest = ""
    if "approval_digest" in payload:
        raw_digest = payload["approval_digest"]
        if not isinstance(raw_digest, str):
            out.append(Violation(FAMILY_MCP, "approval_digest", "必须是字符串"))
        else:
            digest = raw_digest.strip()
    if approved and not digest:
        out.append(
            Violation(
                FAMILY_MCP,
                "approval_digest",
                "approved=true 时必须非空（没有摘要的批准按未授权处理）",
            )
        )

    if "read_only" in payload and not isinstance(payload["read_only"], bool):
        out.append(Violation(FAMILY_MCP, "read_only", "必须是布尔"))
    return out
