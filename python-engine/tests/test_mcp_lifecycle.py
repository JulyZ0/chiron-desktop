"""MCP stdio request lifecycle and JSON-RPC correlation regressions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import app.mcp.client as client_module
from app.mcp.client import (
    MCP_PROTOCOL_VERSION,
    HTTPSSEConnection,
    MCPClient,
    ServerConnection,
    ServerDef,
)


class _Stdin:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _Process:
    def __init__(self, stdin: _Stdin, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=1.0)


def _connection(
    *,
    notification_handler: Callable[[dict[str, Any]], Any] | None = None,
    request_handler: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[ServerConnection, _Stdin, asyncio.StreamReader]:
    stdin = _Stdin()
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    proc = _Process(stdin, stdout, stderr)
    conn = ServerConnection(
        proc,
        "test",
        notification_handler=notification_handler,
        request_handler=request_handler,
    )
    return conn, stdin, stdout


def _response(request_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def test_initialize_negotiates_version_then_sends_initialized() -> None:
    conn, stdin, stdout = _connection()
    try:
        task = asyncio.create_task(conn.initialize(timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 1)
        request = stdin.messages[0]
        assert request["method"] == "initialize"
        assert request["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION

        stdout.feed_data(
            json.dumps(_response(request["id"], {"protocolVersion": "2024-11-05"})).encode() + b"\n"
        )
        await task

        assert conn.protocol_version == "2024-11-05"
        assert stdin.messages[1] == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    finally:
        await conn.close()


async def test_initialize_rejects_unsupported_version_without_initialized() -> None:
    conn, stdin, stdout = _connection()
    try:
        task = asyncio.create_task(conn.initialize(timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 1)
        stdout.feed_data(
            json.dumps(_response(stdin.messages[0]["id"], {"protocolVersion": "2099-01-01"})).encode()
            + b"\n"
        )
        with pytest.raises(RuntimeError, match="protocol version"):
            await task
        assert [message["method"] for message in stdin.messages] == ["initialize"]
    finally:
        await conn.close()


async def test_concurrent_responses_notifications_progress_and_sampling_stay_correlated() -> None:
    progress_seen = asyncio.Event()
    sampling_started = asyncio.Event()
    sampling_release = asyncio.Event()

    async def on_notification(message: dict[str, Any]) -> None:
        assert message["method"] == "notifications/progress"
        assert message["params"]["progressToken"] == "token-a"
        progress_seen.set()

    async def on_request(message: dict[str, Any]) -> dict[str, Any]:
        assert message["method"] == "sampling/createMessage"
        assert message["id"] == first_id
        sampling_started.set()
        await sampling_release.wait()
        return {"role": "assistant", "content": {"type": "text", "text": "sampled"}}

    conn, stdin, stdout = _connection(
        notification_handler=on_notification,
        request_handler=on_request,
    )
    try:
        first = asyncio.create_task(conn.send_jsonrpc("tools/call", {"n": 1}, timeout=1.0))
        second = asyncio.create_task(conn.send_jsonrpc("tools/call", {"n": 2}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 2)
        first_id, second_id = (message["id"] for message in stdin.messages)
        assert first_id != second_id

        stdout.feed_data(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progressToken": "token-a", "progress": 0.5},
                }
            ).encode()
            + b"\n"
        )
        stdout.feed_data(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": first_id,
                    "method": "sampling/createMessage",
                    "params": {"messages": []},
                }
            ).encode()
            + b"\n"
        )
        stdout.feed_data(json.dumps(_response(second_id, {"value": "second"})).encode() + b"\n")
        stdout.feed_data(json.dumps(_response(first_id, {"value": "first"})).encode() + b"\n")

        assert await first == {"value": "first"}
        assert await second == {"value": "second"}
        await asyncio.wait_for(progress_seen.wait(), timeout=1.0)
        await asyncio.wait_for(sampling_started.wait(), timeout=1.0)
        sampling_release.set()
        await _wait_until(lambda: len(stdin.messages) == 3)
        assert stdin.messages[2] == _response(first_id, {
            "role": "assistant",
            "content": {"type": "text", "text": "sampled"},
        })
    finally:
        sampling_release.set()
        await conn.close()


async def test_unhandled_sampling_gets_correlated_method_not_found() -> None:
    conn, stdin, stdout = _connection()
    try:
        request = asyncio.create_task(conn.send_jsonrpc("tools/call", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 1)
        request_id = stdin.messages[0]["id"]
        stdout.feed_data(
            json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "sampling/createMessage",
                "params": {"messages": []},
            }).encode()
            + b"\n"
        )
        await _wait_until(lambda: len(stdin.messages) == 2)
        assert stdin.messages[1]["id"] == request_id
        assert stdin.messages[1]["error"]["code"] == -32601
        assert not request.done()

        stdout.feed_data(json.dumps(_response(request_id, {"ok": True})).encode() + b"\n")
        assert await request == {"ok": True}
    finally:
        await conn.close()


async def test_unknown_malformed_and_late_responses_do_not_complete_new_requests() -> None:
    conn, stdin, stdout = _connection()
    try:
        task = asyncio.create_task(conn.send_jsonrpc("tools/call", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 1)
        request_id = stdin.messages[0]["id"]

        stdout.feed_data(json.dumps(_response(9999, {"value": "unknown"})).encode() + b"\n")
        stdout.feed_data(b"not-json\n")
        stdout.feed_data(json.dumps(_response(str(request_id), {"value": "wrong-id-type"})).encode() + b"\n")
        await asyncio.sleep(0)
        assert not task.done()

        stdout.feed_data(json.dumps(_response(request_id, {"value": "right"})).encode() + b"\n")
        assert await task == {"value": "right"}
        stdout.feed_data(json.dumps(_response(request_id, {"value": "late"})).encode() + b"\n")

        next_task = asyncio.create_task(conn.send_jsonrpc("tools/list", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 2)
        next_id = stdin.messages[1]["id"]
        assert next_id != request_id
        stdout.feed_data(json.dumps(_response(next_id, {"tools": []})).encode() + b"\n")
        assert await next_task == {"tools": []}
    finally:
        await conn.close()


async def test_timeout_sends_cancellation_and_late_response_is_ignored() -> None:
    conn, stdin, stdout = _connection()
    try:
        with pytest.raises(TimeoutError):
            await conn.send_jsonrpc("tools/call", {}, timeout=0.01)
        request_id = stdin.messages[0]["id"]
        assert stdin.messages[1] == {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": "timeout"},
        }
        assert conn._pending == {}  # noqa: SLF001

        stdout.feed_data(json.dumps(_response(request_id, {"value": "too-late"})).encode() + b"\n")
        task = asyncio.create_task(conn.send_jsonrpc("tools/list", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 3)
        assert stdin.messages[2]["id"] != request_id
        stdout.feed_data(json.dumps(_response(stdin.messages[2]["id"], {"tools": []})).encode() + b"\n")
        assert await task == {"tools": []}
    finally:
        await conn.close()


async def test_caller_cancellation_sends_cancellation_and_connection_remains_usable() -> None:
    conn, stdin, stdout = _connection()
    try:
        task = asyncio.create_task(conn.send_jsonrpc("tools/call", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 1)
        request_id = stdin.messages[0]["id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stdin.messages[1]["method"] == "notifications/cancelled"
        assert stdin.messages[1]["params"] == {"requestId": request_id, "reason": "cancelled"}

        stdout.feed_data(json.dumps(_response(request_id, {"value": "late"})).encode() + b"\n")
        next_task = asyncio.create_task(conn.send_jsonrpc("tools/list", {}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 3)
        stdout.feed_data(json.dumps(_response(stdin.messages[2]["id"], {"tools": []})).encode() + b"\n")
        assert await next_task == {"tools": []}
    finally:
        await conn.close()


async def test_server_cancellation_settles_only_matching_request() -> None:
    conn, stdin, stdout = _connection()
    try:
        first = asyncio.create_task(conn.send_jsonrpc("tools/call", {"n": 1}, timeout=1.0))
        second = asyncio.create_task(conn.send_jsonrpc("tools/call", {"n": 2}, timeout=1.0))
        await _wait_until(lambda: len(stdin.messages) == 2)
        first_id = stdin.messages[0]["id"]
        stdout.feed_data(
            json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": first_id, "reason": "server stopped"},
            }).encode()
            + b"\n"
        )
        with pytest.raises(RuntimeError, match="cancelled by MCP server"):
            await first
        assert not second.done()

        stdout.feed_data(json.dumps(_response(stdin.messages[1]["id"], {"ok": True})).encode() + b"\n")
        assert await second == {"ok": True}
    finally:
        await conn.close()


async def test_close_fails_pending_requests_instead_of_hanging() -> None:
    conn, _, _ = _connection()
    task = asyncio.create_task(conn.send_jsonrpc("tools/call", {}, timeout=30.0))
    await _wait_until(lambda: bool(conn._pending))  # noqa: SLF001
    await conn.close()
    with pytest.raises(ConnectionError):
        await task


async def test_connect_stdio_sends_initialized_before_tools_list(monkeypatch) -> None:
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    methods: list[str] = []

    class ScriptedStdin(_Stdin):
        def write(self, data: bytes) -> None:
            super().write(data)
            message = self.messages[-1]
            methods.append(message["method"])
            if message["method"] == "initialize":
                stdout.feed_data(
                    json.dumps(_response(message["id"], {"protocolVersion": "2024-11-05"})).encode()
                    + b"\n"
                )
            elif message["method"] == "tools/list":
                assert "notifications/initialized" in methods
                stdout.feed_data(json.dumps(_response(message["id"], {"tools": []})).encode() + b"\n")

    async def spawn(*args, **kwargs):
        return _Process(ScriptedStdin(), stdout, stderr)

    monkeypatch.setattr("app.tools.ssrf.command_allowed", lambda command: True)
    monkeypatch.setattr("app.tools.sandbox.sandboxed_env", lambda: {})
    monkeypatch.setattr("app.isolation.attach_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.plugins.audit.record_plugin", lambda **kwargs: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    server = ServerDef(name="scripted", command="python", read_only=True)
    client = MCPClient([server])
    try:
        await client._connect_stdio(server)  # noqa: SLF001
        assert methods == ["initialize", "notifications/initialized", "tools/list"]
        assert client._conns["scripted"].protocol_version == "2024-11-05"  # noqa: SLF001
    finally:
        await client.close()


async def _http_connection(monkeypatch, handler) -> HTTPSSEConnection:
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(*args, transport=transport, **kwargs),
    )
    monkeypatch.setattr(client_module, "_is_private_ip", lambda host: False)
    return HTTPSSEConnection("https://mcp.example.test", "http-test")


async def test_http_initialize_notification_and_session_header(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        requests.append(message)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json=_response(message["id"], {"protocolVersion": "2024-11-05"}),
                headers={"Mcp-Session-Id": "session-1"},
            )
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Mcp-Session-Id"] == "session-1"
        return httpx.Response(200, json=_response(message["id"], {"tools": []}))

    conn = await _http_connection(monkeypatch, handler)
    server = ServerDef(name="http", command="", transport="http_sse", url=conn.url, read_only=True)
    client = MCPClient([server])
    client._conns[server.name] = conn  # noqa: SLF001
    monkeypatch.setattr(client_module, "HTTPSSEConnection", lambda url, name: conn)
    try:
        await client._connect_http_sse(server)  # noqa: SLF001
        assert [request["method"] for request in requests] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert conn.protocol_version == "2024-11-05"
    finally:
        await client.close()


async def test_http_timeout_sends_cancellation(monkeypatch) -> None:
    messages: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        messages.append(message)
        if message["method"] == "notifications/cancelled":
            return httpx.Response(202)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    conn = await _http_connection(monkeypatch, handler)
    try:
        with pytest.raises(TimeoutError):
            await conn.send_jsonrpc("tools/call", {}, timeout=0.01)
        assert messages[1]["method"] == "notifications/cancelled"
        assert messages[1]["params"]["requestId"] == messages[0]["id"]
        assert messages[1]["params"]["reason"] == "timeout"
    finally:
        await conn.close()
