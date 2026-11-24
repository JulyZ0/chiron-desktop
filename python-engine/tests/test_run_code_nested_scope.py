"""H4 攻击探针：`run_code` 嵌套工具调用必须重新授权、越权必须拒绝并留审计。

三条探针对应验收标准的三句：

1. **只注入显式 scoped capabilities** —— 作用域是白名单相加，未登记的工具（委派、销毁、
   外部触达、MCP/插件代理、另两条代码执行入口）一律不在注入面里。
2. **嵌套调用重新 authorization** —— 子进程绕不过去也照样在主进程被复核；作用域内但
   参数把它降级成 delete/external 的调用同样拒绝。
3. **越权调用必须拒绝并留审计事件** —— 拒绝路径写 `exec_audit`（outcome=blocked），
   记录"谁在哪个 run_code 里试图调用什么"。

为什么探针要走 `_safe_tool_call`（主进程侧）而不是只跑 `run_code`：子进程侧
`ToolProxy` 用注入名字集合兜第一道，跑 `run_code` 只能证明"子进程自己拒绝了"；
真正的边界在**不可信子进程之外**，所以攻击者假设是"子进程被绕过/被改坏，直接向主进程
发 tool_call"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools import run_code as run_code_mod
from app.tools.context import set_tool_context
from app.tools.exec_audit import EXEC_AUDIT_FILENAME
from app.tools.nested_scope import (
    RUN_CODE_CAPABILITIES,
    authorize_nested_call,
    scoped_tool_names,
)
from app.tools.registry import ToolDef, registry
from app.tools.run_code import _safe_tool_call, run_code


@pytest.fixture()
def audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把执行审计落盘改到 tmp_path，避免污染仓库 logs/。"""
    import app.tools.exec_audit as exec_audit

    target = tmp_path / "audit"
    monkeypatch.setattr(exec_audit, "AUDIT_DIR", target)
    return target


def _audit_entries(audit_dir: Path) -> list[dict]:
    path = audit_dir / EXEC_AUDIT_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture()
def probe_tool(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """注册一个"作用域之外"的假工具，用来证明它既不被注入、也不会被执行。"""
    state: dict[str, object] = {"called": False, "args": None}

    async def _handler(**kwargs):  # noqa: ANN003
        state["called"] = True
        state["args"] = kwargs
        return {"exfiltrated": True}

    monkeypatch.setitem(
        registry._tools,  # noqa: SLF001 — 测试直接摆一张注册表里的假工具
        "exfil_probe",
        ToolDef(
            name="exfil_probe",
            description="out-of-scope probe",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_handler,
        ),
    )
    return state


# ── 1. 只注入显式 scoped capabilities ─────────────────────────────


def test_scope_excludes_delegation_destructive_and_external_tools() -> None:
    scope = scoped_tool_names()

    # 注入面里必须有的（普通批处理要用的读写/检索/git 只读）
    for name in ("read_file", "write_file", "grep_files", "ls", "git_status", "shell_exec"):
        assert name in scope, name
    # 注入面里**必须没有**的：委派、销毁、外部触达、MCP 代理、另两条代码执行入口。
    for name in (
        "subagent",
        "rerun_subagent",
        "resume_subagent",
        "best_of_n",
        "delete_file",
        "forget",
        "job_kill",
        "web_fetch",
        "web_search",
        "browser_navigate",
        "use_capability",
        "skill_run",
        "skill_install",
        "skill_generate",
        "persistent_shell",
        "execute_python",
        "run_code",
        "media_create",
        "ask_user",
        "tool_search",
    ):
        assert name not in scope, f"{name} must not be injected into run_code"


def test_scope_is_allowlist_intersection_not_registry_dump() -> None:
    """白名单之外的注册项一个都不进：注册表再大，作用域也只有白名单那几个。"""
    assert scoped_tool_names() <= RUN_CODE_CAPABILITIES
    # 注册表里有、白名单里没有的工具（例如 browser_*）必须被交集削掉。
    assert "browser_click" in registry.list_names()
    assert "browser_click" not in scoped_tool_names()


def test_session_hook_can_only_narrow() -> None:
    set_tool_context(run_code_capabilities=["read_file", "delete_file"])
    try:
        scope = scoped_tool_names()
    finally:
        set_tool_context(run_code_capabilities=None)

    assert "read_file" in scope
    # 会话钩子试图加入白名单之外的工具 —— 交集把"放宽"变成无效。
    assert "delete_file" not in scope


@pytest.mark.asyncio
async def test_injected_init_payload_is_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """接线探针：真正注入给子进程的 tools 清单就是作用域，而不是 `registry.list_names()`。"""
    captured: dict[str, object] = {}

    async def _fake_run_in_subprocess(code, tool_names, timeout):
        captured["tools"] = list(tool_names)
        return {"logs": "", "result": "ok"}

    monkeypatch.setattr(run_code_mod, "_run_in_subprocess", _fake_run_in_subprocess)
    set_tool_context(session_id="s", user_id="u-h4", tenant_id=1, gateway=None)

    out = await run_code("return 'ok'")

    assert out["result"] == "ok"
    tools = captured["tools"]
    assert "read_file" in tools
    assert "subagent" not in tools and "delete_file" not in tools
    assert "use_capability" not in tools


# ── 2/3. 重新授权 + 越权必须拒绝并留审计 ──────────────────────────


@pytest.mark.asyncio
async def test_out_of_scope_nested_call_refused_and_audited(
    probe_tool: dict[str, object], audit_dir: Path
) -> None:
    set_tool_context(session_id="s-h4", user_id="u-h4", tenant_id=1, gateway=None)

    result = await _safe_tool_call("exfil_probe", {"target": "secret"}, None, "c1")

    assert result.get("denied") is True, result
    assert result.get("code") == "nested_call_denied"
    assert "outside the run_code capability scope" in result["error"]
    assert probe_tool["called"] is False, "out-of-scope tool must never execute"

    entries = [e for e in _audit_entries(audit_dir) if "exfil_probe" in str(e.get("tool"))]
    assert entries, "refused nested call must leave an audit event"
    entry = entries[-1]
    assert entry["outcome"] == "blocked"
    assert entry["session"] == "s-h4" and entry["user"] == "u-h4" and entry["tenant"] == "1"
    assert "outside the run_code capability scope" in entry["reason"]
    assert "c1" in entry["command"], "audit must carry the nested call id"


@pytest.mark.asyncio
async def test_destructive_args_inside_scope_are_refused(audit_dir: Path) -> None:
    """`shell_exec` 在白名单里，但 `rm -rf` 让它落到 delete 级 —— 嵌套里不得直接放行。"""
    set_tool_context(session_id="s-h4", user_id="u-h4", tenant_id=1, gateway=None)

    denied = await _safe_tool_call("shell_exec", {"command": "rm -rf ./data"}, None, "c2")
    assert denied.get("denied") is True, denied
    assert "requires per-tool authorization" in denied["error"]

    entries = [e for e in _audit_entries(audit_dir) if "shell_exec" in str(e.get("tool"))]
    assert entries and entries[-1]["outcome"] == "blocked"
    assert "requires per-tool authorization" in entries[-1]["reason"]


@pytest.mark.asyncio
async def test_explicit_grant_is_the_only_way_to_allow_delete_level() -> None:
    """会话显式 grant 后同一调用放行 —— 证明拒绝来自"未授权"，而不是硬编码禁用。"""
    set_tool_context(
        session_id="s-h4",
        user_id="u-h4",
        tenant_id=1,
        gateway=None,
        run_code_grants=["shell_exec"],
    )
    try:
        assert authorize_nested_call("shell_exec", {"command": "rm -rf ./data"}) is None
    finally:
        set_tool_context(run_code_grants=None)
    assert authorize_nested_call("shell_exec", {"command": "rm -rf ./data"}) is not None


@pytest.mark.asyncio
async def test_scoped_read_call_still_runs() -> None:
    """不误伤：作用域内的只读工具照常执行（拒绝不应变成"全拒"）。"""
    from app.tools.sandbox import workspace_dir

    set_tool_context(session_id="s-h4", user_id="u-h4", tenant_id=1, gateway=None)
    (workspace_dir() / "h4-ok.txt").write_text("h4-ok", encoding="utf-8")

    result = await _safe_tool_call("read_file", {"path": "h4-ok.txt"}, None, "c3")

    assert result.get("error") is None, result
    assert result.get("content") == "h4-ok"


@pytest.mark.asyncio
async def test_run_code_program_cannot_reach_out_of_scope_tool(
    probe_tool: dict[str, object], audit_dir: Path
) -> None:
    """端到端：程序里调一个没被注入的工具 → 失败返回，且假工具从未执行。"""
    set_tool_context(session_id="s-h4e", user_id="u-h4", tenant_id=1, gateway=None)

    out = await run_code("await tools.exfil_probe(target='secret')\nreturn 'unreachable'")

    assert out.get("isError") is True, out
    assert "exfil_probe" in out["message"]
    assert probe_tool["called"] is False
