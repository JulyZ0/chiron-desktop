"""MCP Client — connects to MCP servers over stdio, discovers and calls tools.

Mirrors Go internal/mcp/client.go with multi-server support.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

# MCP protocol version
MCP_PROTOCOL_VERSION = "2025-03-26"

logger = logging.getLogger(__name__)


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
    #: 该 server 是否**只提供只读操作**。引擎只直连 read_only 的 server（见 _connect_server 的
    #: 连接守卫）：连接凭据（env / url）会留在引擎进程里，而那正是 agent 能读到的地方。
    #: 未声明默认 False —— fail-closed：宁可工具不可用，也不给引擎高权限凭据。
    read_only: bool = False
    #: 拒绝装载的工具（**服务器本地名**，即 tools/list 返回的 name）。
    #: 用于剔除出站写操作 —— 典型是 XERJ 的 `field_report` / `corpus_feedback`：
    #: 它们 `confirm` 默认 true，省略参数就会把内容发往 xerj.org。只读插件不该持有。
    #: 在内置清单里声明（app/plugins/builtin.py），注册阶段**根本不注册**。
    deny_tools: frozenset[str] = frozenset()
    #: 该命令是否来自**部署者受信的内置清单**（app/plugins/builtin.py）。
    #: 为 True 时跳过用户级 `PLUGIN_COMMAND_ALLOWLIST`（内置命令由清单自身约束），
    #: 但**仍**要求 command 是裸 basename / 绝对路径（复用 `_command_path_ok`）。
    builtin: bool = False


def assert_server_connectable(server: ServerDef, *, role: str = "engine") -> None:
    """直连守卫：**引擎**只允许直连 **read_only** 的 MCP server（见 ``_connect_server``）。

    ``role="broker"``（独立 MCP broker 进程）是唯一被允许连危险 server 的角色 —— 它的凭据与
    执行面都与引擎隔离，且执行前仍要向服务端（Go 的 Tool Broker）要授权：判定权威在服务端，
    broker 只负责"用凭据执行"，两者不在同一个信任域。

    抽成模块级函数是为了能被直接单测 —— 这条边界值得有独立断言。
    """
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
    #: 服务器在 `tools/list` 里声明的 annotations（MCP 规范）：
    #: `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`。
    #: 保留原始字典而不是拆成字段 —— 规范还在演进，丢掉未知键就无法在调用阶段
    #: 做新增维度的判定。
    annotations: dict[str, Any] = field(default_factory=dict)


def is_tool_denied(server: ServerDef, local_name: str) -> bool:
    """该工具是否被 `deny_tools` 策略拒绝。**注册与调用两处共用这一个判定**。

    为什么要抽出来：如果注册阶段按一套规则过滤、调用阶段另写一套，两者迟早漂移，
    而漂移的表现是"看起来注册了却调不通"或更糟的"看起来拦了却调得通"。规则只能
    有**一处**事实来源。

    deny 匹配要容忍两种写法：服务器可能返回裸名（`field_report`），也可能自带
    命名空间前缀（XERJ 返回的是 `xerj_field_report`）。只按裸名匹配会漏掉后者。
    """
    prefix = f"{server.name}_"
    bare = local_name[len(prefix) :] if local_name.startswith(prefix) else local_name
    return local_name in server.deny_tools or bare in server.deny_tools


def is_tool_destructive(tool: MCPTool) -> bool:
    """工具是否声明为破坏性。

    **缺省视为破坏性**（fail-closed）：没声明 `readOnlyHint: true` 的工具一律按
    可能写东西对待。MCP 的 annotations 是自述，服务器可以不填；把"没说"当成
    "安全"就是把边界交给对方决定。
    """
    annotations = tool.annotations or {}
    if annotations.get("readOnlyHint") is True:
        return False
    if annotations.get("destructiveHint") is True:
        return True
    return True


class HTTPSSEConnection:
    """Connection to an MCP server over HTTP SSE transport."""

    def __init__(self, url: str, name: str):
        self.url = url.rstrip("/")
        self.name = name
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._session_id: str | None = None

        # P0 安全修复：SSRF 防护 — 检查 URL 主机构是否为私有地址
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname
        if host and _is_private_ip(host):
            raise ValueError(
                f"SSRF protection: connecting to private/restricted IP is forbidden "
                f"(host={host}, server={name})"
            )

    async def send_jsonrpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request via HTTP POST and read the response."""
        self._req_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        headers = {"Content-Type": "application/json"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        async with self._lock:
            resp = await self._client.post(
                f"{self.url}/messages",
                json=req,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

            # Extract session ID from SSE response headers
            session_id = resp.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id

        if "error" in data and data["error"]:
            raise RuntimeError(f"MCP error: {data['error'].get('message', 'unknown')}")
        result: dict[str, Any] = data.get("result", {})
        return result

    async def close(self) -> None:
        await self._client.aclose()


class ServerConnection:
    """Connection to a single MCP server process."""

    def __init__(self, proc: asyncio.subprocess.Process, name: str):
        self.proc = proc
        self.name = name
        self._req_id = 0
        self._lock = asyncio.Lock()
        # create_subprocess_exec 传了 PIPE，三个流运行时恒非 None；这里 cast 只为把
        # `StreamReader | None` 收窄成具体类型（存成属性，避免每个使用点各自断言）。
        self._stdin = cast("asyncio.StreamWriter", proc.stdin)
        self._stdout = cast("asyncio.StreamReader", proc.stdout)
        self._stderr = cast("asyncio.StreamReader", proc.stderr)
        # 异步读取 stderr 并记录到日志，避免 DEVNULL 丢弃错误信息
        self._stderr_task = asyncio.ensure_future(self._read_stderr())

    async def _read_stderr(self) -> None:
        """异步读取 MCP 服务器 stderr 并记录到日志。"""
        try:
            async for line in self._stderr:
                if line.strip():
                    logger.warning(
                        "MCP stderr [%s]: %s", self.name, line.decode().rstrip()
                    )
        except Exception:
            pass

    async def send_jsonrpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and read the response."""
        self._req_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }
        req_line = json.dumps(req) + "\n"

        async with self._lock:
            self._stdin.write(req_line.encode())
            await self._stdin.drain()

            response_line = await asyncio.wait_for(
                self._stdout.readline(), timeout=30.0
            )
            if not response_line:
                raise ConnectionError(f"No response from MCP server {self.name}")

        resp = json.loads(response_line)
        if "error" in resp and resp["error"]:
            raise RuntimeError(f"MCP error: {resp['error'].get('message', 'unknown')}")
        result: dict[str, Any] = resp.get("result", {})
        return result

    async def close(self) -> None:
        """Kill the server process."""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                self.proc.kill()


class MCPClient:
    """Manages connections to multiple MCP servers and their tools."""

    def __init__(self, servers: list[ServerDef], *, role: str = "engine"):
        #: "engine"（默认）= 只能连 read_only 的 server；"broker" = 独立 MCP broker 进程，
        #: 唯一被允许持凭据连危险 server 的角色（见 assert_server_connectable）。
        self._role = role
        self._servers = servers
        self._conns: dict[str, ServerConnection | HTTPSSEConnection] = {}
        self._tools: list[MCPTool] = []

    async def start(self) -> None:
        """Connect to all configured MCP servers and discover their tools."""
        for server in self._servers:
            try:
                # 收尾3：把该 server 的凭据登记进**进程级集合**。于是所有走 `redact_text`
                # 的出站路径（输出栅栏 / 审计留痕 / 工具结果回灌）自动覆盖它们 ——
                # 不必要求每个调用点各自记得处理（那正是"迟早漏一处"的来源）。
                from app.mcp.credential_projection import collect_secrets, register_secrets

                register_secrets(collect_secrets(server))
                await self._connect_server(server)
            except Exception as e:
                # D4：连接失败消息里可能带 url / env 的凭据（HTTP SSE 常把 token 放 query），
                # 所以这里先**脱敏**再记日志 —— 日志会被采集、被转发、被贴进工单。
                from app.mcp.credential_projection import collect_secrets, redact, safe_label

                logger.error(
                    "MCP connect %s failed: %s",
                    safe_label(server),
                    redact(str(e), collect_secrets(server)),
                )
                raise

    async def _connect_server(self, server: ServerDef) -> None:
        """Connect to a single MCP server and discover its tools.

        **直连守卫（安全边界）**：引擎只允许直连**声明为只读**的 MCP server。

        理由：连接凭据来自 ``ServerDef.env`` / ``url``（插件配置），而引擎进程正在处理
        不可信内容 —— 被 prompt injection 影响的 agent 可以直接拿这些凭据触达外部系统，
        绕过网关的全部授权、审计与限流（docs/agent-safety-and-reliability.md §1.4）。

        未声明 ``read_only`` 的 server 一律**不连**（fail-closed）。要放行请在插件配置里显式
        写 ``"read_only": true``（表示它只提供读操作）；含写 / 删 / 外部操作的 server 应由服务端
        分发，不能把凭据留在 agent 可触达的地方。
        """
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

        # Initialize
        await conn.send_jsonrpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "chiron-python", "version": "3.0.0"},
                "capabilities": {},
            },
        )

        # List tools
        tools_result = await conn.send_jsonrpc("tools/list", None)
        raw_tools = tools_result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (HTTP SSE): %d tools", server.name, len(raw_tools))

    async def _connect_stdio(self, server: ServerDef) -> None:
        """Connect to an MCP server via stdio (subprocess)."""
        # 安全修复（P0-S7）：仅允许 PLUGIN_COMMAND_ALLOWLIST 白名单内的命令被拉起
        from app.tools.ssrf import _command_path_ok, command_allowed

        if server.builtin:
            # 内置插件（app/plugins/builtin.py）由**部署者**清单约束，不走用户级白名单 ——
            # 否则用户不配 PLUGIN_COMMAND_ALLOWLIST 就用不了随应用分发的能力。
            # 但"从哪跑"的约束一视同仁：仍拒绝相对穿越 / UNC（见 _command_path_ok）。
            if not _command_path_ok(server.command):
                raise PermissionError(
                    f"内置插件命令 {server.command!r} 不是裸 basename 或绝对路径"
                )
        elif not command_allowed(server.command):
            raise PermissionError(
                f"MCP server command {server.command!r} not allowed: "
                "set PLUGIN_COMMAND_ALLOWLIST (comma-separated basenames) to enable"
            )
        # 无论 server 是否声明 env，子进程环境都必须以 sandboxed_env() 为基底。
        #
        # 此前这里是 `env = None` + `if server.env:` —— 于是**未声明 env 的 stdio server
        # 会完整继承引擎进程环境**（JWT_SECRET / INTERNAL_TOKEN / APP_SECRET / 数据库
        # 口令 / 各 provider 的 API key 全在其中）。而 agent 能通过 MCP server 读自己的
        # 环境（`env` 命令、`/proc/self/environ`、任何库读 os.environ），等于把网关的
        # 全部凭据交给一段由第三方代码驱动的子进程 —— 这也是 sandboxed_env() 存在的理由。
        #
        # 复用 credential_projection.project_stdio_env：它已经过单测（不改入参、只叠加
        # server 自己声明的键），避免此处再写一份"清理 + 叠加"而两处漂移。
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
        # 成功拉起：这是"某租户在某会话里让宿主机起了一个插件进程"的事实记录
        record_plugin(plugin=server.name, action="spawn", success=True)
        conn = ServerConnection(proc, server.name)
        self._conns[server.name] = conn

        # Initialize
        await conn.send_jsonrpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "chiron-python", "version": "3.0.0"},
            },
        )

        # List tools
        result = await conn.send_jsonrpc("tools/list", None)
        raw_tools = result.get("tools", [])
        self._register_tools(server, raw_tools)
        logger.info("MCP server %s connected (stdio): %d tools", server.name, len(raw_tools))

    def _register_tools(self, server: ServerDef, raw_tools: list[dict[str, Any]]) -> None:
        """Register tools from a server response.

        - ``deny_tools`` 命中的工具**根本不注册**（不是"隐藏"）：用于剔除出站写操作。
          判定与调用阶段共用 ``is_tool_denied``，避免两处规则漂移。
        - 命名空间前缀去重：工具的本地名已带 ``{server.name}_`` 前缀时不再叠加，
          否则 XERJ 会得到 ``xerj_xerj_code_search`` 这种双前缀。
        - ``annotations`` 原样保留：调用阶段要靠 ``readOnlyHint`` 判断破坏性。
        """
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
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the appropriate MCP server.

        本地名以**注册时登记的映射**为准，而不是从 ``{server}_{...}`` 反推前缀：
        去重后 ``xerj_code_search`` 的本地名仍是 ``xerj_code_search``（不是 ``code_search``），
        按前缀截断会把名字截错，服务器侧必然报 unknown tool。

        **调用阶段的第二道过滤**（注册阶段已经剔除过 deny_tools，这里是复查）：

        1. ``is_tool_denied`` 再算一次 —— 工具条目可能来自更早的一次注册，
           期间 server 的 deny 策略被配置热重载改严；只在注册时判一次，
           改严之后旧条目仍然可调。
        2. ``read_only`` 的 server 上出现**显式声明破坏性**的工具 ⇒ 拒绝。
           这是自相矛盾：部署者声明"这个 server 只读"，服务器却说"这工具会写"。
           两者不能都信，也不能挑软的 —— fail-closed，拒绝并留痕。
        3. 未声明 ``readOnlyHint`` 的工具**照常执行**，但返回体里带上
           ``annotations`` 与 ``destructive`` 标记，让上游（确认卡片 / 审计）
           自己决定要不要拦。不在这里一刀切拒绝：多数 MCP 服务器根本不填
           annotations，全拒会让插件体系整体不可用。
        """
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
            )
        return {"error": f"Tool {tool_name} not found on any MCP server"}

    def annotations_for(self, tool_name: str) -> dict[str, Any]:
        """该工具的自述注解，附上引擎推断出的 ``destructive``。

        用途是让上层（确认卡片 / 审计 / use_capability）知道"这个工具会不会写东西"。
        **不塞进 ``call_tool`` 的返回体** —— 那是 MCP 协议响应，往里加字段等于伪造
        协议内容，而且会让所有既有调用方拿到的形状凭空多一个键。

        没找到该工具时返回空字典：调用方据此按"未知"处理，而不是"安全"。
        """
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
                # 这里只有名字，拿不到 server 定义 ⇒ 无法枚举凭据；但仍按"可能含凭据"处理，
                # 只记异常类型与名字（不对未知文本做替换，避免把日志弄得不可读）。
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
                # 之前漏了这一行：`ServerDef.read_only` 默认 False（fail-closed），而
                # `assert_server_connectable()` 只放行 read_only 的 server ⇒ **任何**
                # 插件都会被引擎拒连，配置里写 `"read_only": true` 也不生效。
                # 表现是 `MCP connect xerj failed: not declared read-only`。
                read_only=bool(s.get("read_only", False)),
            )
        )
    return servers
