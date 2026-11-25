"""G23：**假语言服务器**与内存传输替身（本文件不是测试，是另外三个用例文件的共用夹具）。

为什么单独放一个文件而不是让三个用例文件各写一份：

- 假服务器脚本有 100 行左右，抄三份之后必然出现"改了一份、另两份还是旧协议"的
  漂移；那种失败现场（工具用例红、客户端用例绿）极难归因。
- 文件名的 `test_lsp_*_g23.py` 形态是 G23 的文件所有权约定（只允许新增
  `tests/test_lsp_*_g23.py`），因此辅助模块也必须落在这个前缀里。

**它只实现被断言用到的那几条方法**（initialize / didOpen / definition /
diagnostic / publishDiagnostics），不是通用 LSP 服务器：
用真实语言服务器（pyright / pylsp / gopls）意味着 CI 要装第三方依赖、要建索引、
结果还随版本漂移 —— 那是集成测试，不是这里要钉的东西。

协议分帧刻意在**被测代码之外**再实现一遍（脚本里的 `read_message` / `write`）：
如果两边共用同一个实现，分帧 bug 会被两边的同一个错误同时"验证通过"。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.tools.lsp.client import JsonRpcClient, encode_message, read_message
from app.tools.lsp.config import LspServerConfig
from app.tools.lsp.session import SpawnedTransport

# ---------------------------------------------------------------------------
# 假语言服务器（作为**独立进程**跑，命令由 CHIRON_LSP_<LANG> 指定）
# ---------------------------------------------------------------------------

#: 环境变量含义：
#:
#: * ``FAKE_LSP_MODE``：``push``（默认，诊断靠 publishDiagnostics 推送）或
#:   ``pull``（声明 diagnosticProvider，靠 textDocument/diagnostic 拉取）。
#: * ``FAKE_LSP_LOG``：JSONL 日志路径；每次收到消息写一行，供"复用同一进程"
#:   "谁先谁后"这类用例取证据。
FAKE_SERVER_SOURCE = r'''
"""假语言服务器（G23 测试专用）。只实现被断言用到的方法。"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_LSP_MODE", "push")
LOG_PATH = os.environ.get("FAKE_LSP_LOG", "")


def log(event):
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_message():
    """独立实现的 Content-Length 分帧（刻意不复用被测代码）。"""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        headers[name.strip().lower()] = value.strip()
    remaining = int(headers.get("content-length", "0"))
    chunks = []
    while remaining > 0:
        chunk = sys.stdin.buffer.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return json.loads(b"".join(chunks))


def write(message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream = sys.stdout.buffer
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stream.flush()


def location_for(params):
    """固定结果：定义落在**入参行的下一行**，便于断言 0 基/1 基换算。"""
    position = params["position"]
    return {
        "uri": params["textDocument"]["uri"],
        "range": {
            "start": {"line": position["line"] + 1, "character": 0},
            "end": {"line": position["line"] + 1, "character": 6},
        },
    }


def diagnostics_for(uri):
    return [
        {
            "range": {
                "start": {"line": 1, "character": 0},
                "end": {"line": 1, "character": 4},
            },
            "severity": 1,
            "source": "fake-lsp",
            "code": "F001",
            "message": "假诊断：这里有问题",
        }
    ]


def publish(uri):
    write(
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diagnostics_for(uri)},
        }
    )


def capabilities():
    caps = {"definitionProvider": True}
    if MODE == "pull":
        caps["diagnosticProvider"] = {"interFileDependencies": False}
    return caps


def main():
    log({"event": "start", "pid": os.getpid(), "mode": MODE})
    while True:
        message = read_message()
        if message is None:
            return 0
        method = message.get("method")
        log({"event": "in", "method": method, "id": message.get("id")})
        params = message.get("params") or {}
        if method == "initialize":
            write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "capabilities": capabilities(),
                        "serverInfo": {"name": "fake-lsp"},
                    },
                }
            )
        elif method == "initialized":
            pass
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            if MODE != "pull":
                publish(params["textDocument"]["uri"])
        elif method == "textDocument/definition":
            if MODE == "null":
                # 真实服务器在"找不到定义"时回 `result: null`（而不是空数组）
                write({"jsonrpc": "2.0", "id": message["id"], "result": None})
            else:
                write({"jsonrpc": "2.0", "id": message["id"], "result": location_for(params)})
        elif method == "textDocument/diagnostic":
            uri = params["textDocument"]["uri"]
            write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"kind": "full", "items": diagnostics_for(uri)},
                }
            )
        elif method == "fake/pid":
            write({"jsonrpc": "2.0", "id": message["id"], "result": {"pid": os.getpid()}})
        elif method == "fake/slow":
            time.sleep(float(params.get("seconds", 0.05)))
            write({"jsonrpc": "2.0", "id": message["id"], "result": {"slept": True}})
        elif method == "shutdown":
            write({"jsonrpc": "2.0", "id": message["id"], "result": None})
        elif method == "exit":
            return 0
        elif "id" in message:
            write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "fake-lsp: 未实现的方法 %s" % method},
                }
            )


if __name__ == "__main__":
    sys.exit(main())
'''


def write_fake_server(directory: Path) -> Path:
    """把假服务器脚本落到临时目录，返回脚本路径。"""
    path = Path(directory) / "fake_lsp_server_g23.py"
    path.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return path


def fake_command(script: Path) -> str:
    """`CHIRON_LSP_<LANG>` 的值：解释器 + 脚本，**带引号**以顺带覆盖命令解析。"""
    return f'"{sys.executable}" "{script}"'


def read_log(path: Path) -> list[dict[str, Any]]:
    """读假服务器写的 JSONL 日志（文件不存在 = 空）。"""
    if not Path(path).exists():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# 假进程：只在"信号升级"用例里用
# ---------------------------------------------------------------------------


class FakeProcess:
    """记录 `terminate()` / `kill()` 的假进程。

    为什么需要它：Windows 上 `Popen.terminate()` 是 `TerminateProcess` —— 硬件
    终止，被终止的进程**没有机会**记录"我收到了 SIGTERM"。用真进程断言
    "先 terminate 后 kill" 只能靠猜（日志里什么都看不到）。把进程换成替身，
    "调用了什么、按什么顺序"才成为可断言的事实。

    `ignore_terminate=True` 表示"这个进程对 terminate 不响应"，用来逼出升级到
    `kill()` 的那条路径。
    """

    def __init__(self, *, ignore_terminate: bool = True) -> None:
        self.events: list[str] = []
        self.returncode: int | None = None
        self.pid = 4242
        self.on_terminate: Callable[[], None] | None = None
        self._ignore_terminate = ignore_terminate
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.events.append("terminate")
        if self.on_terminate is not None:
            self.on_terminate()
        if not self._ignore_terminate:
            self._finish(-15)

    def kill(self) -> None:
        self.events.append("kill")
        self._finish(-9)

    def _finish(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        self._done.set()

    async def wait(self) -> int | None:
        # 可被 cancel 后再次 await（session 的超时升级会连 wait 两次）
        await self._done.wait()
        return self.returncode


# ---------------------------------------------------------------------------
# 内存传输：不需要真进程就能测协议行为
# ---------------------------------------------------------------------------


class _MemWriter:
    """`asyncio.StreamWriter` 的最小替身：只要求 write/drain。"""

    def __init__(self, sink: asyncio.StreamReader) -> None:
        self._sink = sink

    def write(self, data: bytes) -> None:
        self._sink.feed_data(bytes(data))

    async def drain(self) -> None:
        return None


Handler = Callable[[dict[str, Any]], Any]


class MemServer:
    """内存里的假服务器：与 `JsonRpcClient` 用**同一套** Content-Length 分帧。

    `handler` 返回 `None` 表示"故意不回应"（超时用例），返回 dict 表示
    `{"result": ...}` 或 `{"error": ...}` 的片段。
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._incoming = asyncio.StreamReader()
        self._outgoing = asyncio.StreamReader()
        self.writer = _MemWriter(self._incoming)
        self.reader = self._outgoing
        self.requests: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            message = await read_message(self._incoming)
            if message is None:
                return
            await self._dispatch(message)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            # 客户端对我们**主动发出**的请求的回应（反向请求）
            self.responses.append(message)
            return
        if "id" not in message:
            self.notifications.append(message)
            return
        self.requests.append(message)
        result = self._handler(message)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        payload.update(result)
        self.push(payload)

    def push(self, payload: dict[str, Any]) -> None:
        self._outgoing.feed_data(encode_message(payload))

    async def aclose(self) -> None:
        self._incoming.feed_eof()
        self._outgoing.feed_eof()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def protocol_handler(
    custom: Handler | None = None,
    *,
    capabilities: dict[str, Any] | None = None,
) -> Handler:
    """最小的协议骨架：initialize / initialized / didOpen 全部应答。

    其它方法交给 `custom`；`custom` 返回 None 表示"故意不回应"（用来制造超时）。
    """

    async def handle(message: dict[str, Any]) -> Any:
        method = message.get("method")
        if method == "initialize":
            return {"result": {"capabilities": dict(capabilities or {})}}
        if method in (
            "initialized",
            "textDocument/didOpen",
            "textDocument/didChange",
            "exit",
        ):
            return None
        if custom is not None:
            result = custom(message)
            if inspect.isawaitable(result):
                result = await result
            return result
        return {"error": {"code": -32601, "message": f"未实现的方法 {method}"}}

    return handle


def mem_spawn(
    handler: Handler,
    *,
    proc: FakeProcess | None = None,
) -> tuple[Callable[[LspServerConfig, Path], Awaitable[SpawnedTransport]], list[MemServer]]:
    """造一个"spawn 替身"：真协议、假进程/假管道。

    返回 `(spawn, servers)`；`spawn` 的签名与生产的 `_spawn_in_sandbox` 一致，
    因此 `LspSession` 不需要知道自己在测试里。用例结束时对 `servers` 里的每个
    服务器 `await server.aclose()`（否则泵任务会以"pending task"告警收场）。
    """
    servers: list[MemServer] = []

    async def spawn(config: LspServerConfig, root: Path) -> SpawnedTransport:
        server = MemServer(handler)
        server.start()
        servers.append(server)
        client = JsonRpcClient(
            server.reader,
            server.writer,
            request_timeout=config.request_timeout,
        )
        client.start()
        return SpawnedTransport(proc=proc or FakeProcess(), client=client)

    return spawn, servers


async def direct_spawn(config: LspServerConfig, root: Path) -> SpawnedTransport:
    """**真进程**、但跳过隔离包装的 spawn（给协议/工具用例用）。

    为什么可以跳过隔离包装：隔离包装（`sandboxed_env` + `child_launch_argv` +
    `attach_job`）的接线由 `test_lsp_session_g23.py::test_默认spawn_经既有隔离入口`
    单独钉住；这里要验的是协议行为，让每条用例都去跑一遍 `icacls` 打标签既慢，
    也会把"隔离层在本机不可用"变成"LSP 实现有 bug"。
    """
    import os

    proc = await asyncio.create_subprocess_exec(
        *config.command,
        cwd=str(root),
        env=dict(os.environ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = JsonRpcClient(proc.stdout, proc.stdin, request_timeout=config.request_timeout)
    client.start()
    return SpawnedTransport(proc=proc, client=client, stderr=proc.stderr)


async def wait_for_requests(server: MemServer, count: int, *, timeout: float = 2.0) -> None:
    """等到内存服务器收到 `count` 条请求（不等就会读到"还没发出去"的空表）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while len(server.requests) < count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"等了 {timeout}s 只收到 {len(server.requests)}/{count} 条请求"
            )
        await asyncio.sleep(0.005)
