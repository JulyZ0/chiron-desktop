"""保护依赖链断言：证明安全保护真的在保护（agent-autoloop 变异测试的轻量版）。

背景
----
`vendor/agent-autoloop/sim/mutate.ts` 预定义 31 个变异，每个把一处关键保护条件
改成放行，然后跑完整测试套件：**如果测试仍然全绿，说明那道保护根本没被测到**。
它抓的是"写了保护但没人验证"这类缺陷 —— 保护存在，但删掉也没人发现。

本文件用 ``monkeypatch`` 实现同思路的轻量版本，聚焦一个可判定的问题：
**上层判定是否真的依赖下层守卫**。

    monkeypatch 把守卫改成"永远放行" ⇒ 上层判定必须随之失效

如果上层仍然拒绝，说明它没走这个守卫（重复实现 / 缓存 / 走别的分支），
那么守卫被删掉也不会有人发现 —— 这正是要抓的。

局限（诚实说明）
----------------
这不是完整的变异测试：它证明"依赖链存在"，不证明"每个测试用例都依赖它"。
完整版本需要改写源码后跑整套测试（见 agent-autoloop 的 ``sim/mutate.ts``），
代价是源码可写 + 全量重跑，暂未引入。
"""

from __future__ import annotations

import pytest

from app.tools import ssrf


# ── 变异 1/2：路径守卫（相对穿越 / UNC）必须被 command_allowed 依赖 ──
#
# 这两条路径的 basename 都是 npx（白名单命中），只有路径守卫能拦住它们。
# 所以"放行守卫后命令被接受"是守卫生效的充要证据。

TRAVERSAL = "../../bin/npx"
UNC = "\\\\evil-host\\share\\npx"


@pytest.mark.parametrize(
    ("name", "attack"),
    [("relative-traversal", TRAVERSAL), ("unc", UNC)],
)
def test_allowlist_depends_on_path_guard(monkeypatch, name, attack):
    """把 _command_path_ok 改成永远放行，command_allowed 必须随之放行。

    若这里失败：说明 command_allowed 没走路径守卫 —— 变异存活，
    该守卫被删除后不会有任何测试变红。
    """
    monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")

    # 基线：守卫在，攻击路径被拒
    assert ssrf._command_path_ok(attack) is False, f"{name}: 路径守卫本身已失效"
    assert ssrf.command_allowed(attack) is False, f"{name}: 白名单没拦住攻击路径"

    # 变异：守卫永远放行
    monkeypatch.setattr(ssrf, "_command_path_ok", lambda _command: True)
    assert ssrf.command_allowed(attack) is True, (
        f"{name}: 变异存活 —— command_allowed 没有依赖 _command_path_ok，"
        "该守卫可被安全删除而不被发现"
    )


# ── 变异 3：白名单未配置时必须 fail-closed ──
#
# 这条不是依赖链，而是"默认值安全性"：环境变量缺失时不能放行任何命令。
# 若有人把 `if not raw: return False` 改成 `return True`（"方便开发"），
# 下面的断言立刻变红。


@pytest.mark.parametrize("command", ["npx", "/usr/bin/npx", "python", "curl"])
def test_allowlist_fails_closed_when_unset(monkeypatch, command):
    monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)
    assert ssrf.command_allowed(command) is False, (
        "白名单未配置时必须拒绝所有命令（fail-closed）"
    )


def test_allowlist_fails_closed_when_blank(monkeypatch):
    """空白字符串等价于未配置 —— 不能因为 `", ,"` 就让所有命令通过。"""
    monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "   ")
    assert ssrf.command_allowed("npx") is False


# ── 正向对照：防止上面的用例被"永远返回 False"的退化实现蒙混过关 ──


def test_allowlist_accepts_whitelisted_bare_name(monkeypatch):
    """配置正确时，白名单内的裸名必须被接受。

    没有这条，一个 `return False` 的错误实现也能让上面全部通过 ——
    保护"太严"和"太松"一样是缺陷。
    """
    monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
    assert ssrf.command_allowed("npx") is True


# ── 变异 4：deny_tools 必须在**注册阶段**剔除，且两种写法都要命中 ──
#
# 这一条比路径守卫更隐蔽：deny 列表本身就是保护，但如果匹配逻辑漏了一种
# 命名写法（裸名 vs 带前缀），出站工具会**照样注册**。实测就漏过 ——
# XERJ 返回的是 `xerj_field_report`，只按裸名匹配匹配不上。


def _fake_outbound_tools():
    """模拟 XERJ 的 tools/list：出站工具混在普通工具里，且带命名空间前缀。"""
    return [
        {"name": "xerj_code_search", "description": "read-only", "inputSchema": {}},
        # 这两种都是出站写操作，必须被剔除
        {"name": "xerj_field_report", "description": "SENDS to xerj.org", "inputSchema": {}},
        {"name": "corpus_feedback", "description": "SENDS", "inputSchema": {}},
    ]


@pytest.mark.parametrize("deny_name", ["field_report", "xerj_field_report"])
def test_deny_tools_matches_both_naming_forms(deny_name):
    """deny 列表写裸名或带前缀名，都必须能拦住同一个出站工具。

    若这里失败：说明匹配只覆盖一种写法，换成另一种就漏 —— 正是实测出现过的缺陷。
    """
    from app.mcp.client import MCPClient, ServerDef

    client = MCPClient([])
    server = ServerDef(
        name="xerj",
        command="xerj",
        read_only=True,
        deny_tools=frozenset({deny_name}),
    )
    client._register_tools(server, _fake_outbound_tools())  # noqa: SLF001

    names = {t.name for t in client.tools}
    assert "xerj_field_report" not in names, (
        f"deny_tools={{'{deny_name}'}} 没拦住 xerj_field_report —— 出站工具被注册了"
    )
    # 对照：只读工具必须留下，否则"全删"也能让上一行通过
    assert "xerj_code_search" in names


def test_deny_tools_empty_registers_everything():
    """空 deny 列表 = 不剔除。这条钉住"剔除确实由 deny_tools 驱动"。

    如果某天有人把过滤写成无条件剔除，上面的用例仍会绿，但功能已坏 ——
    这条是那个方向的反向对照。
    """
    from app.mcp.client import MCPClient, ServerDef

    client = MCPClient([])
    server = ServerDef(name="xerj", command="xerj", read_only=True)
    client._register_tools(server, _fake_outbound_tools())  # noqa: SLF001

    names = {t.name for t in client.tools}
    assert "xerj_field_report" in names
    assert "xerj_code_search" in names
