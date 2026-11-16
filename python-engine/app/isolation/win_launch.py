"""以受限令牌 + 独立桌面拉起子进程。

本模块存在的唯一理由：`win_token` 能造出受限令牌，但**令牌不拿去建进程就等于没有隔离**。
`CreateRestrictedToken` + `CreateProcessAsUserW` 这条路上有三个必须一起处理的细节，
少做任何一个都会让"看起来有隔离"变成"实际没有"：

1. **管道必须可继承**。`asyncio.create_subprocess_exec` 建的管道默认不可继承，
   直接塞给 `CreateProcessAsUserW` 会让子进程拿不到 stdout/stderr（或更糟：
   继承到父进程的其他句柄）。这里用 `CreatePipe` + `SECURITY_ATTRIBUTES(bInheritHandle=TRUE)`
   自建管道，再把父端（读端）的继承位清掉。
2. **环境块必须显式构造**。传 `lpEnvironment=NULL` 会让子进程继承引擎的完整环境 ——
   包括 `API_KEY` / `JWT_SECRET` / `INTERNAL_TOKEN`。那等于绕开了 `sandboxed_env()`。
3. **`lpDesktop` 必须指向非默认桌面**。微软官方明确警告：受限进程若运行在默认桌面，
   仍可对同桌面上的非受限进程发 `SendMessage`/`PostMessage`。chiron 是 Electron 应用，
   主进程窗口就在默认桌面上 —— 不隔离桌面，这条攻击面是敞开的。

`EXEC_READY` 在 `win_token` 里的语义由此明确：本模块整条链路可用。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.isolation import win, win_token

logger = logging.getLogger(__name__)


# ── Win32 常量 ──────────────────────────────────────────────────

CREATE_UNICODE_ENVIRONMENT = 0x0000_0400
STARTF_USESTDHANDLES = 0x0000_0100
CREATE_NO_WINDOW = 0x0800_0000
HANDLE_FLAG_INHERIT = 0x0000_0001
INFINITE = 0xFFFF_FFFF
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: `STARTUPINFOEXW` 的标志位。用 attribute list 就必须带它，否则 `lpAttributeList`
#: 被内核忽略 —— 表现是"进程起来了，但完全不在 AppContainer 里跑"，比直接失败更糟。
EXTENDED_STARTUPINFO_PRESENT = 0x0008_0000

#: `UpdateProcThreadAttribute` 的 attribute id：包 SID 与能力集。
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x0002_0009

#: 桌面完整访问权限
DESKTOP_ALL_ACCESS = 0x000F_01FF
READ_CONTROL = 0x0002_0000
SYNCHRONIZE = 0x0010_0000
PROCESS_TERMINATE = 0x0001


class _SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名
    _fields_ = [
        ("nLength", wt.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wt.BOOL),
    ]


class _STARTUPINFOEXW(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名
    """`STARTUPINFOW` + attribute list 指针。AppContainer 只能走这条结构体。

    `subprocess` / `asyncio.create_subprocess_exec` 都无法表达 attribute list，
    所以 `appcontainer` 档必须像 `restricted` 一样自建进程（见 `launch_appcontainer`）。
    """

    _fields_ = [
        ("StartupInfo", win_token._STARTUPINFOW),  # noqa: SLF001 — 同一套 SDL 定义
        ("lpAttributeList", ctypes.c_void_p),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名
    """`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 的值结构。

    `CapabilityCount=0` 是刻意的：给 0 个能力意味着**没有任何**受控能力
    （不含网络、不含大部分目录）。这正是 `appcontainer` 比 `low_il` 强的地方。
    """

    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


@dataclass(frozen=True)
class LaunchResult:
    """一次受限执行的结果。`spawn_error` 非空表示**进程都没起来**，与"起来了但失败"分开。"""

    exit_code: int | None
    stdout: str
    stderr: str
    spawn_error: str = ""

    @property
    def started(self) -> bool:
        return self.spawn_error == ""


@dataclass
class _Spawned:
    """AppContainer 进程的句柄集合（创建成功后的中间态）。

    `stdin_write` 为 `None` 表示 stdin 接在空设备上（一次性执行不需要输入）。
    """

    pi: Any
    out_r: Any
    err_r: Any
    stdin_write: Any
    k32: Any


def _k32() -> Any:
    """kernel32 中本模块需要的额外入口（`win_token._k32()` 未覆盖的部分）。"""
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreatePipe.restype = wt.BOOL
    lib.CreatePipe.argtypes = [
        ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(wt.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wt.DWORD,
    ]
    lib.SetHandleInformation.restype = wt.BOOL
    lib.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
    lib.ReadFile.restype = wt.BOOL
    lib.ReadFile.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ]
    # 常驻 shell 需要写入端与主动终止（一次性路径用不到，但那两条路径共用本函数）
    lib.WriteFile.restype = wt.BOOL
    lib.WriteFile.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
        ctypes.c_void_p,
    ]
    lib.TerminateProcess.restype = wt.BOOL
    lib.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    lib.LocalFree.restype = ctypes.c_void_p
    lib.LocalFree.argtypes = [ctypes.c_void_p]
    # AppContainer 路径：attribute list 三个入口 + 带 attribute list 的 CreateProcessW。
    #
    # `argtypes` 不是可选的：64 位下不声明，ctypes 按 C `int` 传指针，
    # 高 32 位被截断 ⇒ 稳定 `ERROR_INVALID_PARAMETER(87)`，而错误信息完全不提。
    lib.InitializeProcThreadAttributeList.restype = wt.BOOL
    lib.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.UpdateProcThreadAttribute.restype = wt.BOOL
    lib.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.DeleteProcThreadAttributeList.restype = None
    lib.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    # AppContainer **不走** CreateProcessAsUserW：它不需要另一个令牌，只需要 attribute
    # list，因此普通（非提权）账户也能用 —— 这正是它比 `restricted` 更可达的原因。
    lib.CreateProcessW.restype = wt.BOOL
    lib.CreateProcessW.argtypes = [
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.GetExitCodeProcess.restype = wt.BOOL
    lib.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    lib.WaitForSingleObject.restype = wt.DWORD
    lib.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    lib.CloseHandle.restype = wt.BOOL
    lib.CloseHandle.argtypes = [wt.HANDLE]
    return lib


def _user32() -> Any:
    """`CreateDesktopW` / `CloseDesktop` 属于 **user32**，不在 kernel32。

    这个归属很容易记错（桌面听起来像内核对象），实测 `kernel32.CreateDesktopW`
    会直接 `AttributeError: function not found`。
    """
    lib = ctypes.WinDLL("user32", use_last_error=True)
    lib.CreateDesktopW.restype = wt.HANDLE
    lib.CreateDesktopW.argtypes = [
        wt.LPCWSTR,
        wt.LPCWSTR,
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
    ]
    lib.CloseDesktop.restype = wt.BOOL
    lib.CloseDesktop.argtypes = [wt.HANDLE]
    return lib


def _advapi() -> Any:
    """本模块只用 advapi32 的 SDDL 转换；桌面 API 归 user32（见 `_user32`）。"""
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    lib.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.DWORD),
    ]
    # AppContainer 的 `SECURITY_CAPABILITIES.AppContainerSid` 要的是 **PSID**，
    # 而 `ensure_appcontainer` 只给**字符串** SID（便于 icacls 与日志）。
    # 少了这一步就得让调用方各自转换，而转换要配对 `LocalFree`，漏掉就是每次
    # 执行泄漏一块内核内存。
    lib.ConvertStringSidToSidW.restype = wt.BOOL
    lib.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    return lib


# ── 独立桌面 ────────────────────────────────────────────────────

#: 桌面 DACL：合成 SID / SYSTEM / Administrators 完全控制。
#:
#: 为什么必须显式给合成 SID：受限令牌做**写**访问检查时，必须由 restricting SID
#: 之一满足（`WRITE_RESTRICTED` 的确切语义）。桌面创建与窗口创建都是写操作，
#: 只给 Everyone 权限在受限令牌下不够 —— 这是实测过的失败点，不是推测。
_DESKTOP_SDDL = "D:(A;;GA;;;S-1-0-0)(A;;GA;;;SY)(A;;GA;;;BA)"

_desktop_handle: Any = None
_desktop_error: str = ""


def ensure_desktop() -> tuple[bool, str]:
    """创建（或复用）独立桌面 `ChironSandbox`。成功返回 `(True, "")`。

    句柄在本进程内缓存并不主动关闭：桌面在**创建它的进程退出时**自动销毁，
    这正是我们要的语义（引擎重启 ⇒ 桌面重建，不残留）。
    """
    global _desktop_handle, _desktop_error
    if not win.IS_WINDOWS:
        return False, "not windows"
    if _desktop_handle:
        return True, ""
    if _desktop_error:
        return False, _desktop_error

    k32, adv, u32 = _k32(), _advapi(), _user32()
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _DESKTOP_SDDL, 1, ctypes.byref(sd), None
    ):
        _desktop_error = f"SDDL 转换失败 err={ctypes.get_last_error()}"
        logger.warning("ensure_desktop: %s", _desktop_error)
        return False, _desktop_error

    sa = _SECURITY_ATTRIBUTES(
        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
        lpSecurityDescriptor=sd,
        bInheritHandle=False,
    )
    try:
        # 参数顺序必须严格照 SDK 文档：
        # (lpszDesktop, lpszDevice, pDevmode, dwFlags, dwDesiredAccess, lpsa)
        # 把 lpsa 放到第 3 位是最容易犯的错 —— pDevmode 位置收到一个
        # SECURITY_ATTRIBUTES 指针，结果是稳定的 ERROR_INVALID_PARAMETER(87)，
        # 而错误信息不会提示是哪个参数。本机实测踩过这个坑。
        # lpszDesktop 只接受**桌面名本身**，不接受 `WinSta0\` 前缀 ——
        # 带前缀会稳定返回 ERROR_BAD_PATHNAME(161)。窗口站是隐含的（当前就是
        # WinSta0）；`STARTUPINFOW.lpDesktop` 那边才需要 `WinSta0\名称` 全路径。
        handle = u32.CreateDesktopW(
            win_token.DESKTOP_NAME,
            None,
            None,
            0,
            DESKTOP_ALL_ACCESS,
            ctypes.byref(sa),
        )
        err = ctypes.get_last_error()
    finally:
        k32.LocalFree(sd)

    if not handle:
        _desktop_error = f"CreateDesktopW 失败 err={err}"
        logger.warning("ensure_desktop: %s", _desktop_error)
        return False, _desktop_error

    _desktop_handle = handle
    logger.info("独立桌面已创建: %s", win_token.DESKTOP_NAME)
    return True, ""


def desktop_ready() -> bool:
    return ensure_desktop()[0]


# ── 环境块 ──────────────────────────────────────────────────────

def build_env_block(env: dict[str, str]) -> bytes:
    """构造 `CREATE_UNICODE_ENVIRONMENT` 需要的环境块。

    格式：每个 `KEY=VALUE` 以 `\\0` 结尾，整块再以额外 `\\0` 收尾。
    大小写不敏感的重复键在这里去重（Windows 环境变量本身不区分大小写），
    否则子进程收到两套同名变量，行为取决于解析顺序。
    """
    seen: dict[str, str] = {}
    for key, value in env.items():
        seen[key.upper()] = f"{key}={value}"
    body = "\0".join(seen[k] for k in sorted(seen))
    return (body + "\0\0").encode("utf-16-le")


# ── 主入口 ──────────────────────────────────────────────────────

def launch_restricted(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float = 60.0,
    max_output: int = 1_000_000,
) -> LaunchResult:
    """以受限令牌 + 独立桌面启动 `argv`，等待其结束并取回输出。

    失败语义区分得很清楚：
    * `spawn_error` 非空 ⇒ 进程没起来（令牌/桌面/管道任一步失败）。调用方应据此
      **拒绝执行**，而不是退回无隔离路径 —— 那才是"沙箱不可用时拒绝启动"的兑现。
    * `exit_code` 非空 ⇒ 进程已结束，非 0 是子进程自己的事。

    超时后 `TerminateProcess` 直接终结，不等它优雅退出：受限进程可能卡在等输入上。
    """
    if not win.IS_WINDOWS:
        return LaunchResult(None, "", "", "not windows")
    if not argv:
        return LaunchResult(None, "", "", "empty argv")

    made = win_token.create_restricted_token()
    if made is None:
        return LaunchResult(None, "", "", "受限令牌无法创建")
    token, _sid = made

    ok, derr = ensure_desktop()
    if not ok:
        win_token._k32().CloseHandle(token)  # noqa: SLF001
        return LaunchResult(None, "", "", f"独立桌面不可用: {derr}")

    k32, adv = _k32(), _advapi()
    sa = _SECURITY_ATTRIBUTES(
        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
        lpSecurityDescriptor=None,
        bInheritHandle=True,  # 关键：可继承，否则子进程拿不到管道
    )
    out_r, out_w = wt.HANDLE(), wt.HANDLE()
    err_r, err_w = wt.HANDLE(), wt.HANDLE()
    pi = win_token._PROCESS_INFORMATION()  # noqa: SLF001
    env_block = build_env_block(env)
    env_buf = ctypes.create_string_buffer(env_block, len(env_block))
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

    try:
        if not k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), ctypes.byref(sa), 0):
            return LaunchResult(None, "", "", f"CreatePipe(stdout) 失败 err={ctypes.get_last_error()}")
        if not k32.CreatePipe(ctypes.byref(err_r), ctypes.byref(err_w), ctypes.byref(sa), 0):
            return LaunchResult(None, "", "", f"CreatePipe(stderr) 失败 err={ctypes.get_last_error()}")
        # 父端（读端）不该被子进程继承 —— 否则子进程持有读端，永不 EOF
        k32.SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0)
        k32.SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0)

        si = win_token._STARTUPINFOW()  # noqa: SLF001
        si.cb = ctypes.sizeof(win_token._STARTUPINFOW)  # noqa: SLF001
        si.dwFlags = STARTF_USESTDHANDLES
        # stdin 走空设备而非管道：受限进程不该拿到引擎的 stdin
        si.hStdInput = wt.HANDLE()
        si.hStdOutput = out_w
        si.hStdError = err_w
        si.lpDesktop = f"WinSta0\\{win_token.DESKTOP_NAME}"

        started = adv.CreateProcessAsUserW(
            token,
            None,
            ctypes.cast(cmdline, wt.LPWSTR),
            ctypes.byref(sa),
            None,
            True,  # bInheritHandles
            CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            ctypes.cast(env_buf, ctypes.c_void_p),
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not started:
            return LaunchResult(
                None, "", "", f"CreateProcessAsUserW 失败 err={ctypes.get_last_error()}"
            )

        # 子进程已持有写端，父进程必须立刻关掉，否则 ReadFile 等不到 EOF
        k32.CloseHandle(out_w)
        out_w = wt.HANDLE()
        k32.CloseHandle(err_w)
        err_w = wt.HANDLE()

        stdout = _drain(out_r, max_output)
        stderr = _drain(err_r, max_output)

        wait_ms = INFINITE if timeout <= 0 else int(timeout * 1000)
        waited = k32.WaitForSingleObject(pi.hProcess, wait_ms)
        if waited != WAIT_OBJECT_0:
            k32.TerminateProcess(pi.hProcess, 124)
            k32.WaitForSingleObject(pi.hProcess, 5000)
            return LaunchResult(124, stdout, stderr)

        code = wt.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return LaunchResult(int(code.value), stdout, stderr)
    except Exception as exc:  # noqa: BLE001 — 任何异常都按"起不来"处理
        logger.warning("launch_restricted 异常", exc_info=True)
        return LaunchResult(None, "", "", f"launch_restricted 异常: {exc}")
    finally:
        for handle in (out_w, err_w, out_r, err_r, pi.hProcess, pi.hThread, token):
            if handle:
                try:
                    k32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass


# ── AppContainer 执行层 ─────────────────────────────────────────

#: AppContainer 子进程**必须**具备的环境变量名。
#:
#: 缺任何一个都会让 `CreateProcessW` 直接失败并返回 `ERROR_ENVVAR_NOT_FOUND(203)`,
#: 而错误信息完全不指向环境块 —— 本机实测：只给 `SystemRoot/PATH/TEMP` 时稳定
#: `err=203`，补齐下面这组后立刻 `exit=0` 且 stdout 正常。
#:
#: 原因在 AppContainer 的进程初始化会解析"已知文件夹"与几个系统标识，缺了就建不起来。
#: 这些值本身不敏感（`C:\Windows`、计算机名、用户名），所以从宿主继承；
#: 真正带路径语义的用户目录另见 `appcontainer_env`，一律重定向进工作区。
_APPCONTAINER_SYSTEM_ENV = (
    "SYSTEMDRIVE",
    "WINDIR",
    "SYSTEMROOT",
    "ALLUSERSPROFILE",
    "PUBLIC",
    "COMPUTERNAME",
    "USERNAME",
    "OS",
    "NUMBER_OF_PROCESSORS",
)

#: 用户已知目录。AppContainer 的包 SID 默认对这些路径**没有**权限，
#: 指向宿主只会换来 `PermissionError`，还把宿主路径泄给模型，所以一律指进工作区。
_APPCONTAINER_USER_ENV = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME")


def appcontainer_env(env: dict[str, str], *, cwd: str) -> dict[str, str]:
    """补全 AppContainer 子进程的环境（在 `sandboxed_env()` 的结果之上）。

    两件事，缺一不可：

    1. **补齐系统变量** —— 否则 `CreateProcessW` 返回 `err=203`，进程根本起不来；
    2. **用户目录重定向进工作区** —— 包 SID 读不到宿主用户目录，指过去只会失败。
    """
    merged = dict(env)
    for key in _APPCONTAINER_SYSTEM_ENV:
        if not merged.get(key):
            host_value = os.environ.get(key)
            if host_value:
                merged[key] = host_value
    for key in _APPCONTAINER_USER_ENV:
        merged[key] = cwd
    # TEMP/TMP 必须在工作区内：包 SID 没有宿主临时目录的写权限
    merged["TEMP"] = cwd
    merged["TMP"] = cwd
    return merged


def _make_attribute_list(sid_string: str) -> tuple[Any, Any, str]:
    """建带包 SID 的 attribute list。返回 `(alist, psid, 错误)`。

    失败必须显式返回错误而不是抛异常：`resolve_level` 要据此**降级并说明原因**。
    调用方负责 `DeleteProcThreadAttributeList` 与 `LocalFree(psid)`。
    """
    k32, adv = _k32(), _advapi()
    psid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(sid_string, ctypes.byref(psid)):
        return None, None, f"ConvertStringSidToSidW 失败 err={ctypes.get_last_error()}"

    caps = _SECURITY_CAPABILITIES(
        # `CapabilityCount=0`：不授予任何受控能力（无网络、无多数目录）。
        AppContainerSid=ctypes.cast(psid, ctypes.c_void_p),
        Capabilities=None,
        CapabilityCount=0,
        Reserved=0,
    )

    size = ctypes.c_size_t(0)
    # 第一次调用只为拿所需大小；返回值必为 FALSE（缓冲区还没给）
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if size.value == 0:
        k32.LocalFree(psid)
        return None, None, "InitializeProcThreadAttributeList 未给出缓冲区大小"
    # 缓冲区必须**由本栈帧持有**到进程创建完成：attribute list 里存的是
    # `&caps` 指向的结构指针，`caps` 一离开作用域就是悬空指针。
    buf = ctypes.create_string_buffer(size.value)
    alist = ctypes.cast(buf, ctypes.c_void_p)
    if not k32.InitializeProcThreadAttributeList(alist, 1, 0, ctypes.byref(size)):
        k32.LocalFree(psid)
        return None, None, f"InitializeProcThreadAttributeList 失败 err={ctypes.get_last_error()}"
    if not k32.UpdateProcThreadAttribute(
        alist,
        0,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(caps),
        ctypes.sizeof(caps),
        None,
        None,
    ):
        err = ctypes.get_last_error()
        k32.DeleteProcThreadAttributeList(alist)
        k32.LocalFree(psid)
        return None, None, f"UpdateProcThreadAttribute 失败 err={err}"
    # 把 caps 与 psid 一起挂到 buffer 上，保证生命周期与 alist 一致
    buf._caps = caps  # type: ignore[attr-defined]
    buf._psid = psid  # type: ignore[attr-defined]
    return alist, psid, ""


def _spawn_appcontainer(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    with_stdin: bool,
) -> tuple[_Spawned | None, str]:
    """在 AppContainer 里创建进程。成功返回句柄集合，失败返回具体原因。

    **为什么必须是 `CreateProcessW` 而不是 `CreateProcessAsUserW`**：
    AppContainer 靠 `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 指定包 SID，
    不需要另一个用户令牌，因此普通（非提权）账户就能用 —— 这正是它比
    `restricted` 更可达的原因（后者要 `SeIncreaseQuotaPrivilege`）。
    """
    if not win.IS_WINDOWS:
        return None, "not windows"
    if not argv:
        return None, "empty argv"

    ok, sid, err = win.ensure_appcontainer(win.appcontainer_name("default"))
    if not ok:
        return None, f"AppContainer profile 创建失败: {err}"
    granted, gerr = win.grant_appcontainer_path(sid, cwd)
    if not granted:
        return None, f"工作区授权给包 SID 失败: {gerr}"

    alist, psid, aerr = _make_attribute_list(sid)
    if alist is None:
        return None, aerr

    k32 = _k32()
    sa = _SECURITY_ATTRIBUTES(
        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
        lpSecurityDescriptor=None,
        bInheritHandle=True,
    )
    out_r, out_w = wt.HANDLE(), wt.HANDLE()
    err_r, err_w = wt.HANDLE(), wt.HANDLE()
    in_r, in_w = wt.HANDLE(), wt.HANDLE()
    pi = win_token._PROCESS_INFORMATION()  # noqa: SLF001
    env_block = build_env_block(env)
    env_buf = ctypes.create_string_buffer(env_block, len(env_block))
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

    try:
        for label, pair in (("stdout", (out_r, out_w)), ("stderr", (err_r, err_w))):
            if not k32.CreatePipe(
                ctypes.byref(pair[0]), ctypes.byref(pair[1]), ctypes.byref(sa), 0
            ):
                return None, f"CreatePipe({label}) 失败 err={ctypes.get_last_error()}"
        if with_stdin and not k32.CreatePipe(
            ctypes.byref(in_r), ctypes.byref(in_w), ctypes.byref(sa), 0
        ):
            return None, f"CreatePipe(stdin) 失败 err={ctypes.get_last_error()}"
        # 父端不可继承：读端被继承会让父进程等不到 EOF，写端被继承会让 shell 等不到 EOF
        for handle in (out_r, err_r, in_w):
            if handle:
                k32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0)

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        # 一次性执行不接 stdin：给空句柄即"没有输入"，避免子进程读到引擎的 stdin
        si.StartupInfo.hStdInput = in_r if with_stdin else wt.HANDLE()
        si.StartupInfo.hStdOutput = out_w
        si.StartupInfo.hStdError = err_w
        si.lpAttributeList = alist

        started = k32.CreateProcessW(
            None,
            ctypes.cast(cmdline, wt.LPWSTR),
            ctypes.byref(sa),
            None,
            True,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            ctypes.cast(env_buf, ctypes.c_void_p),
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not started:
            return None, f"CreateProcessW(AppContainer) 失败 err={ctypes.get_last_error()}"
    except Exception as exc:  # noqa: BLE001 — 任何异常都按"起不来"处理
        logger.warning("_spawn_appcontainer 异常", exc_info=True)
        return None, f"_spawn_appcontainer 异常: {exc}"
    finally:
        k32.DeleteProcThreadAttributeList(alist)
        if psid:
            k32.LocalFree(psid)
        # 子进程已持有自己的副本，父进程立刻关掉这些，否则读不到 EOF
        for handle in (out_w, err_w, in_r):
            if handle:
                try:
                    k32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass

    return (
        _Spawned(
            pi=pi,
            out_r=out_r,
            err_r=err_r,
            stdin_write=in_w if with_stdin else None,
            k32=k32,
        ),
        "",
    )


def launch_appcontainer(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float = 60.0,
    max_output: int = 1_000_000,
) -> LaunchResult:
    """在 AppContainer 里执行一条命令，等它结束并取回输出。

    失败语义与 `launch_restricted` 一致：`spawn_error` 非空 ⇒ 进程没起来，
    调用方应当**拒绝执行**，不得退回无隔离路径。
    """
    spawned, serr = _spawn_appcontainer(
        argv, cwd=cwd, env=appcontainer_env(env, cwd=cwd), with_stdin=False
    )
    if spawned is None:
        return LaunchResult(None, "", "", serr)

    k32, pi = spawned.k32, spawned.pi
    try:
        stdout = _drain(spawned.out_r, max_output)
        stderr = _drain(spawned.err_r, max_output)
        wait_ms = INFINITE if timeout <= 0 else int(timeout * 1000)
        if k32.WaitForSingleObject(pi.hProcess, wait_ms) != WAIT_OBJECT_0:
            k32.TerminateProcess(pi.hProcess, 124)
            k32.WaitForSingleObject(pi.hProcess, 5000)
            return LaunchResult(124, stdout, stderr)
        code = wt.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return LaunchResult(int(code.value), stdout, stderr)
    finally:
        for handle in (spawned.out_r, spawned.err_r, pi.hProcess, pi.hThread):
            if handle:
                try:
                    k32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass


def open_appcontainer_session(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
) -> tuple[RestrictedSession | None, str]:
    """在 AppContainer 里起一个**常驻**进程（复用 `RestrictedSession` 的管道泵）。

    常驻模式是持久 shell 能工作的前提：受限档下 `launch_restricted` 无法
    "常驻 + 增量写 stdin"，`open_restricted_session` 补上了这个缺口；
    AppContainer 档走同一套会话结构，只是建进程的方式不同。
    """
    spawned, serr = _spawn_appcontainer(
        argv, cwd=cwd, env=appcontainer_env(env, cwd=cwd), with_stdin=True
    )
    if spawned is None:
        return None, serr

    session = RestrictedSession(
        pid=int(spawned.pi.dwProcessId),
        hprocess=spawned.pi.hProcess,
        hthread=spawned.pi.hThread,
        stdin_write=spawned.stdin_write,
        k32=spawned.k32,
    )
    # 不排空的话 4KB 管道缓冲一满子进程就卡在 write 上（表现为"命令莫名挂住"）
    for handle, sink in (
        (spawned.out_r, session._out_chunks),  # noqa: SLF001
        (spawned.err_r, session._err_chunks),  # noqa: SLF001
    ):
        threading.Thread(
            target=session._pump,  # noqa: SLF001
            args=(handle, sink),
            daemon=True,
            name=f"appcontainer-pump-{session.pid}",
        ).start()
    logger.info("AppContainer 常驻进程已启动: pid=%s", session.pid)
    return session, ""


def appcontainer_smoke_test() -> tuple[bool, str]:
    """端到端验证 AppContainer 真能起进程并拿回输出（`exec_ready` 的判定依据）。

    与 `smoke_test` 同样用 `cmd.exe` 的 `echo`：不依赖工作区可执行权限，
    让"起不来"只可能是机制本身的问题。

    **已验证的能力边界**（本机零提权实测，务必别当成"支持一切外挂程序"）：

    * `cmd.exe` / 原生命令：可用，stdout/stderr/退出码都正常；
    * `python.exe`：**不可用** —— `exit=3221225781`（`0xC0000135`，DLL 加载失败）。
      包 SID 读不到 CPython 安装目录，而给该目录授权（`grant_appcontainer_path`）
      会因安装目录的继承 ACL 而失败（`icacls` 返回 5 个失败文件）。所以
      `run_code` 的 Python worker **不能**走这一档，它由
      `child_applicable_level` 如实折算到 `low_il`（仍高于硬底线）。

    因此冒烟只证明"执行层可用"，不证明"任意程序都能在包里跑"。
    """
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or r"C:\Windows\System32\cmd.exe"
    workdir = os.path.join(
        os.environ.get("TEMP", r"C:\Windows\Temp"), "chiron-appcontainer-probe"
    )
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建探测目录: {exc}"

    result = launch_appcontainer(
        [comspec, "/d", "/c", "echo ok&&echo bad 1>&2"],
        cwd=workdir,
        env={"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")},
        timeout=20,
    )
    if not result.started:
        return False, result.spawn_error
    if result.exit_code != 0:
        return False, f"exit={result.exit_code} stderr={result.stderr.strip()[:200]}"
    if "ok" not in result.stdout:
        return False, f"stdout 未收到预期输出: {result.stdout!r}"
    return True, ""


_appcontainer_ok: bool | None = None
_appcontainer_error = ""


def appcontainer_exec_ready() -> bool:
    """AppContainer 执行层是否可用（带缓存）。失败原因见 `appcontainer_exec_error()`。"""
    global _appcontainer_ok, _appcontainer_error
    if _appcontainer_ok is None:
        ok, err = appcontainer_smoke_test()
        _appcontainer_ok = ok
        _appcontainer_error = err
        if not ok:
            logger.warning("appcontainer 执行层冒烟测试失败: %s", err)
    return bool(_appcontainer_ok)


def appcontainer_exec_error() -> str:
    return _appcontainer_error


def _drain(handle: Any, max_output: int) -> str:
    """同步读完一根管道。读端已被父进程独占，所以阻塞到 EOF 是安全的。"""
    chunks: list[bytes] = []
    total = 0
    buf = ctypes.create_string_buffer(65536)
    read = wt.DWORD()
    while total < max_output:
        if not _k32().ReadFile(
            handle, buf, len(buf), ctypes.byref(read), None
        ):
            break
        if read.value == 0:
            break
        chunks.append(buf.raw[: read.value])
        total += read.value
    return b"".join(chunks).decode("utf-8", errors="replace")


# ── 常驻模式：长驻 shell 的受限启动器 ──────────────────────────
#
# `launch_restricted` 是**一次性**模型（起进程 → 读完两根管道 → 等退出），
# 长驻 shell 需要的是相反的东西：进程常驻、stdin 可增量写入、输出持续可得。
#
# 两者共用同一套令牌/桌面/管道机制，差别只在这三点：
#   1. 额外建一根 stdin 管道（一次性模型用空设备，因为受限进程不该读引擎的 stdin）；
#   2. 不阻塞等待退出，交给调用方轮询；
#   3. stdout/stderr 必须**后台持续排空** —— 不排空的话管道缓冲区（默认 4KB）填满后
#      子进程会卡在 write 上，表现为"命令莫名挂住"。
#
# 排空用线程而非 PeekNamedPipe 轮询：ReadFile 阻塞在网络/文件无关的管道上，
# 线程里等是零成本的；轮询则要么空转烧 CPU，要么延迟可感知。


class RestrictedStdin:
    """`RestrictedSession` 的 stdin 适配层，形似 `asyncio.StreamWriter` 的最小子集。

    长驻 shell 的既有代码写的是 `proc.stdin.write(...)` + `await proc.stdin.drain()`；
    提供同样两个方法，`terminal.py` 就不必为受限档写第二套调用序列 ——
    两套序列一旦分叉，日后改动只会修到其中一条。
    """

    def __init__(self, session: RestrictedSession) -> None:
        self._session = session

    def write(self, data: bytes | str) -> None:
        # `run_code` 的 worker IPC 用 `text=True` 语义写 `str`，长驻 shell 写 `bytes`；
        # 两条调用方都复用本方法，所以在这里统一编码，而不是让调用方各写一份。
        payload = data.encode("utf-8") if isinstance(data, str) else data
        written = self._session.write(payload)
        if written <= 0:
            raise BrokenPipeError("受限进程 stdin 已关闭")

    async def drain(self) -> None:
        # `write()` 内部是同步 WriteFile，写完即已落到管道里；没有额外缓冲要冲刷。
        return None

    def flush(self) -> None:
        """`subprocess.Popen.stdin.flush()` 的对齐方法（同步实现下是 no-op）。

        `run_code` 的 worker IPC 写的是 `write()` + `flush()`；缺这个方法，
        受限档会在第一条 init 消息上就 `AttributeError` —— 而且是在子线程里，
        表现为"worker 起不来但没报错"。
        """
        return None


class RestrictedSession:
    """一个**常驻**受限进程：受限令牌 + 独立桌面 + 三根可继承管道。

    生命周期由调用方掌握；`close()` 之后所有句柄释放，重复调用安全。
    """

    def __init__(
        self,
        *,
        pid: int,
        hprocess: Any,
        hthread: Any,
        stdin_write: Any,
        k32: Any,
    ) -> None:
        self.pid = pid
        self._hprocess = hprocess
        self._hthread = hthread
        self._stdin_write = stdin_write
        self._k32 = k32
        self._exit_code: int | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._out_chunks: list[bytes] = []
        self._err_chunks: list[bytes] = []
        self.stdin = RestrictedStdin(self)

    # ── 输出 ──
    def _pump(self, handle: Any, sink: list[bytes]) -> None:
        """后台线程：把管道排空到内存，直到 EOF 或句柄失效。"""
        buf = ctypes.create_string_buffer(65536)
        read = wt.DWORD()
        # 用累计计数而非每次 sum()：长驻 shell 的输出 chunk 可能有上万个，
        # 每轮重算总和会把排空线程变成 O(n²)，恰好卡住它该保护的对象。
        kept = 0
        while True:
            ok = self._k32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
            if not ok or read.value == 0:
                break
            chunk = buf.raw[: read.value]
            with self._lock:
                # 上限防止失控输出吃光内存；超限后继续读但不保留（只为不阻塞子进程）
                if kept < _PERSISTENT_OUTPUT_CAP:
                    sink.append(chunk)
                    kept += len(chunk)
        try:
            self._k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass

    def take_stream(self, which: str) -> str:
        """取走**单一**流的新增输出（`"out"` / `"err"`）。

        `take_output()` 一次取两根流，对"读完 stdout 再读 stderr"的调用方不成立 ——
        先取的那次会把另一根的内容也吞掉，于是行缓冲里丢消息。行式 IPC 必须按流取。
        """
        with self._lock:
            sink = self._out_chunks if which == "out" else self._err_chunks
            data = b"".join(sink)
            sink.clear()
        return data.decode("utf-8", errors="replace")

    def take_output(self) -> tuple[str, str]:
        """取走自上次调用以来的新增输出（增量语义，取完清空）。"""
        with self._lock:
            out = b"".join(self._out_chunks)
            err = b"".join(self._err_chunks)
            self._out_chunks.clear()
            self._err_chunks.clear()
        return out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")

    # ── stdin ──
    def write(self, data: bytes) -> int:
        """同步写入 stdin，返回实际写入字节数（0 表示管道已断）。"""
        if self._closed or not self._stdin_write:
            return 0
        written = wt.DWORD()
        ok = self._k32.WriteFile(
            self._stdin_write,
            data,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            return 0
        return int(written.value)

    # ── 状态 ──
    def poll(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        # 0 超时 = 不等待，立刻返回状态
        if self._k32.WaitForSingleObject(self._hprocess, 0) != WAIT_OBJECT_0:
            return None
        code = wt.DWORD()
        if self._k32.GetExitCodeProcess(self._hprocess, ctypes.byref(code)):
            self._exit_code = int(code.value)
        else:
            self._exit_code = -1
        return self._exit_code

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def terminate(self, code: int = 1) -> None:
        """直接终结。受限进程可能卡在等输入上，不指望它优雅退出。"""
        if self.poll() is None:
            try:
                self._k32.TerminateProcess(self._hprocess, code)
            except Exception:  # noqa: BLE001
                pass
        self._exit_code = code if self._exit_code is None else self._exit_code

    def kill(self) -> None:
        """`asyncio.subprocess.Process.kill()` 的对齐方法。

        `terminal.py` 只调 `kill()` / `await wait()`；提供同名同义的方法，
        长驻 shell 就不必为受限档写第二套清理序列 —— 两套序列一旦分叉，
        日后修资源泄漏只会修到其中一条。
        """
        self.terminate(1)

    async def wait(self, timeout: float = 5.0) -> int:
        """轮询等退出（不阻塞事件循环）。超时则终结。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = self.poll()
            if code is not None:
                return code
            await asyncio.sleep(0.02)
        self.terminate(124)
        return self._exit_code if self._exit_code is not None else 124

    def wait_sync(self, timeout: float | None = None) -> int:
        """同步版本，供在 executor 线程里跑的消费方（`run_code`）使用。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                self.terminate(124)
                return self._exit_code if self._exit_code is not None else 124
            time.sleep(0.02)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handle in (self._stdin_write, self._hprocess, self._hthread):
            if handle:
                try:
                    self._k32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
        self._stdin_write = wt.HANDLE()
        self._hprocess = wt.HANDLE()
        self._hthread = wt.HANDLE()


#: 每个会话保留的输出上限（超限后仍继续排空，只是不再累积）
_PERSISTENT_OUTPUT_CAP = 4 * 1024 * 1024


def open_restricted_session(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
) -> tuple[RestrictedSession | None, str]:
    """以受限令牌起一个**常驻**进程。成功返回 `(session, "")`。

    失败返回 `(None, 原因)` —— 调用方必须据此拒绝执行，不得退回无隔离路径。
    """
    if not win.IS_WINDOWS:
        return None, "not windows"
    if not argv:
        return None, "empty argv"

    made = win_token.create_restricted_token()
    if made is None:
        return None, "受限令牌无法创建"
    token, _sid = made

    ok, derr = ensure_desktop()
    if not ok:
        win_token._k32().CloseHandle(token)  # noqa: SLF001
        return None, f"独立桌面不可用: {derr}"

    k32, adv = _k32(), _advapi()
    sa = _SECURITY_ATTRIBUTES(
        nLength=ctypes.sizeof(_SECURITY_ATTRIBUTES),
        lpSecurityDescriptor=None,
        bInheritHandle=True,
    )
    in_r, in_w = wt.HANDLE(), wt.HANDLE()
    out_r, out_w = wt.HANDLE(), wt.HANDLE()
    err_r, err_w = wt.HANDLE(), wt.HANDLE()
    pi = win_token._PROCESS_INFORMATION()  # noqa: SLF001
    env_block = build_env_block(env)
    env_buf = ctypes.create_string_buffer(env_block, len(env_block))
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

    try:
        for name, pair in (("stdin", (in_r, in_w)), ("stdout", (out_r, out_w)), ("stderr", (err_r, err_w))):
            if not k32.CreatePipe(
                ctypes.byref(pair[0]), ctypes.byref(pair[1]), ctypes.byref(sa), 0
            ):
                return None, f"CreatePipe({name}) 失败 err={ctypes.get_last_error()}"
        # 父端句柄不能被子进程继承：
        # * stdin **写**端若被继承，shell 永远等不到 EOF，某些命令会一直读下去；
        # * stdout/stderr **读**端若被继承，父进程同样等不到 EOF。
        for h in (in_w, out_r, err_r):
            k32.SetHandleInformation(h, HANDLE_FLAG_INHERIT, 0)

        si = win_token._STARTUPINFOW()  # noqa: SLF001
        si.cb = ctypes.sizeof(win_token._STARTUPINFOW)  # noqa: SLF001
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput = in_r
        si.hStdOutput = out_w
        si.hStdError = err_w
        si.lpDesktop = f"WinSta0\\{win_token.DESKTOP_NAME}"

        started = adv.CreateProcessAsUserW(
            token,
            None,
            ctypes.cast(cmdline, wt.LPWSTR),
            ctypes.byref(sa),
            None,
            True,  # bInheritHandles
            CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            ctypes.cast(env_buf, ctypes.c_void_p),
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not started:
            return None, f"CreateProcessAsUserW 失败 err={ctypes.get_last_error()}"
    except Exception as exc:  # noqa: BLE001 — 任何异常都按"起不来"处理
        logger.warning("open_restricted_session 异常", exc_info=True)
        return None, f"open_restricted_session 异常: {exc}"
    finally:
        # 令牌用完即关：进程已持有自己的令牌副本，父进程不再需要
        try:
            k32.CloseHandle(token)
        except Exception:  # noqa: BLE001
            pass
        # 子进程端句柄父进程必须关掉，否则等不到 EOF
        for h in (in_r, out_w, err_w):
            if h:
                try:
                    k32.CloseHandle(h)
                except Exception:  # noqa: BLE001
                    pass

    session = RestrictedSession(
        pid=int(pi.dwProcessId),
        hprocess=pi.hProcess,
        hthread=pi.hThread,
        stdin_write=in_w,
        k32=k32,
    )
    # stdout/stderr 各起一个排空线程：这是长驻模式能工作的前提，
    # 不排空的话 4KB 管道缓冲一满，子进程就卡在 write 上（表现为命令"莫名挂住"）。
    for handle, sink in ((out_r, session._out_chunks), (err_r, session._err_chunks)):  # noqa: SLF001
        threading.Thread(
            target=session._pump,  # noqa: SLF001
            args=(handle, sink),
            daemon=True,
            name=f"restricted-pump-{session.pid}",
        ).start()

    logger.info("受限常驻进程已启动: pid=%s", session.pid)
    return session, ""



# ── 冒烟测试：`EXEC_READY` 的判定依据 ──────────────────────────

def smoke_test() -> tuple[bool, str]:
    """端到端验证：真的用受限令牌起一个进程并拿回它的输出。

    只验证"令牌造得出来"是不够的 —— 令牌、桌面 DACL、管道继承、环境块
    任何一环错了，进程都起不来或读不到输出。这里让子进程**同时**写 stdout
    和 stderr，确认两根管道都通。
    """
    # 用 cmd.exe 内置的 echo：不依赖工作区权限，也不依赖任何外部可执行文件，
    # 这样"起不来"只可能是令牌/桌面/管道的问题，不会混进路径或权限噪音。
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    result = launch_restricted(
        [comspec, "/d", "/c", "echo ok&&echo bad 1>&2"],
        cwd=r"C:\Windows",
        env={"PATH": r"C:\Windows\System32", "SystemRoot": r"C:\Windows"},
        timeout=20,
    )
    if not result.started:
        return False, result.spawn_error
    if result.exit_code != 0:
        return False, f"exit={result.exit_code} stderr={result.stderr.strip()[:200]}"
    if "ok" not in result.stdout:
        return False, f"stdout 未收到预期输出: {result.stdout!r}"
    return True, ""


_smoke_ok: bool | None = None
_smoke_error = ""

#: TokenElevation = 20
_TOKEN_ELEVATION = 20


def is_admin() -> bool:
    """当前进程是否以**提权**令牌运行（UAC 已同意）。

    这是 `restricted` 档能否启用的**前置条件**，不是充分条件：
    `CreateProcessAsUserW` 需要 `SeIncreaseQuotaPrivilege`，该特权只授予提权令牌。
    实测在普通（非提权）账户上：`CreateProcessAsUserW` → `err=5`，
    退路 `CreateProcessWithTokenW` → `err=1314`（缺 `SeImpersonatePrivilege`）。

    单独暴露这个判定，是为了让降级提示能说"引擎未以管理员身份运行"，
    而不是抛一个需要查 Win32 错误码才知道含义的 `err=5`。
    """
    if not win.IS_WINDOWS:
        return False
    try:
        k32 = win_token._k32()  # noqa: SLF001 — 已配置 OpenProcessToken 的 argtypes
        adv = win_token._advapi()  # noqa: SLF001
        token = wt.HANDLE()
        if not k32.OpenProcessToken(
            k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
        ):
            return False
        try:
            elevated = wt.DWORD()
            size = wt.DWORD()
            if not adv.GetTokenInformation(
                token,
                _TOKEN_ELEVATION,
                ctypes.byref(elevated),
                ctypes.sizeof(elevated),
                ctypes.byref(size),
            ):
                return False
            return bool(elevated.value)
        finally:
            k32.CloseHandle(token)
    except Exception:  # noqa: BLE001 — 判定失败按"未提权"处理（fail-closed）
        return False

def exec_ready() -> bool:
    """本机是否真能把子进程以受限令牌拉起（带缓存，失败原因可从 `exec_error()` 取）。"""
    global _smoke_ok, _smoke_error
    if _smoke_ok is None:
        ok, err = smoke_test()
        _smoke_ok = ok
        _smoke_error = err
        if not ok:
            logger.warning("restricted 执行层冒烟测试失败: %s", err)
    return bool(_smoke_ok)


def exec_error() -> str:
    return _smoke_error


def reset_probe() -> None:
    """清缓存，供测试与运行时切换档位后重新探测。"""
    global _smoke_ok, _smoke_error, _desktop_handle, _desktop_error
    global _appcontainer_ok, _appcontainer_error
    _smoke_ok = None
    _smoke_error = ""
    _desktop_handle = None
    _desktop_error = ""
    # AppContainer 的探测结果同样要清：重新探测的前提是"上次的结论不再可信"。
    _appcontainer_ok = None
    _appcontainer_error = ""


__all__ = [
    "LaunchResult",
    "RestrictedSession",
    "appcontainer_env",
    "appcontainer_exec_error",
    "appcontainer_exec_ready",
    "appcontainer_smoke_test",
    "build_env_block",
    "desktop_ready",
    "ensure_desktop",
    "exec_error",
    "exec_ready",
    "is_admin",
    "launch_appcontainer",
    "launch_restricted",
    "open_appcontainer_session",
    "open_restricted_session",
    "reset_probe",
    "smoke_test",
]
