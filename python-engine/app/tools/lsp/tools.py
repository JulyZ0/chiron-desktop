"""两个常驻语言服务器工具：`lsp_definition`（跳转定义）/ `lsp_diagnostics`（取诊断）。

## 边界（这个仓库对工具边界很严，逐条写清）

* **路径**：一律经 `app/tools/sandbox.safe_join` 夹到工作区内，越界直接拒绝
  （与 `read_file` / `grep_files` 同一道边界）。语言服务器进程的 `cwd` 也是这个
  工作区，服务器自己 `didOpen` 只会拿到工作区内的文件。
* **进程**：只经 `sandboxed_env()` + `child_launch_argv()` + `attach_job()`
  （见 `session._spawn_in_sandbox`），不自己 `subprocess.Popen`。
* **语言服务器命令**：只从 `CHIRON_LSP_<LANG>` 读，没有配置就明确报错，
  不内置、不猜测（见 `config.py`）。
* **并发安全**：**不声明**。语言服务器是有状态、串行的进程，会话里的并发上限
  就是 1；声明并发安全只会让调度器把并行请求打进这个队列，最终变成超时。

## 位置口径

入参 `line` / `character` 是 **1 基**（和编辑器、以及模型对"第几行"的直觉一致），
输出也是 1 基。LSP 协议内部是 0 基，换算只发生在这个文件里 —— 三段口径
（入参 / 协议 / 输出）混用是这类工具最容易出的差一行 bug。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.tools.lsp.config import (
    LspConfigError,
    language_for_path,
    normalize_language,
)
from app.tools.lsp.session import (
    LspDocumentError,
    LspSessionClosedError,
    LspSessionManager,
    LspStartError,
    uri_to_path,
)
from app.tools.registry import registry
from app.tools.sandbox import safe_join, workspace_dir

logger = logging.getLogger(__name__)

#: 诊断等待的默认上限（秒）。推送式诊断是异步的，等不到就返回缓存并标 stale。
DIAGNOSTICS_WAIT = 5.0

#: LSP `DiagnosticSeverity` → 名字（模型读名字比读 1/2/3/4 可靠）
_SEVERITY_NAMES = {1: "error", 2: "warning", 3: "information", 4: "hint"}

_manager: LspSessionManager | None = None


def manager() -> LspSessionManager:
    """取会话路由表单例（首次调用时创建）。"""
    global _manager
    if _manager is None:
        _manager = LspSessionManager()
    return _manager


def set_manager(instance: LspSessionManager | None) -> None:
    """替换路由表（测试与嵌入场景用；`None` 恢复默认单例）。"""
    global _manager
    _manager = instance


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def _error(code: str, message: str) -> dict[str, Any]:
    """工具错误的统一形态：`error` 给模型读，`code` 给前端/审计归类。"""
    return {"error": message, "code": code}


def _relative(path: Path) -> str:
    """输出用**工作区相对路径**（不回显宿主绝对路径，与其它工具一致）。"""
    try:
        return path.resolve().relative_to(workspace_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _resolve_language(language: str, path: Path) -> str:
    if language.strip():
        return normalize_language(language)
    # 按扩展名推断**语言键**是安全的：推断错了只会得到"该语言没配置"，
    # 而推断命令则会静默跑错服务器（见 config.py 的说明）。
    return language_for_path(path)


async def _acquire(language: str, raw_path: str) -> tuple[Any, dict[str, Any] | None]:
    """夹路径 + 定语言 + 取（必要时启动）会话。

    全部前置校验（越界 / 语言 / 配置）都在这里完成，因此**任何**返回错误的路径
    都不会留下进程 —— 这是"边界检查必须在起进程之前"的具体落地。
    """
    try:
        target = safe_join(raw_path)
    except ValueError as exc:
        return None, _error("path_escapes_sandbox", f"路径越界，拒绝访问：{exc}")
    try:
        lang = _resolve_language(language, target)
    except LspConfigError as exc:
        return None, _error("lsp_language_unknown", str(exc))
    try:
        session = await manager().acquire(workspace_dir(), lang)
    except LspConfigError as exc:
        return None, _error("lsp_not_configured", str(exc))
    except LspStartError as exc:
        return None, _error("lsp_start_failed", str(exc))
    return (session, target), None


# ---------------------------------------------------------------------------
# 工具：跳转定义
# ---------------------------------------------------------------------------


async def lsp_definition(
    path: str,
    line: int,
    character: int,
    language: str = "",
) -> dict[str, Any]:
    """跳转到定义（LSP `textDocument/definition`）。

    `line` / `character` 是 1 基（第 1 行第 1 列 = 文件开头）。
    """
    if line < 1 or character < 1:
        return _error(
            "invalid_position",
            f"line/character 必须 >= 1（1 基），实得 line={line}, character={character}",
        )
    resolved, failure = await _acquire(language, path)
    if failure is not None:
        return failure
    session, target = resolved
    try:
        locations = await session.definition(target, line - 1, character - 1)
    except LspDocumentError as exc:
        return _error("lsp_file_unreadable", str(exc))
    except LspSessionClosedError as exc:
        return _error("lsp_session_closed", str(exc))
    except TimeoutError as exc:
        return _error("lsp_query_timeout", str(exc))
    except (ConnectionError, RuntimeError) as exc:
        return _error("lsp_query_failed", str(exc))

    converted = [_location_payload(item) for item in locations]
    return {
        "path": _relative(target),
        "language": session.language,
        "count": len(converted),
        "locations": converted,
    }


def _location_payload(item: dict[str, Any]) -> dict[str, Any]:
    """协议位置（0 基、uri）→ 工具输出（1 基、工作区相对路径）。"""
    uri = item["uri"]
    ready = uri_to_path(uri)
    return {
        "uri": uri,
        "path": _relative(ready) if ready is not None else "",
        "line": item["start_line"] + 1,
        "character": item["start_character"] + 1,
        "end_line": item["end_line"] + 1,
        "end_character": item["end_character"] + 1,
    }


# ---------------------------------------------------------------------------
# 工具：诊断
# ---------------------------------------------------------------------------


async def lsp_diagnostics(
    path: str,
    language: str = "",
    timeout: float = 0.0,
) -> dict[str, Any]:
    """取当前文件的诊断（错误/警告）。

    `timeout` 是等待推送式诊断的上限（秒），0 = 用默认值。等不到时返回已有结果并
    在 `stale` 里说明 —— 诊断不会因为一次等待超时而杀掉语言服务器。
    """
    resolved, failure = await _acquire(language, path)
    if failure is not None:
        return failure
    session, target = resolved
    wait = timeout if timeout > 0 else DIAGNOSTICS_WAIT
    try:
        result = await session.diagnostics(target, wait=wait)
    except LspDocumentError as exc:
        return _error("lsp_file_unreadable", str(exc))
    except LspSessionClosedError as exc:
        return _error("lsp_session_closed", str(exc))
    except TimeoutError as exc:
        return _error("lsp_query_timeout", str(exc))
    except (ConnectionError, RuntimeError) as exc:
        return _error("lsp_query_failed", str(exc))

    items = [_diagnostic_payload(item, _relative(target)) for item in result.items]
    payload: dict[str, Any] = {
        "path": _relative(target),
        "language": session.language,
        "source": result.source,
        "count": len(items),
        "diagnostics": items,
    }
    if result.stale:
        payload["stale"] = True
        payload["note"] = (
            f"语言服务器没有在 {wait:g}s 内推送新诊断，返回的是上一次的结果"
            "（诊断是异步通知：大项目首次分析可能更慢，可稍后重试或增大 timeout）"
        )
    return payload


def _diagnostic_payload(item: dict[str, Any], path_label: str) -> dict[str, Any]:
    """协议诊断 → 工具输出（1 基位置 + 可读 severity）。

    `path` 逐条带上：诊断项本身不带文件名（它属于被查询的文档），但消费方
    （前端列表、多个诊断拼一张表时）需要能一行一条地渲染。
    """
    raw_range = item.get("range")
    range_: dict[str, Any] = raw_range if isinstance(raw_range, dict) else {}
    raw_start = range_.get("start")
    start: dict[str, Any] = raw_start if isinstance(raw_start, dict) else {}
    raw_end = range_.get("end")
    end: dict[str, Any] = raw_end if isinstance(raw_end, dict) else start
    severity = item.get("severity")
    return {
        "path": path_label,
        "line": _one_based(start.get("line")),
        "character": _one_based(start.get("character")),
        "end_line": _one_based(end.get("line")),
        "end_character": _one_based(end.get("character")),
        "severity": _SEVERITY_NAMES.get(severity, "unknown")
        if isinstance(severity, int)
        else "unknown",
        "message": str(item.get("message") or ""),
        "source": str(item.get("source") or ""),
        "code": "" if item.get("code") is None else str(item.get("code")),
    }


def _one_based(value: Any) -> int:
    """协议 0 基 → 输出 1 基（非法值按 1 处理，至少不会给出"第 0 行"）。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value + 1
    return 1


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

_WORKSPACE_NOTE = "路径必须落在工作区内（越界会被 sandbox.safe_join 拒绝）"

registry.register(
    name="lsp_definition",
    description=(
        "跳转到定义：返回 path/line/character 指向的符号在项目里的定义位置。"
        f"line 与 character 都是 1 基。{_WORKSPACE_NOTE}。"
        "语言服务器命令来自环境变量 CHIRON_LSP_<LANG>（未配置时会明确报错）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（工作区相对或工作区内绝对）"},
            "line": {"type": "integer", "description": "1 基行号"},
            "character": {"type": "integer", "description": "1 基列号"},
            "language": {
                "type": "string",
                "description": "语言键（python / typescript / go …）；留空按扩展名推断",
                "default": "",
            },
        },
        "required": ["path", "line", "character"],
    },
    handler=lsp_definition,
)

registry.register(
    name="lsp_diagnostics",
    description=(
        "取文件的诊断（错误/警告/提示）：等语言服务器推送或按需拉取。"
        f"{_WORKSPACE_NOTE}。语言服务器命令来自环境变量 CHIRON_LSP_<LANG>。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（工作区相对或工作区内绝对）"},
            "language": {
                "type": "string",
                "description": "语言键；留空按扩展名推断",
                "default": "",
            },
            "timeout": {
                "type": "number",
                "description": "等待推送式诊断的上限（秒）；0 表示用默认值",
                "default": 0,
            },
        },
        "required": ["path"],
    },
    handler=lsp_diagnostics,
)
