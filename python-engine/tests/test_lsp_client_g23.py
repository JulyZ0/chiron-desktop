"""G23：JSON-RPC over stdio 客户端（`app/tools/lsp/client.py`）的协议级用例。

**修前会怎样**：仓库此前完全没有 LSP / JSON-RPC 客户端（`grep -ri lsp python-engine/app`
无实现），本文件的所有 import 在修前都是 `ModuleNotFoundError`。

覆盖四件必须钉死的事：

1. **分帧**：LSP 规定 `Content-Length` 头 + `\\r\\n\\r\\n`。半包（一次 read 只到一半）、
   多包（一次到达两条）都必须处理对 —— 管道是字节流，没有"消息"这个概念。
2. **超大 Content-Length 必须拒绝**：这个头是**对端**给的，一个坏掉的/恶意的
   服务器声明 `Content-Length: 999999999999` 会让 `readexactly` 直接吃掉几个 G 内存
   （等于一条远控级的 OOM），所以要在读消息体**之前**拒绝。
3. **id 配对**：并发请求的响应可以乱序（JSON-RPC 明确允许），必须按 id 回调，
   而不是"按到达顺序塞给最早的那个未来"。
4. **超时与错误归一**：超时要抛可读异常并清掉挂起表；`error` 字段的三种形态
   （对象 / 字符串 / 缺字段）都要归一成同一种异常，否则上层只能 `str(exc)`。
"""

from __future__ import annotations

import asyncio

import pytest

from app.tools.lsp.client import (
    MAX_CONTENT_LENGTH,
    JsonRpcClient,
    LspProtocolError,
    LspRpcError,
    LspTimeoutError,
    encode_message,
    read_message,
)
from tests.test_lsp_fake_server_g23 import (
    MemServer,
    protocol_handler,
    wait_for_requests,
)


def _stream() -> asyncio.StreamReader:
    return asyncio.StreamReader()


# ---------------------------------------------------------------------------
# 分帧
# ---------------------------------------------------------------------------


async def test_分帧_半包时必须阻塞到消息体收满():
    """半包：只喂前 10 字节时 read_message 不能返回。

    修前会怎样：如果分帧是按行读（像 `app/mcp/client.py` 那样，MCP 用换行分隔），
    这里会立刻返回一条**残缺**消息或 None —— 表面"能跑"，实际把 JSON 截断了。
    """
    reader = _stream()
    message = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    data = encode_message(message)
    assert data.startswith(b"Content-Length: ")

    reader.feed_data(data[:10])
    task = asyncio.create_task(read_message(reader))
    await asyncio.sleep(0.05)
    assert not task.done(), "半包时 read_message 提前返回了（分帧没有按 Content-Length 走）"

    reader.feed_data(data[10:])
    assert await asyncio.wait_for(task, 1) == message


async def test_分帧_一次到达多条消息时逐条返回():
    """多包：两条消息粘在一个 read 里，也必须一条一条吐出来。"""
    reader = _stream()
    first = {"jsonrpc": "2.0", "id": 1, "result": "甲"}
    second = {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {}}
    reader.feed_data(encode_message(first) + encode_message(second))

    assert await asyncio.wait_for(read_message(reader), 1) == first
    assert await asyncio.wait_for(read_message(reader), 1) == second


async def test_分帧_超大ContentLength在读消息体前就被拒绝():
    """对端声明一个天文数字的长度 → 立刻协议错误，不分配内存。"""
    reader = _stream()
    reader.feed_data(b"Content-Length: %d\r\n\r\n" % (MAX_CONTENT_LENGTH + 1))
    with pytest.raises(LspProtocolError) as excinfo:
        await asyncio.wait_for(read_message(reader), 1)
    assert "上限" in str(excinfo.value)


async def test_分帧_缺少ContentLength报协议错误():
    reader = _stream()
    reader.feed_data(b"Content-Type: application/json\r\n\r\n")
    with pytest.raises(LspProtocolError):
        await asyncio.wait_for(read_message(reader), 1)


async def test_分帧_ContentLength不是整数报协议错误():
    reader = _stream()
    reader.feed_data(b"Content-Length: " + "十二".encode() + b"\r\n\r\n")
    with pytest.raises(LspProtocolError):
        await asyncio.wait_for(read_message(reader), 1)


async def test_分帧_消息体不是JSON时报协议错误():
    reader = _stream()
    reader.feed_data(b"Content-Length: 5\r\n\r\n{xxxx")
    with pytest.raises(LspProtocolError):
        await asyncio.wait_for(read_message(reader), 1)


async def test_分帧_EOF返回None而不是抛错():
    """对端正常关闭 stdin：`read_message` 返回 None，调用方据此结束读循环。"""
    reader = _stream()
    reader.feed_eof()
    assert await asyncio.wait_for(read_message(reader), 1) is None


# ---------------------------------------------------------------------------
# id 配对
# ---------------------------------------------------------------------------


async def _client(handler) -> tuple[JsonRpcClient, MemServer]:  # noqa: ANN001 - 测试内部
    server = MemServer(handler)
    server.start()
    client = JsonRpcClient(server.reader, server.writer, request_timeout=1.0)
    client.start()
    return client, server


async def test_id配对_响应乱序也能对上号():
    """两个并发请求，服务器**反序**响应：结果仍要各归各位。

    修前会怎样：按到达顺序配对（"第一个响应就是第一个请求的"）在这里会把
    `甲` 的结果发给 `乙` 的调用方 —— 而且很难察觉，因为两条都是成功响应。
    """
    async def handler(message: dict) -> None:  # 故意不回应，由用例手动乱序 push
        return None
    client, server = await _client(handler)
    first = asyncio.create_task(client.request("甲", {}))
    second = asyncio.create_task(client.request("乙", {}))
    await wait_for_requests(server, 2)
    ids = {message["method"]: message["id"] for message in server.requests}
    assert ids["甲"] != ids["乙"]

    # 反序回应
    server.push({"jsonrpc": "2.0", "id": ids["乙"], "result": "乙的结果"})
    server.push({"jsonrpc": "2.0", "id": ids["甲"], "result": "甲的结果"})

    assert await asyncio.wait_for(first, 1) == "甲的结果"
    assert await asyncio.wait_for(second, 1) == "乙的结果"
    assert client.pending == 0
    await client.aclose()
    await server.aclose()


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------


async def test_请求超时抛LspTimeoutError并清理挂起表():
    """服务器不回应 → 可读超时异常 + 挂起表清空（否则内存按请求数增长）。"""

    async def handler(message: dict) -> None:
        return None  # 永不回应

    client, server = await _client(handler)
    assert client.pending == 0
    with pytest.raises(LspTimeoutError) as excinfo:
        await client.request("textDocument/definition", {}, timeout=0.05)
    assert "textDocument/definition" in str(excinfo.value)
    assert client.pending == 0, "超时后挂起表里还留着这个 id"
    await client.aclose()
    await server.aclose()


async def test_关闭客户端后发请求立刻失败():
    """连接已关时不能再发请求（否则等一个永远不来的响应）。"""

    async def handler(message: dict) -> None:
        return {"result": None}

    client, server = await _client(handler)
    await client.aclose()
    with pytest.raises(ConnectionError):
        await client.request("x", {})
    await server.aclose()


# ---------------------------------------------------------------------------
# 错误归一
# ---------------------------------------------------------------------------


async def test_错误归一_错误对象给出code与message():
    """`error: {code, message}` → `LspRpcError`，code 保留、消息可读。"""

    async def handler(message: dict) -> dict:
        return {"error": {"code": -32601, "message": "未找到方法 Foo", "data": {"hint": 1}}}

    client, server = await _client(handler)
    with pytest.raises(LspRpcError) as excinfo:
        await client.request("Foo", {})
    error = excinfo.value
    assert error.code == -32601
    assert error.message == "未找到方法 Foo"
    assert error.data == {"hint": 1}
    assert "-32601" in str(error) and "未找到方法 Foo" in str(error)
    await client.aclose()
    await server.aclose()


async def test_错误归一_非对象错误也给出可读消息():
    """`error: "boom"`（不规范但真实存在）→ 归一成同一种异常，不抛 KeyError。"""

    async def handler(message: dict) -> dict:
        return {"error": "boom"}

    client, server = await _client(handler)
    with pytest.raises(LspRpcError) as excinfo:
        await client.request("Foo", {})
    assert "boom" in str(excinfo.value)
    assert excinfo.value.code == 0
    await client.aclose()
    await server.aclose()


async def test_坏消息体不会毒化整个连接():
    """一条坏 JSON 只该被丢弃/记错，后面的正常响应仍要能配对。

    修前会怎样：读循环里一旦 `json.loads` 抛异常就结束任务 ⇒ 这个会话之后
    **所有**请求都超时，而错误现场只剩一句"没有响应"。
    """

    async def handler(message: dict) -> dict:
        if message["method"] == "坏":
            return None
        return {"result": "正常结果"}

    client, server = await _client(handler)
    # 手动塞一条坏的（长度正确、内容不是 JSON），再确认后续请求照常工作
    server.reader.feed_data(b"Content-Length: 3\r\n\r\nxxx")
    assert await asyncio.wait_for(client.request("好", {}), 1) == "正常结果"
    await client.aclose()
    await server.aclose()


async def test_服务器发来的请求得到默认回应():
    """服务器反向请求（如 `workspace/configuration`）：必须有回应，否则对端会挂住。

    真语言服务器启动时会问客户端配置；不回应的表现是"initialize 之后永远没动静"。
    """
    handler = protocol_handler()
    client, server = await _client(handler)
    server.push(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "workspace/configuration",
            "params": {"items": [{"section": "python"}, {"section": "editor"}]},
        }
    )
    deadline = asyncio.get_running_loop().time() + 2.0
    while not server.responses:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("客户端没有回应服务器发来的 workspace/configuration")
        await asyncio.sleep(0.005)
    response = server.responses[0]
    assert response["id"] == 99
    assert "result" in response or "error" in response
    await client.aclose()
    await server.aclose()
