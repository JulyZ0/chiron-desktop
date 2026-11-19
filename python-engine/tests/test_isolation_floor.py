"""隔离档位**硬底线**：`none` / `job` 不能在 Windows 上被配置选中。

为什么需要这组测试
------------------
`SANDBOX_ISOLATION_ALLOW_DOWNGRADE` 原本是一个能放行**任何**降级的开关 ——
包括降到 `none`（完全没有隔离）。这与 README 的
「执行隔离是默认值；沙箱不可用时拒绝启动，不偷偷回退」直接冲突：
一个布尔开关不该有能力抹掉承诺。

现在的语义分两层，本文件把两层都钉住：

1. **配置改不动底线**：显式请求 `none`/`job` ⇒ Windows 上按 `FLOOR_LEVEL` 执行；
2. **缺机制也不放行**：实际档位落到 `FLOOR_LEVEL` 以下 ⇒ `below_floor=True`，
   `require_or_raise` 拒绝执行，且**不看** `allow_downgrade`。

第 2 条是这套设计的重点：开关放宽的是"稍弱的隔离"，不是"没有隔离"。
"""

from __future__ import annotations

import pytest

import app.isolation as iso
from app.isolation import (
    FLOOR_LEVEL,
    IsolationDecision,
    Level,
    require_or_raise,
    resolve_level,
)
from tests.source_scan import code_of


@pytest.fixture
def windows_healthy(monkeypatch):
    """模拟一台机制齐备的 Windows 机器：Job 与 Low IL 均可用。"""
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    return iso


# ── 第一层：请求值低于底线 → 按底线执行，不报错 ──


@pytest.mark.parametrize("low", [Level.NONE, Level.JOB])
def test_low_request_is_raised_to_floor(windows_healthy, low):
    """显式请求 `none`/`job`，Windows 上实际生效值必须顶到 `FLOOR_LEVEL`。"""
    decision = resolve_level(low)

    assert decision.effective.rank >= FLOOR_LEVEL.rank, (
        f"请求 {low.value} 竟然真的生效为 {decision.effective.value} —— 底线被绕过了"
    )
    assert decision.reasons, "抬到底线必须在 reasons 里说明，否则是静默改写"
    assert not decision.below_floor


def test_floor_itself_is_accepted(windows_healthy):
    """请求正好等于底线 ⇒ 原样生效，且不该有 reasons。"""
    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective == FLOOR_LEVEL
    assert decision.reasons == []
    assert not decision.below_floor


def test_request_above_floor_still_honored(windows_healthy):
    """底线不能反过来削掉更高的请求（appcontainer 仍要尝试）。"""
    decision = resolve_level(Level.APPCONTAINER)

    # APPCONTAINER 需要 profile 授权，这里未 mock，允许降级到 low_il ——
    # 但要确认没有因为底线逻辑被错误地压到更低。
    assert decision.effective.rank >= FLOOR_LEVEL.rank


# ── 第二层：机制缺失 → below_floor，且开关放不过去 ──


def test_missing_job_object_marks_below_floor(windows_healthy, monkeypatch):
    """Job Object 不可用 ⇒ 退到 `none`，必须标记 below_floor。"""
    monkeypatch.setattr(windows_healthy, "_probe_job", lambda: False)

    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective is Level.NONE
    assert decision.below_floor, "退到 none 却没标记 below_floor —— fail-closed 会失效"


def test_missing_low_il_marks_below_floor(windows_healthy, monkeypatch):
    """Low IL 不可设置 ⇒ 退到 `job`，低于底线，同样标记。"""
    monkeypatch.setattr(windows_healthy, "_probe_low_il", lambda: False)

    decision = resolve_level(FLOOR_LEVEL)

    assert decision.effective is Level.JOB
    assert decision.below_floor


def test_below_floor_cannot_be_waived_by_downgrade_switch(windows_healthy, monkeypatch):
    """**核心断言**：allow_downgrade=true 也放不过**被动**跌破底线的档位。

    这是本文件存在的理由。若这条变红，说明开关又变回了"能关掉隔离"。
    注意与"用户显式选 `none`"的差别：那种情况 `user_selected=True`，是本人决定；
    这里模拟的是**机制缺失**导致谁也不同意就掉到 `none`。
    """
    monkeypatch.setattr(windows_healthy, "_probe_low_il", lambda: False)
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: True)

    decision = resolve_level(FLOOR_LEVEL)
    assert decision.below_floor
    assert not decision.user_selected, "机制缺失导致的跌破不该被标成用户选择"

    with pytest.raises(RuntimeError) as exc:
        require_or_raise(decision)

    message = str(exc.value)
    assert "安全底线" in message, "拒绝信息必须点明是安全底线，而不是普通降级"
    assert "ALLOW_DOWNGRADE" in message, "要告诉用户那个开关为什么不管用"


# ── 普通降级仍可经开关放行（底线之上，不能一刀切） ──


def test_ordinary_degradation_still_needs_permission(windows_healthy, monkeypatch):
    """`appcontainer → low_il` 这类降级：默认拒绝。"""
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: False)

    decision = IsolationDecision(Level.APPCONTAINER, Level.LOW_IL, ["AppContainer 不可用"])

    with pytest.raises(RuntimeError) as exc:
        require_or_raise(decision)
    assert "ALLOW_DOWNGRADE" in str(exc.value)


def test_ordinary_degradation_allowed_when_permitted(windows_healthy, monkeypatch):
    """底线之上的降级，开关有效 —— 否则开发机无法使用。"""
    monkeypatch.setattr(windows_healthy, "downgrade_allowed", lambda: True)

    decision = IsolationDecision(Level.APPCONTAINER, Level.LOW_IL, ["AppContainer 不可用"])

    require_or_raise(decision)  # 不抛


def test_non_windows_is_not_subject_to_floor(monkeypatch):
    """Linux 侧保持既有 local 实现，不受硬底线约束（用户明确要求）。"""
    monkeypatch.setattr(iso.win, "IS_WINDOWS", False)

    decision = resolve_level(Level.LOW_IL)

    assert decision.effective is Level.NONE
    assert not decision.below_floor, "非 Windows 不该触发 Windows 硬底线"


def test_status_reports_floor_and_selectable(monkeypatch):
    """前端据此渲染选项卡：低档位可选，但 `none` 必须二次确认。

    这里锁的是**可达性**：把 `none`/`job` 挡在墙外，用户就只能改代码才能关掉
    隔离。但"可达"不等于"随手可点" —— `none` 要在 `acknowledge_required` 里，
    前端据此弹二次确认。
    """
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    monkeypatch.setattr(iso, "configured_level", lambda: FLOOR_LEVEL)

    payload = iso.status()

    assert payload["floor"] == FLOOR_LEVEL.value
    assert "below_floor" in payload
    selectable = payload["selectable"]
    assert Level.NONE.value in selectable, "none 必须可达，否则关隔离只能改代码"
    assert Level.JOB.value in selectable, "job 仍有 Job Object 边界，该直接可选"
    assert FLOOR_LEVEL.value in selectable
    # 只有"完全没有边界"的 none 需要确认；job 不值得为它加一道门槛
    assert payload["acknowledge_required"] == [Level.NONE.value]


def test_selectable_excludes_tiers_this_machine_cannot_run(monkeypatch):
    """★★ 面板不能把**本机做不到**的档位列成可选。

    缺陷形态（真机实测，非管理员账户）：

        selectable = ["low_il", "restricted", "appcontainer"]
        PUT restricted → requested=restricted, effective=low_il
        reasons: 受限令牌执行层不可用（CreateProcessAsUserW 失败 err=5）

    用户选了"受限令牌"、界面回一句"已切换"，实际强度一点没变 ——
    这就是「无法真实切换受限令牌」的全部原因。`selectable` 必须同时满足
    「不低于底线」与「本机真能执行」。
    """
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    # 本机能做 low_il / appcontainer，做不了 restricted（模拟缺 SeIncreaseQuotaPrivilege）
    monkeypatch.setattr(iso, "_probe_restricted", lambda: False)
    monkeypatch.setattr(iso, "_probe_appcontainer", lambda: True)
    monkeypatch.setattr(iso, "configured_level", lambda: FLOOR_LEVEL)

    payload = iso.status()

    assert Level.RESTRICTED.value not in payload["selectable"], (
        "本机做不到 restricted 却列成可选 —— 用户选了只会静默落到 low_il"
    )
    assert Level.LOW_IL.value in payload["selectable"]
    assert Level.APPCONTAINER.value in payload["selectable"]
    # 不可选的原因要能说清"缺机制"，而不是笼统的"低于底线"
    reason = payload["unavailable_reasons"].get(Level.RESTRICTED.value, "")
    assert reason, "缺机制却没有给出原因，用户无从知道该去提权"


def test_unavailable_reason_is_empty_for_below_floor_tiers(monkeypatch):
    """低于底线的档位**不**给"机器原因"—— 那是改配置也解除不了的另一回事。

    前端据此区分两种"不可选"：低于底线用 `isolation_below_floor_hint`，
    缺机制用 `isolation_unavailable_hint`。若这里也返回文案，前端就得去解析
    中英文措辞来分类，切语言或改措辞时必然失效。
    """
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    monkeypatch.setattr(iso, "configured_level", lambda: FLOOR_LEVEL)

    payload = iso.status()

    for low in (Level.NONE.value, Level.JOB.value):
        assert payload["unavailable_reasons"].get(low, "") == "", (
            f"{low} 低于底线，不该给机器原因"
        )


# ── 孤儿进程：agent 可触发的每个 spawn 点都必须挂 Job Object ──

#: agent 能间接驱动起子进程的模块。每一处 `await asyncio.create_subprocess_exec(`
#: 都必须配一次 `attach_job(`，否则那条路径在 Chiron 退出后会留下孤儿进程树。
#: （`shell_exec` / `run_code` / `persistent_shell` 走 `wrap_async_process`，另有守卫。）
_AGENT_SPAWN_SITES = (
    "app/api/plugins.py",
    "app/hooks/runner.py",
    "app/skill/manager.py",
    "app/mcp/client.py",
    "app/tools/git_tools.py",
    "app/tools/job_runner.py",
)


@pytest.mark.parametrize("path", _AGENT_SPAWN_SITES)
def test_every_agent_spawn_site_attaches_a_job_object(path):
    """★ 每个 spawn 点都要挂 Job Object —— 漏一处就是一条孤儿进程来源。

    孤儿是怎么来的：Windows 上 `KILL_ON_JOB_CLOSE` 是**唯一**能覆盖整棵进程树
    的机制。墙钟 timeout 只能 kill 直接子进程，而 `cmd /c build.bat`、`npx mcp-server`、
    插件脚本派生的孙进程都不归我们管。Chiron 退出（尤其是被强杀/崩溃，走不到任何
    `close()`）之后，那些进程会一直留在列表里 —— 用户看到的是"关掉应用，任务管理器
    里还挂着一堆 python.exe / node.exe"。

    用 `code_of` 剥掉注释再匹配：否则把调用注释掉的回归仍能通过字面量断言
    （这正是 `tests/source_scan.py` 存在的理由）。
    """
    code = code_of(path)
    spawns = code.count("await asyncio.create_subprocess_exec(")
    jobs = code.count("attach_job(")
    assert spawns > 0, f"{path} 里没有找到 spawn 点，守卫失效了"
    assert jobs >= spawns, (
        f"{path}: {spawns} 处 spawn 只有 {jobs} 处挂了 Job Object —— "
        "没挂的那些在 Chiron 退出后会留成孤儿进程"
    )


def test_attach_job_never_raises(monkeypatch):
    """`attach_job` 必须吞掉一切异常：隔离是**增强**，不是功能前提。

    它要接住两类失败：`resolve_level()` 在配置拼错时抛 `RuntimeError`；以及
    `JobObject.open()` 之外任何 Win32 调用失败。任何一处漏接，都会把"防孤儿"的
    收益换成"插件/MCP/git 直接起不来"，那不划算。
    """

    def _boom(*_a, **_kw):
        raise RuntimeError("invalid isolation level 'lowil'")

    monkeypatch.setattr(iso, "resolve_level", _boom)

    assert iso.attach_job(object(), label="probe") is None
