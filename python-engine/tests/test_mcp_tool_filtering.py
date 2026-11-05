"""MCP 工具双层过滤：注册阶段裁剪 + 调用阶段校验。

## 为什么需要两层

只做注册阶段过滤的问题在于**时间**：注册发生在连接建立的那一刻，而
`deny_tools` 策略可能在之后被配置热重载改严。工具条目留在 `self._tools` 里，
旧条目仍然可以被 `call_tool` 调到 —— 只判一次等于只保护了"首次注册之后、
策略变更之前"这一段时间。

只做调用阶段过滤的问题在于**暴露面**：未注册的工具不会出现在 `tools` 列表里，
也就不可能被上层（`use_capability` 发现、前端能力清单）看见。让一个明确被拒绝
的工具一直存在于内存列表中，是在赌"所有读取路径都会再判一次"。

两层都做，且**共用同一个判定函数**（`is_tool_denied`）：规则只能有一处事实来源，
否则迟早漂移成"看着拦了其实调得通"。

## annotations 的语义边界

MCP 的 `annotations` 是服务器**自述**，可以完全不填。因此：

- 显式 `readOnlyHint: true` ⇒ 只读，可信；
- 显式 `destructiveHint: true` ⇒ 破坏性，可信；
- **两者都没有** ⇒ 按破坏性对待（fail-closed），但不拒绝调用 —— 多数服务器根本
  不填 annotations，全拒会让插件体系整体不可用。改为把 `destructive` 标记
  向上传递，由确认卡片 / 审计自行决定处置。
"""

from __future__ import annotations

import asyncio

from app.mcp.client import (
    MCPClient,
    ServerDef,
    is_tool_denied,
    is_tool_destructive,
)


def _server(**kwargs) -> ServerDef:
    base = {"name": "xerj", "command": "xerj", "read_only": True}
    base.update(kwargs)
    return ServerDef(**base)


class _RecordingConn:
    """记录调用，返回固定结果。"""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self._result = {"ok": True} if result is None else result

    async def send_jsonrpc(self, method, params=None):
        self.calls.append({"method": method, "params": params})
        return dict(self._result)


def _client_with(server: ServerDef, raw_tools: list[dict]) -> tuple[MCPClient, _RecordingConn]:
    client = MCPClient([])
    client._register_tools(server, raw_tools)  # noqa: SLF001
    conn = _RecordingConn()
    client._conns[server.name] = conn  # noqa: SLF001
    client._servers = [server]  # noqa: SLF001
    return client, conn


# ── 判定函数：注册与调用共用 ─────────────────────────────────────────────


def test_denied_matches_bare_name():
    server = _server(deny_tools=frozenset({"field_report"}))
    assert is_tool_denied(server, "field_report") is True


def test_denied_matches_prefixed_name():
    """XERJ 返回的是 `xerj_field_report`；只匹配裸名会漏掉它。"""
    server = _server(deny_tools=frozenset({"field_report"}))
    assert is_tool_denied(server, "xerj_field_report") is True


def test_not_denied_when_policy_empty():
    server = _server()
    assert is_tool_denied(server, "xerj_code_search") is False


def test_undeclared_annotations_count_as_destructive():
    """没声明 = 按破坏性对待。把"没说"当成"安全"就是把边界交给对方决定。"""
    from app.mcp.client import MCPTool

    tool = MCPTool(
        name="xerj_x",
        description="",
        input_schema={},
        server_name="xerj",
        local_name="x",
    )
    assert is_tool_destructive(tool) is True


def test_read_only_hint_makes_tool_safe():
    from app.mcp.client import MCPTool

    tool = MCPTool(
        name="xerj_x",
        description="",
        input_schema={},
        server_name="xerj",
        local_name="x",
        annotations={"readOnlyHint": True},
    )
    assert is_tool_destructive(tool) is False


def test_destructive_hint_wins():
    from app.mcp.client import MCPTool

    tool = MCPTool(
        name="xerj_x",
        description="",
        input_schema={},
        server_name="xerj",
        local_name="x",
        annotations={"destructiveHint": True},
    )
    assert is_tool_destructive(tool) is True


# ── 注册阶段：annotations 必须被保留 ─────────────────────────────────────


def test_registration_preserves_annotations():
    """不保留就无法在调用阶段判断破坏性 —— 等于把第二层过滤废掉。"""
    server = _server()
    client, _ = _client_with(
        server,
        [
            {
                "name": "code_search",
                "description": "read",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }
        ],
    )

    tool = client.tools[0]
    assert tool.annotations["readOnlyHint"] is True
    assert tool.annotations["openWorldHint"] is False


def test_registration_tolerates_non_dict_annotations():
    """服务器可能返回 null / 字符串；不能让注册整体崩掉。"""
    server = _server()
    client, _ = _client_with(
        server,
        [{"name": "code_search", "description": "", "inputSchema": {}, "annotations": None}],
    )

    assert client.tools[0].annotations == {}


# ── 调用阶段：deny 复查 ──────────────────────────────────────────────────


def test_call_refused_when_policy_tightened_after_registration():
    """注册之后策略改严，旧条目**仍然可调** —— 这是第一层过滤的时间盲区。

    真实场景：用户在设置里把某工具加入拒绝清单，配置热重载更新了 server 定义，
    但已注册的工具条目还在。没有这道复查，拒绝清单形同虚设直到重启。
    """
    server = _server()
    client, conn = _client_with(
        server,
        [{"name": "code_search", "description": "", "inputSchema": {}}],
    )
    # 注册完成后策略改严
    client._servers = [_server(deny_tools=frozenset({"code_search"}))]  # noqa: SLF001

    result = asyncio.run(client.call_tool("xerj_code_search", {}))

    assert result.get("denied") is True
    assert "denied" in result["error"]
    assert conn.calls == [], "被拒绝的工具不得真的发往服务器"


# ── 调用阶段：只读 server 与破坏性自述的矛盾 ─────────────────────────────


def test_call_refused_when_read_only_server_declares_destructive():
    """部署者说"只读"，服务器说"会写" —— 两者不能都信，fail-closed 拒绝。"""
    server = _server(read_only=True)
    client, conn = _client_with(
        server,
        [
            {
                "name": "code_search",
                "description": "",
                "inputSchema": {},
                "annotations": {"destructiveHint": True},
            }
        ],
    )

    result = asyncio.run(client.call_tool("xerj_code_search", {}))

    assert result.get("denied") is True
    assert "read-only" in result["error"]
    assert conn.calls == []


def test_call_proceeds_when_read_only_server_omits_annotations():
    """只是"没说"，不是"说会写"：放行，但标记出来交上游处置。

    全拒会让插件体系整体不可用 —— 多数 MCP 服务器根本不填 annotations。
    """
    server = _server(read_only=True)
    client, conn = _client_with(
        server,
        [{"name": "code_search", "description": "", "inputSchema": {}}],
    )

    result = asyncio.run(client.call_tool("xerj_code_search", {}))

    assert result == {"ok": True}
    assert len(conn.calls) == 1


def test_call_proceeds_when_read_only_hint_present():
    server = _server(read_only=True)
    client, conn = _client_with(
        server,
        [
            {
                "name": "code_search",
                "description": "",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            }
        ],
    )

    result = asyncio.run(client.call_tool("xerj_code_search", {}))

    assert result == {"ok": True}
    assert len(conn.calls) == 1


# ── annotations_for：给上层的元数据通道 ──────────────────────────────────


def test_annotations_for_reports_destructive_flag():
    server = _server()
    client, _ = _client_with(
        server,
        [
            {
                "name": "code_search",
                "description": "",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            },
            {"name": "other", "description": "", "inputSchema": {}},
        ],
    )

    safe = client.annotations_for("xerj_code_search")
    unsafe = client.annotations_for("xerj_other")

    assert safe["destructive"] is False
    assert safe["readOnlyHint"] is True
    assert unsafe["destructive"] is True
    assert unsafe["readOnlyHint"] is False


def test_annotations_for_unknown_tool_is_empty():
    """查不到时返回空字典 —— 调用方按"未知"处理，不能按"安全"处理。"""
    server = _server()
    client, _ = _client_with(server, [])

    assert client.annotations_for("nope") == {}


def test_call_result_shape_is_not_polluted():
    """协议响应里不能塞引擎字段 —— 那等于伪造 MCP 协议内容。"""
    server = _server()
    client, _ = _client_with(
        server,
        [
            {
                "name": "code_search",
                "description": "",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            }
        ],
    )

    result = asyncio.run(client.call_tool("xerj_code_search", {}))

    assert result == {"ok": True}
    assert "annotations" not in result
