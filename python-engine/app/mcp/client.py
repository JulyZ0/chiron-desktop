"""Multi-server MCP client for stdio and HTTP SSE transports."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import socket
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeGuard, cast

import httpx

MCP_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION, "2024-11-05")
MCP_REQUEST_TIMEOUT = 30.0
MCP_CANCEL_TIMEOUT = 1.0
RequestId = int | str
MessageHandler = Callable[[dict[str, Any]], Any]

logger = logging.getLogger(__name__)


class MCPProtocolError(RuntimeError):
    """The peer violated the JSON-RPC/MCP message contract."""


class MCPError(RuntimeError):
    """A JSON-RPC error response with its wire identity preserved."""

    def __init__(self, error: dict[str, Any], request_id: RequestId):
        self.code = error.get("code")
        self.data = error.get("data")
        self.request_id = request_id
        super().__init__(f"MCP error {self.code}: {error.get('message', 'unknown')}")


class MCPRequestCancelledError(RuntimeError):
    """The MCP server cancelled a pending client request."""


def _valid_request_id(value: Any) -> TypeGuard[RequestId]:
    return type(value) in (int, str)


def _result_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MCPProtocolError("MCP response result must be an object")
    return value


def _is_private_ip(host: str) -> bool:
    """Check if a host resolves to a private/reserved IP address (SSRF protection)."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    try:
        addrinfos = socket.getaddrinfo(host, None, socket.AI_ADDRCONFIG)
        for family, _, _, _, sockaddr in addrinfos:
            ip_str = sockaddr[0] if family in (socket.AF_INET, socket.AF_INET6) else None
            if ip_str:
                try:
                    addr = ipaddress.ip_address(ip_str)
                    if addr.is_private or addr.is_loopback or addr.is_link_local:
                        return True
                except ValueError:
                    continue
    except (socket.gaierror, OSError):
        pass
    return False


@dataclass
class ServerDef:
    """MCP server configuration."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # "stdio" | "http_sse"
    url: str = ""  # HTTP SSE endpoint URL (used when transport="http_sse")
    #: Engine-direct connections require an explicit read-only declaration.
    read_only: bool = False
    #: Server-local tool names excluded at both registration and call time.
    deny_tools: frozenset[str] = frozenset()
    #: Trusted built-ins skip the user command allowlist, not path validation.
    builtin: bool = False


def assert_server_connectable(server: ServerDef, *, role: str = "engine") -> None:
    """Only the isolated broker role may connect directly to a non-read-only server."""
    if role == "broker" or server.read_only:
        return
    raise PermissionError(
        f"MCP server '{server.name}' is not declared read-only — refusing to connect from "
        "the engine. Credentials for write/external tools must not live in agent reach; "
        'declare "read_only": true in the plugin config if this server only reads.'
    )


@dataclass
class MCPTool:
    """A tool provided by an MCP server."""

    name: str  # Namespaced: {server_name}_{tool_name}
    description: str
    input_schema: dict[str, Any]
    server_name: str
    local_name: str  # Original tool name on the server
    #: Raw MCP annotations are retained so call-time policy can inspect every hint.
    annotations: dict[str, Any] = field(default_factory=dict)


def is_tool_denied(server: ServerDef, local_name: str) -> bool:
    """Match deny rules against both bare and server-prefixed local names."""
    prefix = f"{server.name}_"
    bare = local_name[len(prefix) :] if local_name.startswith(prefix) else local_name
    return local_name in server.deny_tools or bare in server.deny_tools


def is_tool_destructive(tool: MCPTool) -> bool:
    """Missing, malformed, or contradictory safety hints are destructive."""
    annotations = tool.annotations or {}
    if annotations.get("destructiveHint") is True:
        return True
    if annotations.get("readOnlyHint") is True:
        return False
    return True


class HTTPSSEConnection:
    """Connection to an MCP server over HTTP SSE transport."""

    def __init__(self, url: str, name: str):
        self.url = url.rstrip("/")
        self.name = name
        self.protocol_version: str | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=MCP_REQUEST_TIMEOUT)
        self._session_id: str | None = None
        self._closed = False

        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname
        if host and _is_private_ip(host):
            raise ValueError(
                f"SSRF protection: connecting to private/restricted IP is forbidden "
                f"(host={host}, server={name})"
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, message: dict[str, Any]) -> httpx.Response:
        if self._closed:
            raise ConnectionError(f"MCP server {self.name} is closed")
        async with self._lock:
            response = await self._client.post(
                f"{self.url}/messages", json=message, headers=self._headers()
            )
            response.raise_for_status()
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id
            return response

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._post(message)

    async def send_jsonrpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = MCP_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request via HTTP POST and validate its response identity."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._req_id += 1
        request_id = self._req_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        reason = "cancelled"
        try:
            response = await asyncio.wait_for(self._post(request), timeout=timeout)
            data = response.json()
        except TimeoutError:
            reason = "timeout"
            await self._cancel(request_id, reason)
            raise
        except asyncio.CancelledError:
            await self._cancel(request_id, reason)
            raise
        except Exception:
            await self._cancel(request_id, reason)
            raise

        if not isinstance(data, dict) or not _valid_request_id(data.get("id")):
            raise MCPProtocolError("MCP HTTP response has an invalid request id")
        if data["id"] != request_id:
            raise MCPProtocolError(
                f"MCP HTTP response id {data['id']!r} does not match request {request_id}"
            )
        if ("result" in data) == ("error" in data):
            raise MCPProtocolError("MCP HTTP response must contain exactly one of result/error")
        if "error" in data:
            error = data["error"]
            if not isinstance(error, dict):
                raise MCPProtocolError("MCP HTTP error must be an object")
            raise MCPError(error, request_id)
        return _result_dict(data["result"])

    async def _cancel(self, request_id: RequestId, reason: str) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(
                    self.send_notification(
                        "notifications/cancelled", {"requestId": request_id, "reason": reason}
                    )
                ),
                timeout=MCP_CANCEL_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - cancellation must preserve the original failure
            logger.warning("MCP %s: failed to send cancellation: %s", self.name, type(exc).__name__)

    async def close(self) -> None:
        self._closed = True
        await self._client.aclose()


class ServerConnection:
    """A single stdio MCP process with one reader and ID-keyed pending requests."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        name: str,
        job: Any = None,
        *,
        notification_handler: MessageHandler | None = None,
        request_handler: MessageHandler | None = None,
    ):
        self.proc = proc
        self.name = name
        self.protocol_version: str | None = None
        self._job = job
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._stdin = cast("asyncio.StreamWriter", proc.stdin)
        self._stdout = cast("asyncio.StreamReader", proc.stdout)
        self._stderr = cast("asyncio.StreamReader", proc.stderr)
        self._notification_handler = notification_handler
        self._request_handler = request_handler
        self._pending: dict[RequestId, asyncio.Future[dict[str, Any]]] = {}
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        try:
            async for line in self._stderr:
                if line.strip():
                    logger.warning("MCP stderr [%s]: %s", self.name, line.decode().rstrip())
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception:
            pass

    def _ensure_open(self) -> None:
        if self._closed or self._reader_task.done():
            raise ConnectionError(f"MCP server {self.name} is closed")

    def _fail_pending(self, error: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def _spawn_handler(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _read_stdout(self) -> None:
        try:
            while line := await self._stdout.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("MCP %s: ignoring malformed stdout JSON", self.name)
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every waiter needs the transport failure
            self._fail_pending(ConnectionError(f"MCP server {self.name} stdout failed: {exc}"))
        finally:
            self._fail_pending(ConnectionError(f"No response from MCP server {self.name}"))

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            logger.warning("MCP %s: ignoring invalid JSON-RPC message", self.name)
            return
        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                if _valid_request_id(message["id"]):
                    self._spawn_handler(self._handle_server_request(message))
                else:
                    logger.warning("MCP %s: ignoring server request with invalid id", self.name)
            elif method == "notifications/cancelled":
                self._handle_server_cancellation(message.get("params"))
                self._spawn_handler(self._handle_notification(message))
            else:
                self._spawn_handler(self._handle_notification(message))
            return

        request_id = message.get("id")
        if not _valid_request_id(request_id):
            logger.warning("MCP %s: ignoring response with invalid id", self.name)
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            logger.debug("MCP %s: ignoring unknown or late response id=%r", self.name, request_id)
            return
        if ("result" in message) == ("error" in message):
            future.set_exception(
                MCPProtocolError("MCP response must contain exactly one of result/error")
            )
        elif "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                future.set_exception(MCPError(error, request_id))
            else:
                future.set_exception(MCPProtocolError("MCP error response must be an object"))
        else:
            try:
                future.set_result(_result_dict(message["result"]))
            except MCPProtocolError as exc:
                future.set_exception(exc)

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        try:
            if self._notification_handler is not None:
                result = self._notification_handler(message)
                if inspect.isawaitable(result):
                    await result
            elif message.get("method") == "notifications/progress":
                logger.debug("MCP %s progress: %s", self.name, message.get("params"))
        except Exception:  # noqa: BLE001 - observer failures must not break request routing
            logger.exception("MCP %s notification handler failed", self.name)

    def _handle_server_cancellation(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        request_id = params.get("requestId")
        if not _valid_request_id(request_id):
            return
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            reason = params.get("reason", "cancelled")
            future.set_exception(MCPRequestCancelledError(f"Request {request_id} cancelled by MCP server: {reason}"))

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = cast("RequestId", message["id"])
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        try:
            if self._request_handler is None:
                response["error"] = {"code": -32601, "message": "Method not found"}
            else:
                result = self._request_handler(message)
                if inspect.isawaitable(result):
                    result = await result
                response["result"] = _result_dict(result)
        except Exception as exc:  # noqa: BLE001 - peer receives an error, reader keeps running
            logger.warning("MCP %s server request handler failed: %s", self.name, type(exc).__name__)
            response.pop("result", None)
            response["error"] = {"code": -32603, "message": "Internal error"}
        try:
            await self._write_message(response)
        except (ConnectionError, OSError):
            pass

    async def _write_message(self, message: dict[str, Any]) -> None:
        self._ensure_open()
        async with self._lock:
            self._stdin.write((json.dumps(message) + "\n").encode())
            await self._stdin.drain()

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write_message(message)

    async def send_jsonrpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = MCP_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Send one request; notifications and other responses never consume its result."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._ensure_open()
        self._req_id += 1
        request_id = self._req_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._write_message(request)
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.CancelledError:
            await self._cancel(request_id, "cancelled")
            raise
        except TimeoutError:
            await self._cancel(request_id, "timeout")
            raise
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

    async def _cancel(self, request_id: RequestId, reason: str) -> None:
        future = self._pending.pop(request_id, None)
        if future is None:
            return
        if not future.done():
            future.cancel()
        if self._closed:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(
                    self.send_notification(
                        "notifications/cancelled", {"requestId": request_id, "reason": reason}
                    )
                ),
                timeout=MCP_CANCEL_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - preserve timeout/cancellation as the primary error
            logger.warning("MCP %s: failed to send cancellation: %s", self.name, type(exc).__name__)

    async def initialize(self, *, timeout: float = MCP_REQUEST_TIMEOUT) -> str:
        return await _initialize_connection(self, timeout=timeout)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(ConnectionError(f"MCP server {self.name} is closed"))
        tasks = [self._reader_task, self._stderr_task, *self._handler_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self.proc.kill()
        if self._job is not None:
            try:
                self._job.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP %s: job object close failed: %s", self.name, exc)
            self._job = None


async def _initialize_connection(
    conn: ServerConnection | HTTPSSEConnection, *, timeout: float = MCP_REQUEST_TIMEOUT
) -> str:
    if conn.protocol_version is not None:
        raise MCPProtocolError("MCP connection is already initialized")
    capabilities: dict[str, Any] = {}
    if isinstance(conn, ServerConnection) and conn._request_handler is not None:  # noqa: SLF001
        capabilities["sampling"] = {}
    result = await conn.send_jsonrpc(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "clientInfo": {"name": "chiron-python", "version": "3.0.0"},
            "capabilities": capabilities,
        },
        timeout=timeout,
    )
    version = result.get("protocolVersion")
    if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise MCPProtocolError(f"Unsupported MCP protocol version: {version!r}")
    conn.protocol_version = version
    await conn.send_notification("notifications/initialized")
    return version


class MCPClient:
    """Manages connections to multiple MCP servers and their tools."""

    def __init__(
        self,
        servers: list[ServerDef],
        *,
        role: str = "engine",
        notification_handler: MessageHandler | None = None,
        request_handler: MessageHandler | None = None,
    ):
        self._role = role
        self._servers = servers
        self._conns: dict[str, ServerConnection | HTTPSSEConnection] = {}
        self._tools: list[MCPTool] = []
        self._notification_handler = notification_handler
        self._request_handler = request_handler

    async def start(self) -> None:
        """Connect to all configured MCP servers and discover their tools."""
        for server in self._servers:
            try:
                from app.mcp.credential_projection import collect_secrets, register_secrets

                register_secrets(collect_secrets(server))
                await self._connect_server(server)
            except Exception as e:
                # Connection errors can contain credentials from URLs or environment values.
                from app.mcp.credential_projection import collect_secrets, redact, safe_label

                logger.error(
                    "MCP connect %s failed: %s",
                    safe_label(server),
                    redact(str(e), collect_secrets(server)),
                )
                raise

    async def _connect_server(self, server: ServerDef) -> None:
        """Apply the direct-connection guard before selecting a transport."""
        assert_server_connectable(server, role=self._role)
        if server.transport == "http_sse":
            await self._connect_http_sse(server)
        elif server.transport == "stdio":
            await self._connect_stdio(server)
        else:
            raise ValueError(f"Unsupported MCP transport: {server.transport}")

    async def _connect_http_sse(self, server: ServerDef) -> None:
        """Connect to an MCP server via HTTP SSE."""
        if not server.url:
            raise ValueError("HTTP SSE transport requires 'url' in ServerDef")
        conn = HTTPSSEConnection(server.url, server.name)
        self._conns[server.name] = conn
        try:
            await _initialize_connection(conn)
            tools_result = await conn.send_jsonrpc("tools/list", None)
        except BaseException:
            self._conns.pop(server.name, None)
            await conn.close()
            raise
        raw_tools = tools_result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (HTTP SSE): %d tools", server.name, len(raw_tools))

    async def _connect_stdio(self, server: ServerDef) -> None:
        """Connect to an MCP server via stdio (subprocess)."""
        # User-configured commands require the administrator allowlist.
        from app.tools.ssrf import _command_path_ok, command_allowed

        if server.builtin:
            if not _command_path_ok(server.command):
                raise PermissionError(
                    f"内置插件命令 {server.command!r} 不是裸 basename 或绝对路径"
                )
        elif not command_allowed(server.command):
            raise PermissionError(
                f"MCP server command {server.command!r} not allowed: add its basename to the "
                "plugin command allowlist in admin settings (takes effect immediately, no restart)"
            )
        # Never inherit engine credentials into a third-party stdio process.
        from app.mcp.credential_projection import project_stdio_env
        from app.tools.sandbox import sandboxed_env

        env = project_stdio_env(server, sandboxed_env())

        from app.plugins.audit import record_plugin

        try:
            proc = await asyncio.create_subprocess_exec(
                server.command,
                *server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            # 拉起插件进程失败同样要留痕（谁、哪个插件、哪个命令、为什么失败）
            record_plugin(plugin=server.name, action="spawn", success=False, error=str(exc))
            raise
        # The connection owns the Job Object so process-tree cleanup survives until close.
        from app.isolation import COMMAND_LIMITS, attach_job

        job = attach_job(proc, COMMAND_LIMITS, label=f"mcp:{server.name}")
        # 成功拉起：这是"某租户在某会话里让宿主机起了一个插件进程"的事实记录
        record_plugin(plugin=server.name, action="spawn", success=True)
        conn = ServerConnection(
            proc,
            server.name,
            job=job,
            notification_handler=self._notification_handler,
            request_handler=self._request_handler,
        )
        self._conns[server.name] = conn
        try:
            await conn.initialize()
            result = await conn.send_jsonrpc("tools/list", None)
        except BaseException:
            self._conns.pop(server.name, None)
            await conn.close()
            raise
        raw_tools = result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (stdio): %d tools", server.name, len(raw_tools))

    def _register_tools(self, server: ServerDef, raw_tools: list[dict[str, Any]]) -> None:
        """Register non-denied tools, preserving local names and raw annotations."""
        prefix = f"{server.name}_"
        for i, t in enumerate(raw_tools):
            local_name = t.get("name", f"unnamed_{i}")
            if is_tool_denied(server, local_name):
                logger.info(
                    "MCP tool skipped: %s (denied by %s policy)", local_name, server.name
                )
                continue
            namespaced = local_name if local_name.startswith(prefix) else f"{prefix}{local_name}"
            raw_annotations = t.get("annotations")
            tool = MCPTool(
                name=namespaced,
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=server.name,
                local_name=local_name,
                annotations=raw_annotations if isinstance(raw_annotations, dict) else {},
            )
            self._tools.append(tool)
            logger.info("MCP tool discovered: %s (%s)", tool.name, server.name)

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = MCP_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Recheck deny/destructive policy at call time, then propagate timeout/cancellation."""
        for tool in self._tools:
            if tool.name != tool_name:
                continue
            server = next((s for s in self._servers if s.name == tool.server_name), None)
            if server is None:
                return {"error": f"MCP server {tool.server_name} not configured"}

            # 第二道：deny 策略复查（配置可能在注册之后改严）
            if is_tool_denied(server, tool.local_name):
                logger.warning(
                    "MCP tool call refused: %s (denied by %s policy)", tool.local_name, server.name
                )
                return {
                    "error": f"Tool {tool_name} is denied by the {server.name} policy",
                    "denied": True,
                }

            # 第二道：只读 server 与破坏性自述矛盾 ⇒ 拒绝并留痕
            if server.read_only and is_tool_destructive(tool):
                annotations = tool.annotations or {}
                if annotations.get("destructiveHint") is True:
                    logger.error(
                        "MCP tool call refused: %s declares destructiveHint but server %s "
                        "is declared read-only — contradictory declarations",
                        tool.local_name,
                        server.name,
                    )
                    return {
                        "error": (
                            f"Tool {tool_name} declares destructiveHint but server "
                            f"'{server.name}' is declared read-only; refusing to call"
                        ),
                        "denied": True,
                    }
                # 只是"没说"，不是"说会写"：放行但标注，交上游处置
                logger.info(
                    "MCP tool %s/%s has no readOnlyHint; proceeding with annotation flag",
                    server.name,
                    tool.local_name,
                )

            conn = self._conns.get(tool.server_name)
            if not conn:
                return {"error": f"MCP server {tool.server_name} not connected"}
            return await conn.send_jsonrpc(
                "tools/call",
                {"name": tool.local_name, "arguments": arguments},
                timeout=timeout,
            )
        return {"error": f"Tool {tool_name} not found on any MCP server"}

    def annotations_for(self, tool_name: str) -> dict[str, Any]:
        """Expose the engine's destructive inference without altering protocol results."""
        for tool in self._tools:
            if tool.name != tool_name:
                continue
            destructive = is_tool_destructive(tool)
            return {
                **(tool.annotations or {}),
                "destructive": destructive,
                "readOnlyHint": not destructive,
            }
        return {}

    async def close(self) -> None:
        """Shut down all MCP server connections."""
        for name, conn in self._conns.items():
            try:
                await conn.close()
            except Exception as e:
                # Server definitions are unavailable here, so log only the exception type.
                logger.warning("Error closing MCP server %s: %s", name, type(e).__name__)
        self._conns.clear()


async def load_mcp_config(config_path: str) -> list[ServerDef]:
    """Load MCP server definitions from a JSON config file."""
    from pathlib import Path

    p = Path(config_path)
    if not p.exists():
        return []

    data = json.loads(p.read_text(encoding="utf-8"))
    servers = []
    for s in data.get("mcp_servers", []):
        servers.append(
            ServerDef(
                name=s["name"],
                command=s["command"],
                args=s.get("args", []),
                env=s.get("env", {}),
                transport=s.get("transport", "stdio"),
                url=s.get("url", ""),
                read_only=bool(s.get("read_only", False)),
            )
        )
    return servers
