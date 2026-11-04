"""技能管理器 (app/skill/manager.py) 测试。

意图:
- MCP STDIO 传输: 使用 echo fixture 测试 discover_tools + call_tool
- MCP HTTP 传输: 不支持的传输方式必须 fail-loud
- PROMPT 技能必须完成真实的模板渲染
- PYTHON_SCRIPT 技能必须在沙箱中执行并拦截危险 import
- HTTP_REQUEST 技能必须拦截内网地址 (SSRF)
- 租户隔离: 其他租户不得访问不属于自己的技能
"""
from __future__ import annotations

import os
import sys

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.skill.manager import MCPClient, SkillManager, SkillType

TENANT = "tenant-skill-test"

# MCP STDIO echo fixture 的路径
_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "mcp_stdio_echo.py"
)
_STDIO_CMD = f"{sys.executable} {_FIXTURE}"

# S 安全修复：MCP STDIO 命令须过 PLUGIN_COMMAND_ALLOWLIST 白名单(fail-close)。
# 测试用当前 python 解释器作为 MCP 命令，须在测试环境列入白名单。
os.environ.setdefault(
    "PLUGIN_COMMAND_ALLOWLIST",
    "python,python.exe,python3,node,node.exe,echo",
)


# ── STDIO 传输测试 ────────────────────────────────────────────────


async def test_stdio_discover_tools():
    """STDIO 传输: discover_tools 返回工具列表。"""
    client = MCPClient(server_url=_STDIO_CMD, transport="stdio")
    tools = await client.discover_tools()
    assert len(tools) == 2
    names = {t["name"] for t in tools}
    assert names == {"echo", "add"}


async def test_stdio_discover_tools_caches():
    """STDIO 传输: 第二次 discover_tools 走缓存不 spawn 子进程。"""
    client = MCPClient(server_url=_STDIO_CMD, transport="stdio")
    tools1 = await client.discover_tools()
    tools2 = await client.discover_tools()
    assert tools1 == tools2


async def test_stdio_call_tool_echo():
    """STDIO 传输: call_tool 执行 echo 工具。"""
    client = MCPClient(server_url=_STDIO_CMD, transport="stdio")
    result = await client.call_tool(
        tool_name="echo",
        arguments={"message": "hello world"},
    )
    assert result.success is True
    assert "hello world" in result.output


async def test_stdio_call_tool_add():
    """STDIO 传输: call_tool 执行 add 工具。"""
    client = MCPClient(server_url=_STDIO_CMD, transport="stdio")
    result = await client.call_tool(
        tool_name="add",
        arguments={"a": 3, "b": 7},
    )
    assert result.success is True
    assert "10" in result.output


async def test_stdio_unknown_tool_fails_loud():
    """STDIO 传输: 调用不存在的工具必须 fail-loud。"""
    client = MCPClient(server_url=_STDIO_CMD, transport="stdio")
    result = await client.call_tool(
        tool_name="nonexistent",
        arguments={},
    )
    assert result.success is False
    assert "error" in result.error.lower() or "unknown" in result.error.lower()


async def test_stdio_empty_command_fails_loud():
    """STDIO 传输: 空 server_url 必须抛 ValueError。"""
    client = MCPClient(server_url="", transport="stdio")
    with pytest.raises(ValueError, match="command"):
        await client.discover_tools()


async def test_stdio_bad_command_fails_loud():
    """STDIO 传输: 不存在的命令必须 fail-loud（RuntimeError）。"""
    client = MCPClient(
        server_url="nonexistent-binary-xyz --flag", transport="stdio"
    )
    with pytest.raises((RuntimeError, ValueError), match="not allowed|not found|failed to spawn"):
        await client.discover_tools()


# ── 不支持的传输方式 ─────────────────────────────────────────────


async def test_unsupported_transport_fails_loud():
    client = MCPClient(server_url="http://localhost:1", transport="carrier-pigeon")
    with pytest.raises(ValueError, match="Unsupported transport"):
        await client.discover_tools()


async def test_unsupported_transport_call_fails_loud():
    client = MCPClient(server_url="http://localhost:1", transport="carrier-pigeon")
    with pytest.raises(ValueError, match="Unsupported transport"):
        await client.call_tool(tool_name="any", arguments={})


async def test_prompt_skill_renders_template():
    """PROMPT 技能必须真实渲染模板变量 (注册时的 config 必须持久化)。"""
    manager = SkillManager()
    await manager.register_skill(
        tenant_id=TENANT,
        skill_id="greet",
        name="greet",
        description="问候模板",
        type=SkillType.PROMPT,
        config={"template": "你好, {name}!"},
    )
    result = await manager.execute_skill(TENANT, "greet", {"name": "世界"})
    assert result.success is True
    assert result.output == "你好, 世界!"


async def test_python_shell_http_skills_missing_config_fail_loud():
    """python/shell/http 技能已实现: 空 config 必须返回明确失败,不得静默成功。"""
    manager = SkillManager()
    for skill_type in (SkillType.PYTHON_SCRIPT, SkillType.SHELL_COMMAND, SkillType.HTTP_REQUEST):
        await manager.register_skill(
            tenant_id=TENANT,
            skill_id=f"s-{skill_type.value}",
            name=f"s-{skill_type.value}",
            description="t",
            type=skill_type,
            config={},
        )
        result = await manager.execute_skill(TENANT, f"s-{skill_type.value}", {})
        assert result.success is False
        assert "missing" in result.error.lower()


async def test_python_script_skill_executes_in_sandbox():
    """PYTHON_SCRIPT 技能必须在沙箱中执行并返回真实结果。"""
    manager = SkillManager()
    await manager.register_skill(
        tenant_id=TENANT,
        skill_id="calc",
        name="calc",
        description="计算",
        type=SkillType.PYTHON_SCRIPT,
        config={"code": "return {'sum': params['a'] + params['b']}"},
    )
    result = await manager.execute_skill(TENANT, "calc", {"a": 1, "b": 2})
    assert result.success is True
    assert "3" in result.output


async def test_python_script_skill_blocks_dangerous_code():
    """PYTHON_SCRIPT 技能必须拦截危险 import (沙箱安全)。"""
    manager = SkillManager()
    await manager.register_skill(
        tenant_id=TENANT,
        skill_id="evil",
        name="evil",
        description="危险脚本",
        type=SkillType.PYTHON_SCRIPT,
        config={"code": "import os\nreturn os.listdir('.')"},
    )
    result = await manager.execute_skill(TENANT, "evil", {})
    assert result.success is False
    assert "blocked" in result.error.lower() or "not allowed" in result.error.lower()


async def test_http_request_skill_blocks_internal_addresses():
    """HTTP_REQUEST 技能必须拦截内网地址 (SSRF 防护)。"""
    manager = SkillManager()
    await manager.register_skill(
        tenant_id=TENANT,
        skill_id="fetch",
        name="fetch",
        description="HTTP 请求",
        type=SkillType.HTTP_REQUEST,
        config={"url": "http://169.254.169.254/meta", "method": "GET"},
    )
    result = await manager.execute_skill(TENANT, "fetch", {})
    assert result.success is False
    assert "blocked" in result.error.lower() or "internal" in result.error.lower()


async def test_skill_tenant_isolation():
    """其他租户不得看到/执行不属于自己的技能。"""
    manager = SkillManager()
    await manager.register_skill(
        tenant_id="tenantA", skill_id="priv", name="priv",
        description="t", type=SkillType.PROMPT, config={"template": "x"},
    )
    assert await manager.list_skills("tenantB") == []
    result = await manager.execute_skill("tenantB", "priv", {})
    assert result.success is False
    assert "not found" in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
