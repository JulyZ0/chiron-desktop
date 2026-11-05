"""内置插件（app/plugins/builtin.py）—— 清单、命令约束、出站工具剔除、命名去重。

内置插件与用户插件的差异必须在**行为**上钉住，而不是只写在注释里：

1. 随应用分发 ⇒ 不依赖用户配置 `plugins.json`，归属为全局（`owner=""`）；
2. 二进制缺失 ⇒ 跳过而非让引擎启动失败（检索增强不是运行前提）；
3. 命令白名单 ⇒ 跳过**用户级** `PLUGIN_COMMAND_ALLOWLIST`（内置命令由部署者清单约束），
   但"从哪跑"的约束（相对穿越 / UNC）一视同仁；
4. 只读定位 ⇒ 出站写操作工具**根本不注册**，不是靠 `use_capability` 隐藏。
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp.client import MCPClient, ServerDef
from app.plugins import builtin as builtin_mod
from app.plugins.builtin import (
    available_builtin_plugins,
    builtin_commands,
    builtin_names,
    builtin_plugins,
)


# ── 清单 ─────────────────────────────────────────────────────────────────


def test_xerj_is_a_builtin_plugin():
    names = builtin_names()
    assert "xerj" in names


def test_builtin_plugins_are_read_only():
    """内置插件都是检索增强；只读声明是引擎直连的前提（fail-closed）。"""
    for plugin in builtin_plugins():
        assert plugin.read_only is True, f"{plugin.name} 必须声明只读"


def test_xerj_denies_outbound_tools():
    """field_report / corpus_feedback 默认发送（confirm=true），只读插件不该持有。"""
    xerj = next(p for p in builtin_plugins() if p.name == "xerj")
    assert "field_report" in xerj.deny_tools
    assert "corpus_feedback" in xerj.deny_tools


def test_xerj_url_avoids_gateway_ports():
    """默认地址必须避开 8080/8081（Go 网关 / gRPC 默认值，否则静默抢流量）。"""
    xerj = next(p for p in builtin_plugins() if p.name == "xerj")
    url = xerj.env["XERJ_URL"]
    assert not url.endswith(":8080") and not url.endswith(":8081"), url


def test_builtin_commands_cover_declared_commands():
    assert builtin_commands() == frozenset(p.command for p in builtin_plugins())


def test_missing_binary_is_skipped_not_fatal(monkeypatch):
    """二进制不存在时过滤掉，而不是抛错 —— 与 run.py 的 optional=True 语义一致。"""
    monkeypatch.setattr(builtin_mod.shutil, "which", lambda _cmd: None)
    assert available_builtin_plugins() == []


def test_available_returns_plugins_when_binary_present(monkeypatch):
    monkeypatch.setattr(builtin_mod.shutil, "which", lambda cmd: f"/fake/bin/{cmd}")
    names = {p.name for p in available_builtin_plugins()}
    assert "xerj" in names


# ── 命令约束 ─────────────────────────────────────────────────────────────


def test_builtin_command_skips_user_allowlist(monkeypatch):
    """内置命令不要求用户配 PLUGIN_COMMAND_ALLOWLIST，否则开箱不可用。

    用**不存在的**命令名：本机 PATH 上若真有 xerj，会真的拉起一个 MCP 子进程
    （还会把它的 ANSI 日志喂给 JSON 解析），那不是这个用例要验的。这里只关心
    "有没有在 allowlist 这一步被挡住"。
    """
    monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)

    async def _run():
        client = MCPClient(
            [ServerDef(name="builtin-probe", command="builtin-probe-not-installed", builtin=True, read_only=True)]
        )
        try:
            await client.start()
        except PermissionError as e:
            pytest.fail(f"内置插件不该被用户级白名单拦住：{e}")
        except (OSError, FileNotFoundError):
            pass  # 命令不存在 ⇒ 走到 spawn 才失败，说明白名单已放行
        finally:
            await client.close()

    asyncio.run(_run())


def test_user_command_still_requires_allowlist(monkeypatch):
    """非内置命令必须仍然受用户级白名单约束（不能因为加了 builtin 就放开）。"""
    monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)

    async def _run():
        client = MCPClient([ServerDef(name="evil", command="evil", read_only=True)])
        with pytest.raises(PermissionError):
            await client.start()
        await client.close()

    asyncio.run(_run())


def test_builtin_command_still_rejects_traversal(monkeypatch):
    """内置标记只豁免白名单，不豁免"从哪跑"：相对穿越 / UNC 一律拒绝。"""
    monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)

    async def _run(path):
        client = MCPClient(
            [ServerDef(name="xerj", command=path, builtin=True, read_only=True)]
        )
        with pytest.raises(PermissionError):
            await client.start()
        await client.close()

    asyncio.run(_run("../../tmp/xerj"))
    asyncio.run(_run("\\\\evil-host\\share\\xerj"))


# ── 工具注册：剔除 + 命名去重 ────────────────────────────────────────────


def _fake_tools():
    return [
        {"name": "xerj_code_search", "description": "search", "inputSchema": {"type": "object"}},
        {"name": "xerj_search", "description": "search2", "inputSchema": {"type": "object"}},
        {"name": "field_report", "description": "SENDS to xerj.org", "inputSchema": {"type": "object"}},
        {"name": "corpus_feedback", "description": "SENDS", "inputSchema": {"type": "object"}},
    ]


def test_denied_tools_are_not_registered_at_all():
    """关键语义：**不注册**（而不是注册后被隐藏）—— 隐藏仍可被调用。"""
    client = MCPClient([])
    server = ServerDef(
        name="xerj",
        command="xerj",
        read_only=True,
        deny_tools=frozenset({"field_report", "corpus_feedback"}),
    )
    client._register_tools(server, _fake_tools())  # noqa: SLF001

    names = {t.local_name for t in client.tools}
    assert names == {"xerj_code_search", "xerj_search"}
    assert "field_report" not in names
    assert "corpus_feedback" not in names


def test_namespace_prefix_is_not_doubled():
    """xerj 的工具本来就带前缀；再叠一次会得到 xerj_xerj_code_search。"""
    client = MCPClient([])
    server = ServerDef(name="xerj", command="xerj", read_only=True)
    client._register_tools(server, _fake_tools())  # noqa: SLF001

    names = [t.name for t in client.tools]
    assert "xerj_code_search" in names
    assert not any("xerj_xerj_" in n for n in names), names


def test_prefix_still_applied_when_absent():
    """没有自带前缀的 server 仍然要加命名空间，避免不同 server 撞名。"""
    client = MCPClient([])
    server = ServerDef(name="docs", command="docs-mcp", read_only=True)
    client._register_tools(
        server, [{"name": "lookup", "description": "", "inputSchema": {"type": "object"}}]
    )  # noqa: SLF001
    assert [t.name for t in client.tools] == ["docs_lookup"]


def test_call_tool_uses_registered_local_name(monkeypatch):
    """去重后 ``xerj_code_search`` 的本地名仍是它自己，不能按前缀截断成 ``code_search``。"""
    recorded: dict[str, object] = {}

    class _Conn:
        async def send_jsonrpc(self, method, params=None):
            recorded["method"] = method
            recorded["params"] = params
            return {"ok": True}

    client = MCPClient([])
    server = ServerDef(name="xerj", command="xerj", read_only=True)
    client._register_tools(server, _fake_tools())  # noqa: SLF001
    client._conns["xerj"] = _Conn()  # noqa: SLF001
    client._servers = [server]  # noqa: SLF001

    result = asyncio.run(client.call_tool("xerj_code_search", {"query": "x"}))
    assert result == {"ok": True}
    assert recorded["params"]["name"] == "xerj_code_search"


# ── 池接线：内置插件注册为全局工具 ───────────────────────────────────────


def test_pool_registers_builtin_tools_as_global(monkeypatch):
    """内置插件的工具必须 `owner=""`（全局）—— 否则每个用户都得自己配一遍。"""
    from app.plugins.pool import MCPClientPool
    from app.tools.registry import registry

    class _FakeClient:
        def __init__(self, servers):
            self._servers = servers
            self.tools = []
            self.closed = False

        async def start(self):
            self.tools = [
                type(
                    "T",
                    (),
                    {
                        "name": f"{self._servers[0].name}_code_search",
                        "description": "d",
                        "input_schema": {"type": "object"},
                    },
                )()
            ]

        async def close(self):
            self.closed = True

    fake = builtin_mod.BuiltinPlugin(
        name="xerj",
        command="xerj",
        args=("mcp",),
        env={"XERJ_URL": "http://127.0.0.1:9200"},
        read_only=True,
    )
    monkeypatch.setattr(builtin_mod, "available_builtin_plugins", lambda: [fake])
    monkeypatch.setattr(builtin_mod.BuiltinPlugin, "resolve_command", lambda self: "/fake/xerj")
    monkeypatch.setattr("app.plugins.pool.MCPClient", _FakeClient)

    pool = MCPClientPool()
    try:
        asyncio.run(pool._start_builtin_plugins())  # noqa: SLF001
        tool = registry.get("xerj_code_search")
        assert tool is not None
        assert tool.owners == set(), "内置插件工具应全局可见（owner 为空）"
    finally:
        registry.unregister("xerj_code_search")
