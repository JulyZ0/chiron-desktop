"""独立桌面（`ChironSandbox`）的三条实测结论，各用一条测试钉住。

## 为什么这三条都必须有测试

它们都来自**真机排障**，而且每一条的错误形态都是"看起来像别的原因"：

1. **DACL 必须含当前用户 SID**。旧 DACL `D:(A;;GA;;;S-1-0-0)(A;;GA;;;SY)(A;;GA;;;BA)`
   里 `S-1-0-0` 是 NULL SID，**不匹配任何主体**（只对"匿名"这个抽象主体有意义）。
   于是 `CreateDesktopW` 成功、随后 `OpenDesktopW(同名)` 恒 `err=5`。
   这个症状被误读成"缺 `SeIncreaseQuotaPrivilege`"，用户去提权 —— 而提权只会换
   一个同样打不开的身份（UAC 下 `BUILTIN\\Administrators` 是 deny-only 组）。

2. **名字被占用不能等同"机制不可用"**。窗口站内桌面名全局唯一，多实例启动时
   第二个实例 `CreateDesktopW` 必然失败。它应当先尝试**打开**同名桌面，仍不行
   再退回带 pid 的名字 —— 否则"开第二个窗口"就把 `restricted` 档永久废掉。

3. **`OpenDesktopW` 必须声明 `restype = HANDLE`**。ctypes 默认返回 `c_int`，
   64 位下句柄会被截断；用残缺句柄当 `lpDesktop` 的进程启动失败，
   且失败原因与句柄无关，极难定位。

全部用替身，不需要提权，也不需要真的建桌面。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

import pytest

from app.isolation import win, win_launch, win_token

pytestmark = pytest.mark.skipif(not win.IS_WINDOWS, reason="独立桌面是 Windows 专有机制")

#: 一条形状正确的用户 SID（值不重要，只验证它被真正写进 DACL）
TEST_USER_SID = "S-1-5-21-1111-2222-3333-1001"


@pytest.fixture
def clean_desktop(monkeypatch):
    """把桌面模块的全局状态复位，避免用例间互相污染。

    `ensure_desktop` 用 `global` 赋值，monkeypatch 的恢复会把这个属性设回原值，
    所以这里只需声明"测试开始时是干净的"。
    """
    monkeypatch.setattr(win_launch, "_desktop_handle", None)
    monkeypatch.setattr(win_launch, "_desktop_error", "")
    monkeypatch.setattr(win_launch, "_desktop_name", win_token.DESKTOP_NAME)
    yield


# ── ① DACL 必须含当前用户 SID ──────────────────────────────────


def test_desktop_sddl_includes_the_current_user(monkeypatch):
    """DACL 缺当前用户 ⇒ 连创建者自己都打不开这个桌面。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: TEST_USER_SID)

    sddl = win_launch._desktop_sddl()  # noqa: SLF001 — 直接验证 DACL 内容

    assert TEST_USER_SID in sddl, "缺当前用户 SID 的 DACL 连创建者都打不开桌面"
    # 受限子进程仍需要合成 SID：它带 WRITE_RESTRICTED，写访问必须由 restricting SID 命中
    assert "S-1-0-0" in sddl, "去掉合成 SID 后受限子进程反而打不开桌面"
    assert ";SY)" in sddl and ";BA)" in sddl, "服务账户与管理员会话的正常路径不能丢"


def test_desktop_sddl_is_empty_without_user_sid(monkeypatch):
    """取不到用户 SID 时返回空串 —— 宁可失败，也不要建一个权限不明的桌面。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: "")

    assert win_launch._desktop_sddl() == ""  # noqa: SLF001


def test_ensure_desktop_refuses_when_user_sid_is_unknown(monkeypatch, clean_desktop):
    """拿不到当前用户 SID 时 `ensure_desktop` 必须 fail-closed。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: "")

    def _must_not_be_called(*_a, **_kw):  # pragma: no cover - 被调用即失败
        raise AssertionError("拿不到用户 SID 时不该尝试创建桌面")

    monkeypatch.setattr(win_launch, "_create_or_open_desktop", _must_not_be_called)

    ok, err = win_launch.ensure_desktop()

    assert ok is False
    assert "SID" in err, "失败原因要指向真正的问题（拿不到用户 SID），而不是笼统的不可用"


# ── ② 名字被占用 ⇒ 复用同名 / 退回带 pid 的名字 ────────────────


def test_ensure_desktop_reuses_the_preferred_name(monkeypatch, clean_desktop):
    """首选名字可用时，必须用它，且不产生 pid 后缀。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: TEST_USER_SID)
    calls: list[str] = []

    def fake_create(name: str, _sddl: str):
        calls.append(name)
        return object(), ""

    monkeypatch.setattr(win_launch, "_create_or_open_desktop", fake_create)

    ok, err = win_launch.ensure_desktop()

    assert ok is True and err == ""
    assert calls == [win_token.DESKTOP_NAME]
    assert win_launch.desktop_name() == win_token.DESKTOP_NAME


def test_ensure_desktop_falls_back_to_pid_name(monkeypatch, clean_desktop):
    """首选名字打不开（旧版本残留的 DACL）⇒ 换带 pid 的名字，而不是全档不可用。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: TEST_USER_SID)
    fallback = f"{win_token.DESKTOP_NAME}-{os.getpid()}"
    calls: list[str] = []

    def fake_create(name: str, _sddl: str):
        calls.append(name)
        if name == win_token.DESKTOP_NAME:
            return None, "dacl 不含当前用户"
        return object(), ""

    monkeypatch.setattr(win_launch, "_create_or_open_desktop", fake_create)

    ok, err = win_launch.ensure_desktop()

    assert ok is True and err == ""
    assert calls == [win_token.DESKTOP_NAME, fallback]
    assert win_launch.desktop_name() == fallback, (
        "实际桌面名必须跟着回退结果走，否则子进程会被丢到不存在的桌面上"
    )


def test_ensure_desktop_reports_both_attempts_on_total_failure(monkeypatch, clean_desktop):
    """两个名字都失败时，错误信息要同时指向它们，而不是只报第一个。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: TEST_USER_SID)

    def fake_create(name: str, _sddl: str):
        return None, f"{name} 不可用"

    monkeypatch.setattr(win_launch, "_create_or_open_desktop", fake_create)

    ok, err = win_launch.ensure_desktop()

    assert ok is False
    assert win_token.DESKTOP_NAME in err
    assert f"{win_token.DESKTOP_NAME}-{os.getpid()}" in err


def test_ensure_desktop_caches_the_handle(monkeypatch, clean_desktop):
    """成功一次之后不再重复建桌面。"""
    monkeypatch.setattr(win_token, "current_user_sid", lambda: TEST_USER_SID)
    calls: list[str] = []

    def fake_create(name: str, _sddl: str):
        calls.append(name)
        return object(), ""

    monkeypatch.setattr(win_launch, "_create_or_open_desktop", fake_create)

    first = win_launch.ensure_desktop()
    second = win_launch.ensure_desktop()

    assert first == (True, "") and second == (True, "")
    assert len(calls) == 1, "已有句柄时不该再建一次桌面（那会创建出同名冲突）"


# ── ③ 创建失败 ⇒ 先尝试打开同名桌面 ────────────────────────────


class _FakeKernel32:
    def LocalFree(self, _ptr):  # noqa: N802 — 沿用 Win32 名称
        return None


class _FakeAdvapi:
    def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, *_a):  # noqa: N802
        return True


class _FakeUser32:
    """按需返回句柄的 user32 替身；句柄用普通 int 表示。"""

    def __init__(self, create: int, open_: int) -> None:
        self._create = create
        self._open = open_
        self.opened: list[str] = []

    def CreateDesktopW(self, name, *_a):  # noqa: N802
        return self._create

    def OpenDesktopW(self, name, *_a):  # noqa: N802
        self.opened.append(name)
        return self._open


def _install_desktop_fakes(monkeypatch, *, create: int, open_: int) -> _FakeUser32:
    u32 = _FakeUser32(create=create, open_=open_)
    monkeypatch.setattr(win_launch, "_k32", lambda: _FakeKernel32())
    monkeypatch.setattr(win_launch, "_advapi", lambda: _FakeAdvapi())
    monkeypatch.setattr(win_launch, "_user32", lambda: u32)
    # ctypes 的上一次错误在线程局部里，测试里无法直接置位；固定成 0 让错误文案可预测
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)
    monkeypatch.setattr(ctypes, "set_last_error", lambda _code: None)
    return u32


def test_create_or_open_desktop_reuses_an_existing_desktop(monkeypatch):
    """`CreateDesktopW` 失败（同名桌面已存在）⇒ 打开它，而不是判不可用。"""
    u32 = _install_desktop_fakes(monkeypatch, create=0, open_=4242)

    handle, err = win_launch._create_or_open_desktop("ChironSandbox", "D:")  # noqa: SLF001

    assert handle == 4242, "已存在且 DACL 合规的同名桌面应当被多实例共享"
    assert err == ""
    assert u32.opened == ["ChironSandbox"]


def test_create_or_open_desktop_reports_both_errors(monkeypatch):
    """两个都失败时，文案要让人想到"残留桌面"，而不是只看到 CreateDesktopW 的 err。"""
    _install_desktop_fakes(monkeypatch, create=0, open_=0)

    handle, err = win_launch._create_or_open_desktop("ChironSandbox", "D:")  # noqa: SLF001

    assert handle is None
    assert "无法创建" in err and "无法打开" in err


def test_create_or_open_desktop_prefers_create(monkeypatch):
    """能建就直接建，不该白白多一次 `OpenDesktopW`。"""
    u32 = _install_desktop_fakes(monkeypatch, create=777, open_=4242)

    handle, err = win_launch._create_or_open_desktop("ChironSandbox", "D:")  # noqa: SLF001

    assert handle == 777 and err == ""
    assert u32.opened == []


# ── ④ 句柄类型声明（64 位截断的隐患）──────────────────────────


def test_user32_declares_open_desktop_handle_return_type():
    """`OpenDesktopW` 未声明 `restype` 时返回 `c_int`，64 位下句柄会被截断。"""
    u32 = win_launch._user32()  # noqa: SLF001

    assert u32.OpenDesktopW.restype is wt.HANDLE
    assert u32.CreateDesktopW.restype is wt.HANDLE
