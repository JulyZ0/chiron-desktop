"""`CreateProcessAsUserW` 之前的**特权启用**时序与失败文案。

## 这组用例针对的事故

用户机器上（`engine_elevated=true`、helper `available=true`、`supported=true`）
反复出现：

    GET /v1/system/isolation
      unavailable_reasons.restricted =
        "受限令牌执行层不可用：CreateProcessAsUserW 失败 err=5（拒绝访问）"

而 `logs/isolation_audit.jsonl` 里那一行是
``requested=restricted effective=low_il reason=""`` —— 既没有原因，也没法自查。

根因不在"没提权"，而在**提权令牌只是持有那两项特权**：
``SeAssignPrimaryTokenPrivilege`` 与 ``SeIncreaseQuotaPrivilege`` 在 UAC 拆分后的
管理员令牌里默认是**禁用**态，必须先用 ``AdjustTokenPrivileges`` 翻成启用。
少了这一步，API 只回一个完全没有指向性的 ``err=5``。

这里钉住四件事：

1. 两个特权都通过 ``AdjustTokenPrivileges`` 启用，且**先于** ``CreateProcessAsUserW``；
2. 启用失败时 reason 点名**缺哪个特权 / 哪个 API**（不是只给 err=5）；
3. 许可路径（本地 + 常驻会话）都走同一条启用逻辑；
4. 真实 Win32 探针在非提权环境下也**不该崩**（只返回 False + 具体原因）。
"""

from __future__ import annotations

import ctypes

import pytest

from app.isolation import win_launch, win_token

#: 俄罗斯套娃式的假 handle：只要不是 0 就会被 `finally` 关掉。
_FAKE_TOKEN_HANDLE = 0xC0FFEE
_FAKE_PROCESS = 0x1234


class _FakeK32:
    """只实现 `_enable_privilege` 用到的三个入口。"""

    def __init__(self, *, open_token_ok: bool = True) -> None:
        self.open_token_ok = open_token_ok

    def GetCurrentProcess(self):
        return _FAKE_PROCESS

    def OpenProcessToken(self, _process, _access, out_handle):
        if not self.open_token_ok:
            ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE
            return False
        out_handle._obj.value = _FAKE_TOKEN_HANDLE  # noqa: SLF001 — byref 的底层对象
        return True

    def CloseHandle(self, _handle):
        return True


class _FakeAdvapi:
    """记录调用顺序的 advapi32 桩。

    `AdjustTokenPrivileges` 的特权名不在参数里（那是 LUID），所以从
    `LookupPrivilegeValueW` 的 `lpName` 记住"这一次在处理哪一项" —— 两者在
    `_enable_privilege` 里是一一配对调用的。
    """

    def __init__(
        self,
        order: list[tuple[str, str]],
        *,
        adjust_returns: bool = True,
        adjust_last_error: int = 0,
        create_returns: bool = False,
    ) -> None:
        self.order = order
        self.adjust_returns = adjust_returns
        self.adjust_last_error = adjust_last_error
        self.create_returns = create_returns
        self._pending = ""

    def LookupPrivilegeValueW(self, _system, name, _luid):
        self._pending = name
        return True

    def AdjustTokenPrivileges(self, _token, _disable_all, _new_state, _len, _prev, _ret):
        self.order.append(("AdjustTokenPrivileges", self._pending))
        ctypes.set_last_error(self.adjust_last_error)
        return self.adjust_returns

    def CreateProcessAsUserW(self, *_args):
        self.order.append(("CreateProcessAsUserW", ""))
        ctypes.set_last_error(5)  # 让失败文案走 err=5 那条分支
        return self.create_returns


def _install_fake_win32(monkeypatch, advapi: _FakeAdvapi, k32: _FakeK32) -> None:
    """把两个模块的 Win32 入口都换成桩（`win_launch` 与 `win_token` 各有一份）。"""
    monkeypatch.setattr(win_token.win, "IS_WINDOWS", True)
    monkeypatch.setattr(win_token, "_advapi", lambda: advapi)
    monkeypatch.setattr(win_token, "_k32", lambda: k32)
    monkeypatch.setattr(win_launch, "_advapi", lambda: advapi)


def _stub_token(monkeypatch) -> None:
    """跳过真正的令牌派生与桌面创建：本组用例只关心启用/建进程的时序。"""
    monkeypatch.setattr(
        win_launch,
        "_restricted_token_for",
        lambda _pid, _ct: (win_token.TargetToken(token=_FAKE_TOKEN_HANDLE, source="self"), ""),
    )
    monkeypatch.setattr(win_launch, "ensure_desktop", lambda *_a, **_k: (True, ""))


def test_both_privileges_are_enabled_before_create_process_as_user(monkeypatch, tmp_path):
    """两个特权都必须经 `AdjustTokenPrivileges` 启用，且**先于**建进程。

    顺序反了等于没做：`CreateProcessAsUserW` 只看调用那一刻令牌里的启用状态。
    """
    order: list[tuple[str, str]] = []
    advapi = _FakeAdvapi(order)
    _install_fake_win32(monkeypatch, advapi, _FakeK32())
    _stub_token(monkeypatch)

    result = win_launch._launch_restricted_local(  # noqa: SLF001 — 直测这条路径本身
        ["cmd.exe", "/c", "echo ok"], cwd=str(tmp_path), env={}, timeout=1, max_output=1024
    )

    # 桩让建进程失败，所以一定拿不到 stdout；这里只关心调用序列。
    assert result.started is False
    assert order == [
        ("AdjustTokenPrivileges", "SeAssignPrimaryTokenPrivilege"),
        ("AdjustTokenPrivileges", "SeIncreaseQuotaPrivilege"),
        ("CreateProcessAsUserW", ""),
    ], f"启用必须发生在 CreateProcessAsUserW 之前，实际顺序 {order}"


def test_enable_failure_names_the_privilege_and_the_api(monkeypatch, tmp_path):
    """`ERROR_NOT_ALL_ASSIGNED`（1300）必须被当成失败，并点名特权与 API。

    这一条是 `err=5` 的替代品：同样是"起不来"，但用户能看出是**哪一项特权**
    没启用，而不是对着"拒绝访问"猜。
    """
    order: list[tuple[str, str]] = []
    # AdjustTokenPrivileges 返回 TRUE 但 last error = 1300 —— 官方文档里
    # "成功却未生效"的唯一形态，只看返回值会误判。
    advapi = _FakeAdvapi(order, adjust_returns=True, adjust_last_error=1300)
    _install_fake_win32(monkeypatch, advapi, _FakeK32())
    _stub_token(monkeypatch)

    result = win_launch._launch_restricted_local(  # noqa: SLF001
        ["cmd.exe", "/c", "echo ok"], cwd=str(tmp_path), env={}, timeout=1, max_output=1024
    )

    assert result.started is False
    assert "SeAssignPrimaryTokenPrivilege" in result.spawn_error
    assert "SeIncreaseQuotaPrivilege" in result.spawn_error
    assert "AdjustTokenPrivileges" in result.spawn_error, (
        f"失败原因必须点名 API，实际：{result.spawn_error}"
    )
    assert not any(name == "CreateProcessAsUserW" for name, _ in order), (
        "特权没启用时不该再去调 CreateProcessAsUserW —— 那只会换来一个无指向性的 err=5"
    )


def test_open_process_token_failure_is_reported_with_api_name(monkeypatch, tmp_path):
    """连自己的令牌都打不开时，原因也要点名 API，而不是静默继续。"""
    order: list[tuple[str, str]] = []
    advapi = _FakeAdvapi(order)
    _install_fake_win32(monkeypatch, advapi, _FakeK32(open_token_ok=False))
    _stub_token(monkeypatch)

    result = win_launch._launch_restricted_local(  # noqa: SLF001
        ["cmd.exe", "/c", "echo ok"], cwd=str(tmp_path), env={}, timeout=1, max_output=1024
    )

    assert result.started is False
    assert "OpenProcessToken" in result.spawn_error
    assert "SeIncreaseQuotaPrivilege" in result.spawn_error


def test_err5_message_points_at_the_privileges(monkeypatch):
    """真的拿到 err=5 时，文案必须补上"特权没启用"这条最可能的解释。"""
    msg = win_launch._spawn_error_with_hint(5)  # noqa: SLF001

    assert "err=5" in msg
    assert "SeAssignPrimaryTokenPrivilege" in msg
    assert "SeIncreaseQuotaPrivilege" in msg
    # 其它错误码不该被硬塞特权解释
    assert "SeIncreaseQuotaPrivilege" not in win_launch._spawn_error_with_hint(87)  # noqa: SLF001


def test_persistent_session_also_enables_privileges_first(monkeypatch, tmp_path):
    """常驻会话走的是另一段建进程代码，同样必须先把特权翻成启用。"""
    order: list[tuple[str, str]] = []
    advapi = _FakeAdvapi(order, adjust_returns=False, adjust_last_error=0)
    _install_fake_win32(monkeypatch, advapi, _FakeK32())
    monkeypatch.setattr(win_launch, "is_admin", lambda: True)
    monkeypatch.setattr(
        win_token,
        "create_restricted_token",
        lambda: (ctypes.c_void_p(_FAKE_TOKEN_HANDLE), win_token.SYNTHETIC_SID),
    )
    monkeypatch.setattr(win_launch, "ensure_desktop", lambda *_a, **_k: (True, ""))

    session, reason = win_launch.open_restricted_session(
        ["cmd.exe"], cwd=str(tmp_path), env={}
    )

    assert session is None
    assert "AdjustTokenPrivileges" in reason
    assert order and order[0][0] == "AdjustTokenPrivileges", (
        f"常驻会话也必须先启用特权，实际顺序 {order}"
    )
    assert not any(name == "CreateProcessAsUserW" for name, _ in order)


@pytest.mark.skipif(not win_token.win.IS_WINDOWS, reason="只在 Windows 上有进程令牌")
def test_real_token_probe_never_crashes():
    """真实 Win32 探针：非提权环境下探测特权能力**不应崩**，只应返回 False + 原因。

    这条用例刻意用真实 `advapi32`：它守的是"探测失败别把引擎打挂"。
    提权环境里它会返回 True（并且真的把该特权启用在本测试进程上 —— 无副作用，
    `AdjustTokenPrivileges` 只影响本进程后续建子进程）。
    """
    ok = win_token._enable_privilege(win_token.SE_INCREASE_QUOTA_NAME)  # noqa: SLF001

    if ok:
        assert win_token.privilege_error(win_token.SE_INCREASE_QUOTA_NAME) == ""
    else:
        reason = win_token.privilege_error(win_token.SE_INCREASE_QUOTA_NAME)
        assert win_token.SE_INCREASE_QUOTA_NAME in reason, (
            f"失败原因必须点名特权，实际：{reason}"
        )


@pytest.mark.skipif(not win_token.win.IS_WINDOWS, reason="只在 Windows 上有进程令牌")
def test_real_enable_launch_privileges_names_every_failed_privilege():
    """`enable_launch_privileges` 要把**每一项真的没启用**的特权都写进原因。

    只报第一项会让人修好一个再撞一次 —— 而两次失败长得一模一样。
    （部分成功是真实形态：本机实测非提权令牌上 `SeAssignPrimaryTokenPrivilege`
    可能启用成功、`SeIncreaseQuotaPrivilege` 报 1300。）
    """
    ok, reason = win_token.enable_launch_privileges()

    if ok:
        assert reason == ""
        return
    failed = [name for name in win_token.LAUNCH_PRIVILEGES if win_token.privilege_error(name)]
    assert failed, "失败时至少要有一项特权带原因"
    for name in failed:
        assert name in reason, f"reason 里缺少 {name}：{reason}"
    assert "AdjustTokenPrivileges" in reason or "LookupPrivilegeValueW" in reason
