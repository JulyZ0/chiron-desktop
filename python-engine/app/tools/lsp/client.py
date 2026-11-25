"""LSP 的 **JSON-RPC over stdio** 客户端（最小实现）。

## 为什么不复用 `app/mcp/client.py`

MCP 的 stdio 传输是**按行**分帧（一行一个 JSON），LSP 规定的是
``Content-Length: N\\r\\n\\r\\n`` + N 字节消息体。两者不能共用读循环：

* 用行分隔读 LSP 流 → 读到的是 `Content-Length: 123` 这一行，不是消息；
* 用长度头读 MCP 流 → 头根本不存在，直接卡死。

所以这里只共用**结构**（读循环 + id 表 + 挂起未来 + 后台任务集合），
分帧与错误形态按 LSP 自己的规矩写。

## 边界（刻意不做的事）

* **不做 `Content-Type` 校验**：头里的 `Content-Type` 是可选且常被服务器写成
  `application/vscode-jsonrpc; charset=utf-8`，校验它只会制造无谓的兼容问题。
* **不做请求重放/自动重连**：LSP 的请求带位置与文档版本，重放会答非所问；
  进程没了就是会话没了（`session.py` 负责重建）。
* **不做 `$/cancelRequest`**：请求超时在本项目里的语义是"这个进程坏了，杀掉重建"
  （见 `session.py`），发取消通知来解决超时没有意义。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 单条消息体的上限。`Content-Length` 是**对端**给的：一个坏掉/被喂了脏数据的
#: 语言服务器声明 `Content-Length: 999999999999` 会让 `readexactly` 直接吃掉几个 G
#: 内存（等于一条 OOM 级的拒绝服务）。所以在读消息体**之前**就拒绝。
MAX_CONTENT_LENGTH = 8 * 1024 * 1024

CONTENT_LENGTH = "content-length"


class LspProtocolError(RuntimeError):
    """对端发的不是合法 LSP 分帧/JSON-RPC（可读消息，不泄露整段原始字节）。"""


class LspTransportError(RuntimeError):
    """管道层面的失败（写不进去、读不到）。"""


class LspRpcError(RuntimeError):
    """服务器返回的 `error` 对象（归一后的形态）。"""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: Any = None,
        request_id: int | None = None,
    ) -> None:
        super().__init__(f"语言服务器返回错误 [{code}] {message}")
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id


class LspTimeoutError(TimeoutError):
    """请求超时。继承 `TimeoutError`：上层按 `TimeoutError` 兜底的地方不需要改。"""


class Writer(Protocol):
    """`asyncio.StreamWriter` 的最小接口（测试用内存替身实现同样两个方法）。"""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


# ---------------------------------------------------------------------------
# 分帧
# ---------------------------------------------------------------------------


def encode_message(message: Mapping[str, Any]) -> bytes:
    """`dict` → `Content-Length` 头 + JSON 消息体。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


async def read_headers(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """读消息头；对端正常关闭（第一条就是 EOF）时返回 None。"""
    headers: dict[str, str] = {}
    while True:
        try:
            line = await reader.readline()
        except ValueError as exc:
            # StreamReader 的行长度上限（默认 64KiB）被打爆 —— 头不可能这么长
            raise LspProtocolError(f"消息头过长或不合法：{exc}") from None
        if line == b"":
            if not headers:
                return None
            raise LspProtocolError("消息头没有以空行结束就遇到 EOF")
        if line in (b"\r\n", b"\n"):
            return headers
        name, sep, value = line.decode("ascii", "replace").partition(":")
        if not sep:
            raise LspProtocolError(f"消息头行缺少冒号：{line[:40]!r}")
        headers[name.strip().lower()] = value.strip()


def parse_content_length(
    headers: Mapping[str, str], max_content_length: int = MAX_CONTENT_LENGTH
) -> int:
    """校验并取回消息体长度（缺头/非整数/越界都在这里拒绝）。"""
    raw = headers.get(CONTENT_LENGTH)
    if raw is None:
        raise LspProtocolError("消息头缺少 Content-Length")
    try:
        length = int(raw)
    except ValueError:
        raise LspProtocolError(f"Content-Length 不是整数：{raw!r}") from None
    if length < 0:
        raise LspProtocolError(f"Content-Length 为负数：{length}")
    if length > max_content_length:
        raise LspProtocolError(
            f"Content-Length {length} 超过上限 {max_content_length} 字节，拒绝读取"
            "（对端可能已损坏，读下去会打爆内存）"
        )
    return length


async def read_message(
    reader: asyncio.StreamReader,
    *,
    max_content_length: int = MAX_CONTENT_LENGTH,
) -> dict[str, Any] | None:
    """读一条消息；EOF 返回 None，分帧/JSON 不合法抛 `LspProtocolError`。"""
    headers = await read_headers(reader)
    if headers is None:
        return None
    length = parse_content_length(headers, max_content_length)
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise LspProtocolError(
            f"消息体不完整：声明 {length} 字节，实得 {len(exc.partial)} 字节"
        ) from None
    try:
        message = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LspProtocolError(f"消息体不是合法 JSON：{exc}") from None
    if not isinstance(message, dict):
        raise LspProtocolError("JSON-RPC 消息必须是对象")
    return message


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def _normalize_error(error: Any, request_id: int) -> LspRpcError:
    """把 `error` 的三种真实形态（对象 / 字符串 / 其它）归一成同一个异常。

    JSON-RPC 规定 `error` 是 `{code, message, data}` 对象，但真实服务器会回
    `"error": "boom"`（早期实现）、`{"message": ...}`（漏 code）、甚至 `{"error": null}`。
    不归一的话上层只能 `str(exc)` 拿到 `'NoneType' object is not subscriptable`。
    """
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        data = error.get("data")
        resolved_code = code if isinstance(code, int) else -32603
        resolved_message = (
            message if isinstance(message, str) and message else f"（错误消息缺失，data={data!r}）"
        )
        return LspRpcError(resolved_code, resolved_message, data=data, request_id=request_id)
    if isinstance(error, str):
        return LspRpcError(0, error, request_id=request_id)
    return LspRpcError(-32603, f"无法识别的错误形态：{error!r}", request_id=request_id)


ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
NotificationHandler = Callable[[dict[str, Any]], Any]


class JsonRpcClient:
    """一条 stdio 连接上的 JSON-RPC 会话（读循环 + id 配对 + 写锁）。

    并发语义：**写侧串行**（一把写锁，避免两条消息的头/体交错），**请求本身可以
    并发**（JSON-RPC 的 id 就是为此存在）。会话级的"并发上限 1"在 `session.py`
    里另有一把锁 —— 那是语言服务器的状态约束，不是协议约束。
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: Writer,
        *,
        request_timeout: float = 20.0,
        max_content_length: int = MAX_CONTENT_LENGTH,
        name: str = "lsp",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._request_timeout = request_timeout
        self._max_content_length = max_content_length
        self._name = name
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._background: set[asyncio.Task[None]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._server_request_handler: ServerRequestHandler | None = None
        self._closed = False
        self.last_protocol_error = ""
        self.request_count = 0

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """起读循环。必须由创建者调用一次（客户端不拥有传输，不自己连）。"""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        self._notification_handlers[method] = handler

    def on_server_request(self, handler: ServerRequestHandler) -> None:
        self._server_request_handler = handler

    async def aclose(self, *, reason: str = "客户端已关闭") -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(LspTransportError(reason))
        tasks = [*self._background]
        if self._reader_task is not None:
            tasks.append(self._reader_task)
            self._reader_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        """挂起中的请求数（用例断言"超时/关闭后没有泄漏"）。"""
        return len(self._pending)

    # ── 发送 ────────────────────────────────────────────────────

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._write(message)

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """发一条请求并等它的响应（按 id 配对，不按到达顺序）。"""
        if not method:
            raise ValueError("method 不能为空")
        limit = self._request_timeout if timeout is None else timeout
        if limit <= 0:
            raise ValueError(f"timeout 必须为正数，实得 {limit}")
        self._ensure_open()
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self.request_count += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        try:
            await self._write(message)
            return await asyncio.wait_for(asyncio.shield(future), timeout=limit)
        except TimeoutError as exc:
            # 超时是**会话级**故障（见 session.py），这里只负责清账 + 抛可读异常。
            # 必须显式摘掉挂起表项：不摘的话它会一直留到会话关闭，
            # 挂起表随超时次数增长，而且关闭时还会给一个"没人等的未来"设异常。
            self._pending.pop(request_id, None)
            future.cancel()
            raise LspTimeoutError(
                f"语言服务器在 {limit:g}s 内没有响应方法 {method}（id={request_id}）"
            ) from exc
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

    async def _write(self, message: Mapping[str, Any]) -> None:
        self._ensure_open()
        data = encode_message(message)
        try:
            async with self._write_lock:
                self._writer.write(data)
                await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            raise LspTransportError(f"写入语言服务器失败：{exc}") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConnectionError(f"{self._name} 连接已关闭")

    # ── 读循环与分派 ────────────────────────────────────────────

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    message = await read_message(
                        self._reader, max_content_length=self._max_content_length
                    )
                except LspProtocolError as exc:
                    # 分帧仍然完好（消息体按长度吃掉了），因此只丢这条、继续读。
                    # 若在这里结束任务，一次坏消息就会让**之后所有**请求超时。
                    self.last_protocol_error = str(exc)
                    logger.warning("%s：丢弃一条不合法消息：%s", self._name, exc)
                    continue
                if message is None:
                    break
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            self.last_protocol_error = str(exc)
            logger.warning("%s：读取失败：%s", self._name, exc)
        finally:
            self._fail_pending(LspTransportError(f"{self._name} 的 stdout 已结束，没有响应"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                self._spawn(self._handle_server_request(message))
            else:
                self._handle_notification(method, message)
            return

        request_id = message.get("id")
        if not isinstance(request_id, int) or isinstance(request_id, bool):
            logger.warning("%s：忽略 id 不合法的响应（id=%r）", self._name, request_id)
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            # 超时后才到的迟到响应：丢掉即可（未来已经没人等它）
            logger.debug("%s：忽略迟到/未知的响应 id=%r", self._name, request_id)
            return
        has_result = "result" in message
        has_error = "error" in message
        if has_result and has_error:
            future.set_exception(
                LspProtocolError(f"响应 id={request_id} 同时带 result 与 error")
            )
        elif has_error:
            future.set_exception(_normalize_error(message.get("error"), request_id))
        elif has_result:
            future.set_result(message.get("result"))
        else:
            future.set_exception(LspProtocolError(f"响应 id={request_id} 既无 result 也无 error"))

    def _handle_notification(self, method: str, message: dict[str, Any]) -> None:
        handler = self._notification_handlers.get(method)
        if handler is None:
            logger.debug("%s：忽略通知 %s", self._name, method)
            return
        # 交给处理器的是 **params**（不是整条消息）：通知的语义全在 params 里，
        # 让每个处理器各自 `message["params"]` 只会让漏取参数成为常态 bug。
        params = message.get("params")
        payload = params if isinstance(params, dict) else {}
        try:
            result = handler(payload)
        except Exception:  # noqa: BLE001 - 通知处理器坏了不该拖垮读循环
            logger.exception("%s：通知处理器 %s 抛异常", self._name, method)
            return
        if inspect.isawaitable(result):
            self._spawn(_await_silently(result, label=method))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """服务器反向请求。**必须回应**，否则对端会一直等（表现为"启动后没动静"）。"""
        request_id = message.get("id")
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        try:
            if self._server_request_handler is not None:
                produced = self._server_request_handler(message)
                payload = await produced if inspect.isawaitable(produced) else produced
                if isinstance(payload, dict):
                    response.update(payload)
                else:
                    response["result"] = None
            else:
                response.update(self._default_server_response(message))
        except Exception:  # noqa: BLE001 - 对端请求处理失败也要回一条错误，不能静默
            logger.exception("%s：处理服务器请求失败", self._name)
            response.pop("result", None)
            response["error"] = {"code": -32603, "message": "客户端内部错误"}
        try:
            await self._write(response)
        except (ConnectionError, OSError, LspTransportError):
            logger.debug("%s：回写服务器请求的响应失败（连接可能已关）", self._name)

    @staticmethod
    def _default_server_response(message: dict[str, Any]) -> dict[str, Any]:
        """没有自定义处理器时的最小应答集。

        `workspace/configuration` 回一组 null：**不能**回 `-32601`，因为多数服务器
        把它当启动的必经步骤，收到错误会直接放弃初始化（表现为"initialize 成功、
        但之后什么都不推"）。其余能力协商类请求回 null 即可（我们一个都不声明，
        服务器会退化成"不开这些特性"）。
        """
        method = message.get("method")
        if method == "workspace/configuration":
            params = message.get("params") or {}
            items = params.get("items") if isinstance(params, dict) else None
            count = len(items) if isinstance(items, list) else 0
            return {"result": [None] * count}
        if method in (
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
            "window/showMessageRequest",
            "workspace/workspaceFolders",
        ):
            return {"result": None}
        return {"error": {"code": -32601, "message": f"客户端不支持服务器请求 {method}"}}

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _fail_pending(self, error: BaseException) -> None:
        """让所有等待者立刻拿到失败原因（而不是各自等到超时）。"""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)


async def _await_silently(awaitable: Awaitable[Any], *, label: str) -> None:
    """跑一个后台通知协程；失败只记日志（读循环不因它中断）。"""
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("通知处理失败：%s", label)
