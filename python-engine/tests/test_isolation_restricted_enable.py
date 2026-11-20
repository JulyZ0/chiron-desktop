"""「受限令牌」档为什么启用不了 —— 逐条钉住探测判据与用户的执行路径。

## 这组用例针对的事故

设置页点「启用助手」，界面回「受限令牌执行助手：启用助手失败：本机无法启用此档位。」
真机上**任何账户**都复现，与管理员身份无关。三个独立缺陷叠在一起：

1. **网关没注册 `/v1/system/isolation/helper` 三条路由** ⇒ 前端 404，`catch` 落到兜底
   文案（Go 侧回归见 `internal/api/routes_proxy_isolation_helper_test.go`）。
2. **提权 helper 会调用自己**：helper 收到 `/v1/exec` 后走
   `win_launch.launch_restricted`，而它**优先**试 helper 这条路；helper 自己的
   `is_admin()` 为真、`helper_available()` 又会 ping 到自己（端口文件是它写的），
   于是它向自己再发一条 `/v1/exec` —— 无限递归，冒烟测试必然超时，
   `restricted` 因此永远不可选。⇒ `launch_restricted` 必须先判提权、提权就走本地。
3. **标准用户（非管理员）的 UAC 是"输入其他管理员账户"**，helper 因此在另一个账户下
   运行，`%LOCALAPPDATA%` 不是引擎的那个 ⇒ 读不到共享密钥，启动即退出。
   ⇒ 引擎必须把自己的状态目录用 `--state-dir` 下发。

另外还钉住两条判据边界：

* 「无隔离」档**不依赖任何助手**：切换它时不该碰 `restricted` / `appcontainer`
  的探测（那两个都要求提权助手）。被无关前置探测挡住会让"关掉隔离"变成空话。
* `reason` 必须逐档给**具体**原因（缺助手 / 执行层失败），而不是一句"不可用"。
"""

from __future__ import annotations

import pytest

import app.isolation as iso
from app.isolation import elevated_client, win, win_launch, win_token
from app.isolation import elevated_protocol as proto


@pytest.fixture(autouse=True)
def _reset_probe_and_override(monkeypatch):
    """每个用例都从"没有覆盖、没有缓存"开始。

    `_runtime_override` 与几个 `_x_ok` 都是模块级全局变量：不清会让上一个用例的
    结论带进下一个（"单跑绿、全跑红"的经典成因）。
    """
    monkeypatch.setattr(iso, "_runtime_override", None)
    monkeypatch.setattr(iso, "_runtime_acknowledged", False)
    monkeypatch.setattr(iso, "_restricted_ok", None)
    monkeypatch.setattr(iso, "_appcontainer_ok", None)
    monkeypatch.setattr(iso, "_job_ok", True)
    monkeypatch.setattr(iso, "_low_il_ok", True)
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    monkeypatch.setattr(iso, "appcontainer_workspace", lambda: "C:\\work")
    monkeypatch.setattr(iso, "record_level_change", lambda **_kw: None)
    yield


# ── 缺陷 2：helper 不得调用自己 ────────────────────────────────────


def test_elevated_process_never_goes_through_the_helper(monkeypatch, tmp_path):
    """已提权的进程（含 helper 自己）必须走本地路径，**不能**再向 helper 转一次。

    这是递归的源头：helper 里 `is_admin()` 为真、`helper_available()` 又会 ping 到
    自己（端口文件是它写的），于是"优先走 helper"变成"无限调用自己"。
    冒烟测试因此恒超时，`restricted` 永远不可选 —— 而界面只会说
    「启用助手失败：本机无法启用此档位」。
    """
    called: list[str] = []
    monkeypatch.setattr(win_launch, "is_admin", lambda: True)
    monkeypatch.setattr(win_launch, "helper_available", lambda: (True, ""))
    monkeypatch.setattr(
        win_launch,
        "_via_helper",
        lambda *a, **k: called.append("helper") or win_launch.LaunchResult(0, "ok", "", ""),
    )
    monkeypatch.setattr(
        win_launch,
        "_launch_restricted_local",
        lambda *a, **k: called.append("local") or win_launch.LaunchResult(0, "ok", "", ""),
    )

    win_launch.launch_restricted(["cmd.exe", "/c", "echo ok"], cwd=str(tmp_path), env={})

    assert called == ["local"], (
        f"已提权进程必须走本地路径，实际调用链 {called} —— "
        "走 helper 会让 helper 递归调用自己"
    )


def test_unelevated_process_still_uses_the_helper(monkeypatch, tmp_path):
    """未提权时 helper 才是正路：引擎自己建受限进程会恒 `err=5`。"""
    called: list[str] = []
    monkeypatch.setattr(win_launch, "is_admin", lambda: False)
    monkeypatch.setattr(win_launch, "helper_available", lambda: (True, ""))
    monkeypatch.setattr(
        win_launch,
        "_via_helper",
        lambda *a, **k: called.append("helper") or win_launch.LaunchResult(0, "ok", "", ""),
    )
    monkeypatch.setattr(
        win_launch,
        "_launch_restricted_local",
        lambda *a, **k: called.append("local") or win_launch.LaunchResult(0, "ok", "", ""),
    )

    win_launch.launch_restricted(["cmd.exe", "/c", "echo ok"], cwd=str(tmp_path), env={})

    assert called == ["helper"]


def test_helper_failure_is_not_silently_retried_into_unisolated_exec(monkeypatch, tmp_path):
    """helper 明确失败 ⇒ 原样返回 `spawn_error`，不退回无隔离执行。"""
    monkeypatch.setattr(win_launch, "is_admin", lambda: False)
    monkeypatch.setattr(win_launch, "helper_available", lambda: (True, ""))
    monkeypatch.setattr(
        win_launch,
        "_via_helper",
        lambda *a, **k: win_launch.LaunchResult(None, "", "", "helper HTTP 500"),
    )
    monkeypatch.setattr(
        win_launch,
        "_launch_restricted_local",
        lambda *a, **k: pytest.fail("helper 失败后不得本地重试：那会绕过 fail-closed"),
    )

    result = win_launch.launch_restricted(["cmd.exe"], cwd=str(tmp_path), env={})

    assert result.spawn_error == "helper HTTP 500"


# ── 缺陷 3：标准用户（UAC 输入其他管理员账户）也要能起来 ─────────────


def test_helper_argv_carries_the_engine_state_dir(monkeypatch, tmp_path):
    """`--state-dir` 必须下发。

    标准用户走 UAC 时只能输入**另一个管理员账户**的凭据，helper 于是在那个账户下
    运行 —— 它的 `%LOCALAPPDATA%` 不是引擎的，默认路径读不到共享密钥，
    `serve()` 直接返回 2，引擎侧只看到"helper 已启动但未就绪"。
    """
    argv = elevated_client.helper_argv(1234, str(tmp_path))

    joined = " ".join(argv)
    assert "app.isolation.elevated_helper" in joined
    assert "--parent-pid=1234" in argv
    assert f"--state-dir={tmp_path}" in argv


def test_custom_state_dir_redirects_key_and_port(monkeypatch, tmp_path):
    """`set_state_dir()` 之后密钥/端口都落在下发的目录里（helper 侧的唯一入口）。"""
    monkeypatch.setattr(proto, "_state_dir_override", None)
    try:
        proto.set_state_dir(tmp_path)
        assert proto.key_path() == tmp_path / "helper.key"
        assert proto.port_path() == tmp_path / "helper.port"

        key = b"k" * proto.KEY_BYTES
        assert proto.write_key(key, proto.key_path()) == ""
        assert proto.read_key() == key
    finally:
        proto.set_state_dir(None)


def test_engine_passes_its_state_dir_to_spawn(monkeypatch, tmp_path):
    """`spawn_elevated` 用的是**引擎自己**的目录，不是 helper 默认推导出来的。"""
    seen: dict[str, object] = {}

    class _FakeShell:
        def __init__(self, *_a, **_k):
            pass

        def __getattr__(self, _name):
            def _call(*args):
                seen["params"] = args[3]
                return 33  # > 32 即成功

            return _call

    monkeypatch.setattr(proto, "_state_dir_override", tmp_path)
    monkeypatch.setattr(elevated_client, "ensure_key", lambda: (b"k" * 32, ""))
    monkeypatch.setattr(win, "IS_WINDOWS", True)
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", _FakeShell)

    ok, reason, cancelled = elevated_client.spawn_elevated(4242)

    assert ok is True, reason
    assert cancelled is False
    assert f"--state-dir={tmp_path}" in str(seen["params"])
    proto.set_state_dir(None)


# ── reason 的每一个来源：都要说清"缺什么" ────────────────────


def test_helper_start_reports_actionable_reason_when_not_ready(monkeypatch):
    """helper 起来了但没就绪：原因必须能让用户动手（UAC 账户 / 安全软件）。"""
    monkeypatch.setattr(elevated_client, "spawn_elevated", lambda _pid=None: (True, "", False))
    monkeypatch.setattr(
        elevated_client, "wait_ready", lambda timeout=20.0: (False, "提权 helper 未启动（缺少端口文件）")
    )
    monkeypatch.setattr(win_launch, "reset_probe", lambda: None)

    ok, reason, cancelled = win_launch.helper_start()

    assert ok is False
    assert cancelled is False
    assert "未就绪" in reason
    assert ("UAC" in reason) or ("管理员账户" in reason), (
        f"失败原因必须可行动（要在 UAC 里允许 / 换账户批准），实际：{reason}"
    )
    assert "本机无法启用此档位" not in reason


def test_restricted_reason_points_at_the_helper_when_it_is_missing(monkeypatch):
    """缺助手 ⇒ 原因指向"去设置页启用助手"，而不是只报 Win32 错误码。"""
    monkeypatch.setattr(win_launch, "exec_ready", lambda: False)
    monkeypatch.setattr(win_launch, "exec_error", lambda: "CreateProcessAsUserW 失败 err=5")
    monkeypatch.setattr(win_launch, "helper_available", lambda: (False, "提权 helper 未启动（缺少端口文件）"))

    reason = win_token.exec_unavailable_reason()

    assert "err=5" in reason, "Win32 细节要留着，否则无法与其它故障区分"
    assert "受限令牌执行助手" in reason, "必须告诉用户去哪里修"
    assert "管理员授权" in reason


def test_restricted_reason_omits_helper_hint_when_helper_is_alive(monkeypatch):
    """助手已在跑却仍失败 ⇒ 别再催用户启用它（那会让人反复点同一个按钮）。"""
    monkeypatch.setattr(win_launch, "exec_ready", lambda: False)
    monkeypatch.setattr(win_launch, "exec_error", lambda: "exit=1 stderr=boom")
    monkeypatch.setattr(win_launch, "helper_available", lambda: (True, ""))

    reason = win_token.exec_unavailable_reason()

    assert "exit=1" in reason
    assert "请在设置页启用" not in reason


def test_resolve_reason_for_restricted_uses_the_probe_details(monkeypatch):
    """`resolve_level` 把不可用的**具体**原因写进 reasons（不是"不可用"三个字）。"""
    monkeypatch.setattr(win_token, "probe", lambda: True)
    monkeypatch.setattr(win_token, "exec_ready", lambda: False)
    monkeypatch.setattr(
        win_token,
        "exec_unavailable_reason",
        lambda: "受限令牌执行层不可用（CreateProcessAsUserW 失败 err=5）；"
        "提权 helper 不可用（提权 helper 未启动（缺少端口文件））——"
        "请在设置页启用「受限令牌执行助手」（会请求一次管理员授权）",
    )

    decision = iso.resolve_level(iso.Level.RESTRICTED)

    assert decision.effective is iso.Level.LOW_IL, "缺执行层时降级到 low_il（仍在安全底线之上）"
    assert decision.reasons
    assert any("受限令牌执行助手" in item for item in decision.reasons), decision.reasons


def test_status_exposes_a_reason_for_each_unavailable_level(monkeypatch):
    """不可选档位必须在 `unavailable_reasons` 里各有一条机器原因。"""
    monkeypatch.setattr(iso, "_probe_restricted", lambda: False)
    monkeypatch.setattr(iso, "_probe_appcontainer", lambda: False)
    monkeypatch.setattr(win_token, "exec_unavailable_reason", lambda: "缺 SeIncreaseQuotaPrivilege")
    monkeypatch.setattr(iso, "_appcontainer_probe_error", lambda: "包 SID 起不来")

    payload = iso.status()

    assert "restricted" not in payload["selectable"]
    assert payload["unavailable_reasons"]["restricted"] == "缺 SeIncreaseQuotaPrivilege"
    assert payload["unavailable_reasons"]["appcontainer"] == "包 SID 起不来"


# ── 「无隔离」档不依赖任何助手 ─────────────────────────────────


def test_none_level_never_probes_the_restricted_or_appcontainer_tiers(monkeypatch):
    """切到 `none` 不该碰 `restricted` / `appcontainer` 的探测。

    那两个探测都要求提权助手（或提权引擎）；把它们挂在 `none` 的路径上，
    等于"想关掉隔离"先得修好隔离 —— 用户确认过的事反而做不成。
    """
    monkeypatch.setattr(
        iso, "_probe_restricted", lambda: pytest.fail("none 档不该探测 restricted")
    )
    monkeypatch.setattr(
        iso, "_probe_appcontainer", lambda: pytest.fail("none 档不该探测 appcontainer")
    )
    monkeypatch.setattr(win_token, "grant_path", lambda *_a, **_k: pytest.fail("none 档不该授权路径"))

    iso.set_runtime_level(iso.Level.NONE, acknowledge_below_floor=True)
    decision = iso.resolve_level()

    assert decision.effective is iso.Level.NONE
    assert decision.below_floor is True
    assert decision.user_selected is True, "用户显式确认过的低档位必须放行（否则工具全废）"
    iso.require_or_raise(decision)  # 不抛
