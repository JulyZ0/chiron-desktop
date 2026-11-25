"""常驻语言服务器会话管理（G23）。

## 形态

DSH 的 LSP 是「**每个项目根一个实例 + 串行查询队列 + 超时 SIGTERM→SIGKILL**」，
这里按同一口径实现：

* :class:`LspSession` —— 一个 (项目根, 语言) 的进程 + JSON-RPC 客户端；
  懒启动（第一次查询才起），查询经一把锁**串行**，请求超时即终止进程
  （先 terminate，宽限内不退出再 kill）并让会话失效。
* :class:`LspSessionManager` —— 路由表 `(根, 语言) → 会话`，负责复用、
  启动失败不留残骸、卸载时**先撤路由再关进程**。

## 为什么必须常驻

语言服务器启动要建索引（pyright / gopls / rust-analyzer 动辄数秒到数十秒）。
"一次查询起一个进程"等于每次都在等索引；而且它们是**有状态**进程：
`didOpen` 之后才有当前文档的定义/诊断（未打开的文档在多数服务器里查不到）。

## 为什么串行

进程内状态与请求顺序耦合（文档版本、索引快照）。并发查询不会更快，只会
让结果取决于调度顺序 —— 同一个位置两次查询给出不同答案，这比慢得多更难查。
并发上限固定为 **1**，就在 :class:`LspSession` 的那把锁上。

## 超时为什么直接杀进程

一个不回应 `textDocument/definition` 的语言服务器已经坏了（死锁、索引崩、
被自己的异常卡住）。留着它，之后每条查询都会超时 —— 用户体感是"这个项目上
LSP 全哑"；而重建一个进程的代价只是一次索引。所以超时的处置是终止会话，
下一次查询自动重建。

**例外**：诊断（`textDocument/diagnostic` 之外的推送模式）是"服务器最终会推"
的异步通知，等不到就返回缓存/空并标注 `stale`，**不**杀进程 —— 代价不对等。

## 刻意不做的事

* **不发 `textDocument/didClose`**：工作区文件在整个会话期内保持打开。关掉的收益
  是"少占服务器内存"，代价是下一次查询要重新 didOpen（pyright 之类会重新解析）；
  会话本身随项目根复用，文件集合不会无限膨胀。
* **不发 `$/cancelRequest`**：超时的处置是杀进程（见上），给一个已经坏掉的进程
  发取消通知没有意义。
* **不做 workspace 级符号搜索 / 引用查询**：PRD 只要求"跳转定义 + 诊断"两条，
  接口留在这里（`request()` 是通用的），但没有把它们注册成工具。
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.tools.lsp.client import JsonRpcClient, LspTimeoutError
from app.tools.lsp.config import LspServerConfig

logger = logging.getLogger(__name__)

#: 初始化握手的额外宽限（有些服务器启动时要先建索引才回 initialize）
INITIALIZE_TIMEOUT = 30.0

#: 推送式诊断的默认等待时间（秒）
DEFAULT_DIAGNOSTICS_WAIT = 5.0

#: 终止进程后最多保留多少行 stderr 用于错误信息
STDERR_TAIL_LINES = 20


class LspStartError(RuntimeError):
    """语言服务器起不来 / 握手失败。消息面向用户，会带 stderr 尾部。"""


class LspSessionClosedError(RuntimeError):
    """会话已被关闭/终止（超时升级或卸载）。"""


class LspDocumentError(RuntimeError):
    """文档读不出来/同步不了（工具层据此区分"文件问题"与"服务器问题"）。"""


class LspProcess(Protocol):
    """`asyncio.subprocess.Process` 的最小接口（测试用替身实现同一组成员）。"""

    @property
    def returncode(self) -> int | None: ...

    @property
    def pid(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int | None: ...


@dataclass
class SpawnedTransport:
    """一次 spawn 的产物：进程 + 已启动的客户端（+可选 stderr 泵）。"""

    proc: LspProcess
    client: JsonRpcClient
    stderr: asyncio.StreamReader | None = None
    job: Any = None


SpawnFn = Callable[[LspServerConfig, Path], Awaitable[SpawnedTransport]]


@dataclass
class _DocumentState:
    uri: str
    version: int
    text: str


@dataclass
class _DiagnosticsState:
    seq: int
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnosticsResult:
    """诊断查询结果。

    `source`：`"push"`（服务器推 `publishDiagnostics`）或 `"pull"`（`textDocument/diagnostic`）。
    `stale`：等不到**新**的推送，返回的是上一次的结果（或空）。
    """

    items: list[dict[str, Any]]
    source: str
    stale: bool = False


def path_to_uri(path: Path) -> str:
    """文件路径 → `file://` URI（LSP 全程用它标识文档）。"""
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> Path | None:
    """`file://` URI → 路径；非 file 协议（`untitled:` 等）返回 None。

    Windows 细节：`as_uri()` 产出 `file:///D:/a/b`，`url2pathname` 会把前导
    `/` 处理成盘符形式，因此这里不能自己 `lstrip("/")`（会把 `/D:/a` 变成 `D:/a`
    再被 `Path` 当成相对路径）。
    """
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return None
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != "file":
        return None
    raw = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if not raw:
        return None
    return Path(raw)


def session_key(root: Path | str, language: str) -> tuple[str, str]:
    """路由表键：`(项目根绝对路径, 语言)`。"""
    return (str(Path(root).resolve()), language)


class LspSession:
    """一个 (项目根, 语言) 的语言服务器会话。"""

    def __init__(
        self,
        *,
        root: Path,
        language: str,
        config: LspServerConfig,
        spawn: SpawnFn | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.language = language
        self.key = session_key(self.root, language)
        self._config = config
        self._spawn: SpawnFn = spawn or _spawn_in_sandbox
        self._transport: SpawnedTransport | None = None
        # 并发上限 1：见模块 docstring「为什么串行」
        self._serial = asyncio.Lock()
        self._documents: dict[str, _DocumentState] = {}
        self._diagnostics: dict[str, _DiagnosticsState] = {}
        self._diagnostics_seq = 0
        self._diagnostics_changed = asyncio.Event()
        self._capabilities: dict[str, Any] = {}
        self._stderr_tail: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None

    # ── 生命周期 ────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        transport = self._transport
        return transport is not None and transport.proc.returncode is None

    @property
    def pid(self) -> int | None:
        transport = self._transport
        return None if transport is None else transport.proc.pid

    @property
    def capabilities(self) -> dict[str, Any]:
        return dict(self._capabilities)

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    async def start(self) -> None:
        """起进程并完成 initialize/initialized 握手（幂等）。

        失败时**一定**先清理进程再抛 `LspStartError`：留下一个"起了但没握手"的
        语言服务器既不响应查询也不再退出，是纯粹的僵尸。
        """
        if self._transport is not None:
            return
        try:
            transport = await self._spawn(self._config, self.root)
        except LspStartError:
            raise
        except Exception as exc:  # noqa: BLE001 - 归一到可读错误，附语言与命令
            raise LspStartError(
                f"语言服务器启动失败（language={self.language}, "
                f"command={self._config.command[0]!r}）：{exc}"
            ) from exc
        self._transport = transport
        if transport.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(transport.stderr), name=f"lsp-stderr:{self.language}"
            )
        # 诊断推送必须在 didOpen **之前**接上，否则第一次查询的诊断会漏
        self.install_notification_handlers()
        try:
            result = await self._request_locked(
                "initialize", self._initialize_params(), timeout=INITIALIZE_TIMEOUT
            )
            if isinstance(result, dict) and isinstance(result.get("capabilities"), dict):
                self._capabilities = dict(result["capabilities"])
            await transport.client.notify("initialized", {})
        except Exception as exc:  # noqa: BLE001 - 握手失败一律清理 + 可读错误
            tail = self.stderr_tail
            await self._terminate(reason="初始化失败")
            detail = f"；stderr: {tail}" if tail else ""
            raise LspStartError(
                f"语言服务器初始化失败（language={self.language}, "
                f"command={self._config.command[0]!r}）：{exc}{detail}"
            ) from exc

    def _initialize_params(self) -> dict[str, Any]:
        """initialize 参数。刻意只声明**真的实现**了的能力（多余声明会让服务器
        反向请求一些我们回不了的东西）。"""
        root_uri = path_to_uri(self.root)
        return {
            "processId": os.getpid(),
            "clientInfo": {"name": "chiron-lsp"},
            "rootUri": root_uri,
            "rootPath": str(self.root),
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "definition": {"linkSupport": True},
                    "publishDiagnostics": {"relatedInformation": False, "versionSupport": False},
                    "synchronization": {"didSave": False, "willSave": False},
                },
                "workspace": {"configuration": True, "workspaceFolders": True},
            },
        }

    async def aclose(self, *, reason: str = "会话关闭") -> None:
        """关闭会话：撤客户端、终止进程（terminate → kill），关闭 Job 句柄。"""
        await self._terminate(reason=reason)

    async def _terminate(self, *, reason: str) -> None:
        transport, self._transport = self._transport, None
        if transport is None:
            return
        # 先撤客户端：挂起中的请求立刻拿到失败，不必等进程死透
        await transport.client.aclose(reason=reason)
        await _stop_process(transport.proc, grace=self._config.kill_grace, reason=reason)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None
        if transport.job is not None:
            try:
                transport.job.close()
            except Exception as exc:  # noqa: BLE001 - 关句柄失败不该影响清理结果
                logger.warning("关闭语言服务器 Job 句柄失败：%s", exc)

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """收 stderr 尾部。语言服务器的报错**只**出现在这里，不读就查不出原因。"""
        try:
            while True:
                raw = await stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                del self._stderr_tail[:-STDERR_TAIL_LINES]
                logger.debug("lsp[%s] stderr: %s", self.language, line)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            return

    # ── 查询 ────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """串行地发一条请求；超时则终止会话并抛 `LspTimeoutError`。"""
        async with self._serial:
            return await self._request_locked(method, params, timeout=timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        async with self._serial:
            await self._notify_locked(method, params)

    async def _request_locked(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        transport = self._transport
        if transport is None:
            raise LspSessionClosedError(
                f"语言服务器会话不可用（language={self.language}, root={self.root}）"
            )
        limit = self._config.request_timeout if timeout is None else timeout
        try:
            return await transport.client.request(
                method,
                params,
                timeout=limit,
            )
        except TimeoutError as exc:
            # 会话语义：超时 = 进程坏了 → 终止（SIGTERM→SIGKILL），下次查询重建
            await self._terminate(reason=f"{method} 超时")
            raise LspTimeoutError(
                f"语言服务器 {method} 超时（>{limit:g}s，language={self.language}）；"
                "已终止该语言服务器进程，下次查询会重新启动"
            ) from exc

    async def _notify_locked(self, method: str, params: dict[str, Any] | None = None) -> None:
        transport = self._transport
        if transport is None:
            raise LspSessionClosedError(
                f"语言服务器会话不可用（language={self.language}, root={self.root}）"
            )
        await transport.client.notify(method, params)

    # ── 文档同步 ────────────────────────────────────────────────

    async def definition(
        self,
        path: Path,
        line: int,
        character: int,
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """跳转定义。`line` / `character` 是 **0 基**（协议口径，工具层再换算）。"""
        async with self._serial:
            uri = await self._open_document_locked(path)
            result = await self._request_locked(
                "textDocument/definition",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                },
                timeout=timeout,
            )
            return normalize_locations(result)

    async def diagnostics(
        self,
        path: Path,
        *,
        wait: float | None = None,
    ) -> DiagnosticsResult:
        """取诊断：服务器声明 `diagnosticProvider` 就走拉取，否则等一次推送。"""
        async with self._serial:
            before = self._diagnostics_seq
            uri = await self._open_document_locked(path, refresh=True)
            if self._capabilities.get("diagnosticProvider"):
                raw = await self._request_locked(
                    "textDocument/diagnostic",
                    {"textDocument": {"uri": uri}},
                )
                return DiagnosticsResult(items=_pull_items(raw), source="pull")
            return await self._wait_diagnostics_locked(uri, before, wait)

    async def _wait_diagnostics_locked(
        self, uri: str, before: int, wait: float | None
    ) -> DiagnosticsResult:
        """等一条**新的** publishDiagnostics；等不到就返回缓存并标注 stale。"""
        limit = DEFAULT_DIAGNOSTICS_WAIT if wait is None else wait
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, limit)
        while True:
            state = self._diagnostics.get(uri)
            if state is not None and state.seq > before:
                return DiagnosticsResult(items=list(state.items), source="push")
            remaining = deadline - loop.time()
            if remaining <= 0:
                cached = state.items if state is not None else []
                return DiagnosticsResult(items=list(cached), source="push", stale=True)
            # Event 必须在等待前 clear，否则"这次推的"会被下一次误当成新的
            self._diagnostics_changed.clear()
            try:
                await asyncio.wait_for(self._diagnostics_changed.wait(), timeout=remaining)
            except TimeoutError:
                continue

    async def _open_document_locked(self, path: Path, *, refresh: bool = False) -> str:
        """把文件同步给服务器（didOpen / didChange），返回 uri。

        `refresh=True`（诊断用）即使内容没变也推进版本并发 didChange：
        诊断是"文件内容 → 诊断集合"的函数，服务器不会因为"我早看过"而重推。
        """
        uri = path_to_uri(path)
        try:
            text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspDocumentError(f"读取文件失败（{path}）：{exc}") from exc
        existing = self._documents.get(uri)
        if existing is None:
            state = _DocumentState(uri=uri, version=1, text=text)
            self._documents[uri] = state
            await self._notify_locked(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.language,
                        "version": state.version,
                        "text": text,
                    }
                },
            )
            return uri
        if existing.text == text and not refresh:
            return uri
        existing.version += 1
        existing.text = text
        await self._notify_locked(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": existing.version},
                "contentChanges": [{"text": text}],
            },
        )
        return uri

    # ── 诊断推送的落地 ──────────────────────────────────────────

    def _on_publish_diagnostics(self, params: dict[str, Any]) -> None:
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        items = params.get("diagnostics")
        self._diagnostics_seq += 1
        self._diagnostics[uri] = _DiagnosticsState(
            seq=self._diagnostics_seq,
            items=[item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else [],
        )
        self._diagnostics_changed.set()

    def install_notification_handlers(self) -> None:
        """把"诊断推送"接到会话状态上（spawn 之后调一次）。

        `start()` 里调用：客户端是 spawn 造出来的，只有它能按传输绑定处理器。
        """
        transport = self._transport
        if transport is None:
            return
        transport.client.on_notification(
            "textDocument/publishDiagnostics", self._on_publish_diagnostics
        )


# ---------------------------------------------------------------------------
# 协议结果归一（纯函数，便于单测）
# ---------------------------------------------------------------------------


def normalize_locations(result: Any) -> list[dict[str, Any]]:
    """把 `textDocument/definition` 的结果归一成位置列表。

    真实服务器会回三种形态，且允许 `null`（"没有定义"）：
    `Location` / `Location[]` / `LocationLink[]`（`targetUri` + `targetSelectionRange`）。
    不归一的实现在 List 与单对象之间会静默丢一半结果。
    """
    if result is None:
        return []
    entries = result if isinstance(result, list) else [result]
    locations: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri") or entry.get("targetUri")
        if not isinstance(uri, str):
            continue
        range_ = _first_dict(entry, "range", "targetSelectionRange", "targetRange")
        if range_ is None:
            continue
        start: dict[str, Any] = _first_dict(range_, "start") or {}
        end: dict[str, Any] = _first_dict(range_, "end") or start
        locations.append(
            {
                "uri": uri,
                "start_line": _int_or_zero(start.get("line")),
                "start_character": _int_or_zero(start.get("character")),
                "end_line": _int_or_zero(end.get("line")),
                "end_character": _int_or_zero(end.get("character")),
            }
        )
    return locations


def _first_dict(source: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """按顺序取第一个**字典**字段（LSP 的 `range` / `targetRange` / `start` 都用得上）。"""
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
    return None


def _pull_items(raw: Any) -> list[dict[str, Any]]:
    """拉取式诊断的 `result` → 诊断项列表（`full` / `unchanged` 都要能处理）。"""
    if not isinstance(raw, dict):
        return []
    items = raw.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# ---------------------------------------------------------------------------
# 进程启动：复用仓库既有的隔离入口
# ---------------------------------------------------------------------------


async def _spawn_in_sandbox(config: LspServerConfig, root: Path) -> SpawnedTransport:
    """按**既有的**子进程/隔离入口启动语言服务器。

    刻意不自己 `subprocess.Popen`：语言服务器是由模型**间接驱动**的长驻第三方
    进程。绕开隔离入口意味着它既不读清理后的环境（会继承引擎的 `JWT_SECRET`、
    数据库口令），也不进 Windows Job Object（关会话时整棵进程树不会回收）。
    因此照抄 `app/tools/sandbox.py::_run_in_sandbox_impl` 的三步：

    1. `sandboxed_env()` —— 清掉宿主凭据，HOME/TEMP 指到工作区；
    2. `child_launch_argv(resolve_level(), argv)` —— 把当前隔离档位变成 argv；
    3. `attach_job(proc, SHELL_LIMITS, label=...)` —— Job Object 兜进程树。

    第 2 步失败是 **fail-closed**（与 shell_exec 同口径：宁可拒绝执行，也不悄悄
    在没有隔离的情况下跑）。
    """
    from app.isolation import SHELL_LIMITS, attach_job, child_launch_argv, resolve_level
    from app.tools.sandbox import sandboxed_env

    argv = list(config.command)
    try:
        exec_argv = child_launch_argv(resolve_level(), argv)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：fail-closed
        raise LspStartError(
            f"语言服务器启动失败（language={config.language}）：无法按当前隔离档位包装命令：{exc}"
        ) from exc
    env = sandboxed_env()
    env.update(dict(config.extra_env))
    try:
        proc = await asyncio.create_subprocess_exec(
            *exec_argv,
            cwd=str(root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise LspStartError(
            f"语言服务器启动失败（language={config.language}, "
            f"command={config.command[0]!r}）：{exc}"
        ) from exc
    job = attach_job(proc, SHELL_LIMITS, label=f"lsp:{config.language}")
    stdout = proc.stdout
    stdin = proc.stdin
    if stdout is None or stdin is None:  # pragma: no cover - PIPE 一定给流
        proc.kill()
        raise LspStartError(
            f"语言服务器启动失败（language={config.language}）：子进程没有 stdio 管道"
        )
    client = JsonRpcClient(stdout, stdin, request_timeout=config.request_timeout)
    try:
        client.start()
    except Exception as exc:  # noqa: BLE001 - 建客户端失败也不能留下一个没人管的进程
        proc.kill()
        raise LspStartError(
            f"语言服务器启动失败（language={config.language}）：无法建立 JSON-RPC 通道：{exc}"
        ) from exc
    return SpawnedTransport(proc=proc, client=client, stderr=proc.stderr, job=job)


async def _stop_process(proc: LspProcess, *, grace: float, reason: str) -> None:
    """SIGTERM → （宽限内不退出）SIGKILL。

    POSIX 上 `terminate()` 就是 SIGTERM；Windows 上是 `TerminateProcess`（没有
    温和的等价物）。两种平台都保留"先温和、后强杀"的顺序：POSIX 下 SIGTERM 让
    语言服务器有机会把索引落盘，Windows 下至少语义一致、日志可读。
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except TimeoutError:
        logger.warning("语言服务器未在 %gs 内退出（%s），改为强杀", grace, reason)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except (TimeoutError, ProcessLookupError, OSError):
        logger.warning("强杀语言服务器后仍未确认退出（%s）", reason)


# ---------------------------------------------------------------------------
# 会话路由表
# ---------------------------------------------------------------------------


class LspSessionManager:
    """`(项目根, 语言) → 会话` 的路由表。

    路由表必须先于进程存在、后于进程消失：

    * `acquire` 里启动失败**不写路由表**（否则下次拿到一个起不来的会话）；
    * `unload` 里**先 pop 再关进程** —— 反过来的话，关闭期间的新请求仍能从表里
      拿到这个半死的会话，表现为"偶发请求消失"。
    """

    def __init__(self, *, spawn: SpawnFn | None = None) -> None:
        self._spawn = spawn
        self._routes: dict[tuple[str, str], LspSession] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def session_for(self, root: Path | str, language: str) -> LspSession | None:
        return self._routes.get(session_key(root, language))

    def keys(self) -> list[tuple[str, str]]:
        return list(self._routes.keys())

    async def acquire(
        self,
        root: Path | str,
        language: str,
        *,
        config: LspServerConfig | None = None,
    ) -> LspSession:
        """取（必要时懒启动）会话。同键并发时只会启动一次。"""
        key = session_key(root, language)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._routes.get(key)
            if existing is not None and existing.alive:
                return existing
            if existing is not None:
                # 死掉的会话（进程退了/被超时终止）：先撤路由再清理，与 unload 同序
                self._routes.pop(key, None)
                await existing.aclose(reason="会话已失效")
            if config is None:
                # 未配置时在这里抛出可读的 LspConfigError，且不写路由表
                from app.tools.lsp.config import load_server_config

                config = load_server_config(language)
            session = LspSession(
                root=Path(root),
                language=language,
                config=config,
                spawn=self._spawn,
            )
            await session.start()
            self._routes[key] = session
            return session

    async def unload(self, key: tuple[str, str]) -> bool:
        """卸载一个会话：**先撤路由，再关进程**。返回是否真的卸载了。"""
        session = self._routes.pop(key, None)
        if session is None:
            return False
        await session.aclose(reason="会话被卸载")
        return True

    async def unload_all(self) -> int:
        """卸载全部（进程/测试退出用）。返回卸载数量。"""
        keys = list(self._routes.keys())
        for key in keys:
            await self.unload(key)
        return len(keys)


#: 进程内单例（工具层用它；测试可经 `tools.set_manager` 换掉）
manager = LspSessionManager()
