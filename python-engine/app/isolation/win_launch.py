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
    """`CreateDesktopW` / `OpenDesktopW` / `CloseDesktop` 属于 **user32**，不在 kernel32。

    这个归属很容易记错（桌面听起来像内核对象），实测 `kernel32.CreateDesktopW`
    会直接 `AttributeError: function not found`。

    **必须显式声明 `restype = wt.HANDLE`**：ctypes 默认返回 `c_int`，在 64 位下
    句柄会被截断成 32 位，后续用这个残缺句柄去建进程只会拿到莫名其妙的失败
    （`CreateProcessAsUserW` 的 `lpDesktop` 打不开）。
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
    # `OpenDesktopW(lpszDesktop, dwFlags, fInherit, dwDesiredAccess)`
    lib.OpenDesktopW.restype = wt.HANDLE
    lib.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]
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

#: 桌面 DACL 模板：**当前用户** / 合成 SID / SYSTEM / Administrators 完全控制。
#:
#: 三件事缺一不可，每一件都对应一次实测失败：
#:
#: 1. **当前用户 SID** —— 没有它，连创建者自己都打不开这个桌面。
#:    实测：`D:(A;;GA;;;S-1-0-0)(A;;GA;;;SY)(A;;GA;;;BA)` 下，
#:    `CreateDesktopW(新名字)` 成功但随后 `OpenDesktopW(同名)` 恒 `err=5`；
#:    把当前用户 SID 加进 DACL 后立刻 `err=0`。
#:    这是"多实例 / 残留桌面"场景的**唯一**错误来源，此前被误归因为
#:    "缺 `SeIncreaseQuotaPrivilege`"，于是用户去提权 —— 而提权只会换一个
#:    同样打不开的身份（UAC 下 `BUILTIN\\Administrators` 是 deny-only 组）。
#: 2. **合成 SID `S-1-0-0`** —— 受限令牌带 `WRITE_RESTRICTED`，做**写**访问检查时
#:    必须由 restricting SID 之一命中 DACL。`create_restricted_token()` 把
#:    `WinNullSid` 放进 `SidsToRestrict`，所以这条 ACE 是受限子进程能打开桌面的前提。
#:    注意它**不能**代替第 1 条：NULL SID 不匹配普通（未受限）主体。
#: 3. **SY / BA** —— 服务账户与管理员会话的正常路径。
_DESKTOP_SDDL_TEMPLATE = "D:(A;;GA;;;{user})(A;;GA;;;S-1-0-0)(A;;GA;;;SY)(A;;GA;;;BA)"

_desktop_handle: Any = None
_desktop_error: str = ""
#: 实际使用的桌面名。多实例场景下可能不是 `win_token.DESKTOP_NAME`（见 `ensure_desktop`）。
_desktop_name: str = win_token.DESKTOP_NAME


def _desktop_sddl() -> str:
    """按当前用户构造桌面 DACL。取不到用户 SID 返回空串（宁可失败也不要无权桌面）。"""
    user = win_token.current_user_sid()
    if not user:
        return ""
    return _DESKTOP_SDDL_TEMPLATE.format(user=user)


def _create_or_open_desktop(name: str, sddl: str) -> tuple[Any, str]:
    """创建桌面；同名桌面已存在时改为打开它。

    返回 `(handle, 错误)`。`handle` 为 `None` 时 `错误` 非空。

    为什么要"创建失败再打开"：桌面名在**窗口站**范围内全局唯一。引擎多实例
    （或旧进程留下的同名桌面）时 `CreateDesktopW` 会因 `ERROR_ACCESS_DENIED(5)`
    失败，而那份桌面可能完全可用 —— 直接判"机制不可用"等于让多个实例互相排斥。
    """
    k32, adv, u32 = _k32(), _advapi(), _user32()
    sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(sd), None
    ):
        return None, f"SDDL 转换失败 err={ctypes.get_last_error()}"

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
        handle = u32.CreateDesktopW(name, None, None, 0, DESKTOP_ALL_ACCESS, ctypes.byref(sa))
        err = ctypes.get_last_error()
    finally:
        k32.LocalFree(sd)

    if handle:
        return handle, ""

    # 已存在但当前令牌打不开：先试打开（DACL 合规的同名桌面可被多实例共享），
    # 仍失败则把两个错误都带上 —— 只说 CreateDesktopW 的 err 会掩盖"桌面残留"
    # 这个真正的原因。
    create_err = err
    ctypes.set_last_error(0)
    handle = u32.OpenDesktopW(name, 0, False, DESKTOP_ALL_ACCESS)
    open_err = ctypes.get_last_error()
    if handle:
        logger.info("复用已存在的独立桌面: %s", name)
        return handle, ""
    return None, (
        f"桌面 {name} 既无法创建（err={create_err}）也无法打开（err={open_err}）；"
        "多半是旧进程残留了同名桌面且 DACL 不含当前用户，重启该进程后再试"
    )


def ensure_desktop() -> tuple[bool, str]:
    """创建（或复用）独立桌面 `ChironSandbox`。成功返回 `(True, "")`。

    句柄在本进程内缓存并不主动关闭：桌面在**创建它的进程退出时**自动销毁，
    这正是我们要的语义（引擎重启 ⇒ 桌面重建，不残留）。

    名字被占用时依次退回 `<名字>-<pid>`：窗口站内桌面名唯一，而"创建失败"
    并不等于"不可用"——
      · 同名桌面已存在且 DACL 含当前用户 ⇒ `_create_or_open_desktop` 直接打开它；
      · DACL 不含当前用户（旧版本残留）⇒ 打不开，此时换一个带 pid 的名字新建，
        而不是让整个 `restricted` 档对本进程永久不可用。
    """
    global _desktop_handle, _desktop_error, _desktop_name
    if not win.IS_WINDOWS:
        return False, "not windows"
    if _desktop_handle:
        return True, ""
    if _desktop_error:
        return False, _desktop_error

    sddl = _desktop_sddl()
    if not sddl:
        _desktop_error = "无法取得当前用户 SID，拒绝创建权限不明的桌面"
        logger.warning("ensure_desktop: %s", _desktop_error)
        return False, _desktop_error

    base = win_token.DESKTOP_NAME
    candidates = [base, f"{base}-{os.getpid()}"]
    errors: list[str] = []
    first_error = ""
    for name in candidates:
        handle, err = _create_or_open_desktop(name, sddl)
        if handle:
            _desktop_handle = handle
            _desktop_name = name
            if first_error:
                logger.warning(
                    "独立桌面 %s 不可用（%s），改用 %s", base, first_error, name
                )
            logger.info("独立桌面已就绪: %s", name)
            return True, ""
        errors.append(err)
        if not first_error:
            first_error = err

    _desktop_error = "；".join(errors)
    logger.warning("ensure_desktop: %s", _desktop_error)
    return False, _desktop_error


def desktop_name() -> str:
    """当前**实际**使用的桌面名（多实例时可能带 pid 后缀）。"""
    return _desktop_name


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

    ## 两条执行路径

    1. **本进程已提权**（用户以管理员运行引擎，或者**调用方就是提权 helper 自己**）：
       直接走 `_launch_restricted_local`。
    2. **本进程未提权**：交给提权 helper（`CreateProcessAsUserW` 需要
       `SeIncreaseQuotaPrivilege`，只有提权令牌才有）。引擎自己不提权（它承载
       模型输出，爆炸半径最大），所以这才是桌面端的正常路径。

    顺序**必须**是"先判提权、再考虑 helper"，不能反过来先试 helper：

    提权 helper 收到 `/v1/exec` 后调用的正是本函数。若这里仍优先走 helper，
    而 helper 自己 `is_admin()` 为真、`helper_available()` 又会 ping 到**自己**
    （端口文件是它写的），于是它会向自己发一条 `/v1/exec` —— 无限递归，
    每层多一个线程、每层等一个超时，请求永不返回。表现是"助手启用成功，
    档位却永远不可用"，同时在用户机上泄漏大量阻塞线程。
    `tests/test_isolation_restricted_enable.py` 的
    `test_elevated_process_never_goes_through_the_helper` 钉住这个顺序。

    两条路径都失败时才返回 `spawn_error`。**绝不**退回无隔离执行 ——
    `spawn_error` 非空就是"拒绝执行"。

    失败语义区分得很清楚：
    * `spawn_error` 非空 ⇒ 进程没起来（令牌/桌面/管道任一步失败）。调用方应据此
      **拒绝执行**，而不是退回无隔离路径 —— 那才是"沙箱不可用时拒绝启动"的兑现。
    * `exit_code` 非空 ⇒ 进程已结束，非 0 是子进程自己的事。

    超时后 `TerminateProcess` 直接终结，不等它优雅退出：受限进程可能卡在等输入上。
    """
    if is_admin():
        # 本进程已有 SeIncreaseQuotaPrivilege：多绕一跳 helper 只会更慢，
        # 而且在 helper 进程内绕回自己会成环（见上面的说明）。
        return _launch_restricted_local(
            argv, cwd=cwd, env=env, timeout=timeout, max_output=max_output
        )

    helper_result = _via_helper(argv, cwd=cwd, env=env, timeout=timeout, max_output=max_output)
    if helper_result is not None:
        # helper 已给出结论（成功或真实失败）。未提权时没有第二条路可走，
        # 如实返回让调用方按 fail-closed 处理。
        return helper_result

    # helper 这条路不适用（未启动 / 未提权 / 客户端不可用）时才落到本地：
    # 非提权下这里会稳定 err=5，但那条错误串带 Win32 细节，比"helper 不可用"
    # 更能说明"到底是谁没做成"。
    return _launch_restricted_local(
        argv, cwd=cwd, env=env, timeout=timeout, max_output=max_output
    )


def _via_helper(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_output: int,
) -> LaunchResult | None:
    """交给提权 helper 执行。helper 不可用时返回 `None`（表示"这条路不适用"）。

    这里把"不可用"与"执行失败"分开：前者该让调用方另寻路径（本进程提权），
    后者是一次真实失败，不该被悄悄重试掉。
    """
    ok, reason = helper_available()
    if not ok:
        logger.debug("restricted 不走 helper: %s", reason)
        return None
    client = _helper_client()
    if client is None:
        return None
    return client.helper_exec(
        argv, cwd=cwd, env=env, timeout=timeout, max_output=max_output
    )


def helper_available() -> tuple[bool, str]:
    """提权 helper 是否可用于执行 `restricted`。返回 `(ok, 原因)`。"""
    client = _helper_client()
    if client is None:
        return False, "helper 客户端不可用"
    try:
        return client.available()
    except Exception as exc:  # noqa: BLE001 — 探测异常按"不可用"（fail-closed）
        return False, f"helper 探测异常: {type(exc).__name__}: {exc}"


def _helper_client() -> Any:
    """延迟取得 helper 客户端。

    `elevated_client` 顶层 `from app.isolation.win_launch import LaunchResult`，
    在模块顶部导入会成环，所以只能在这里取。
    """
    try:
        from app.isolation import elevated_client

        return elevated_client
    except Exception as exc:  # noqa: BLE001
        logger.debug("helper 客户端导入失败: %s", exc)
        return None


def helper_start(parent_pid: int | None = None) -> tuple[bool, str, bool]:
    """以管理员权限启动提权 helper，等它就绪。返回 `(ok, 原因, 是否用户取消)`。

    **只能由用户显式操作触发**（设置页按钮 → 引擎端点）。引擎启动时静默弹 UAC
    是不可接受的：用户不知道自己在同意什么，而同意的是"提权执行通道"。

    这里把"启动"与"等就绪"合成一步：调用方（前端）只关心"现在能不能用"。
    UAC 同意后 helper 还要绑端口、写端口文件，中间有一段不可用窗口；若只返回
    "UAC 已同意"，前端会立刻把档位点亮，而实际执行还在撞 `err=5`。

    第三个返回值一路透传到界面，让"你取消了 UAC"与"真的起不来"分开呈现：
    前者不需要排障，后者需要看 `reason`。
    """
    client = _helper_client()
    if client is None:
        return False, "helper 客户端不可用", False

    ok, reason, cancelled = client.spawn_elevated(parent_pid)
    if not ok:
        return False, reason, cancelled

    ready, wait_reason = client.wait_ready()
    if not ready:
        # UAC 同意了但它没起来：把原因如实带出去，别让用户以为"授权成功了"。
        #
        # 这句必须**可行动**。此前只写"helper 已启动但未就绪（缺少端口文件）"，
        # 用户能做的最多是把同一件事再做一遍；而这条路上的失败几乎总是下面两种，
        # 且处置方式完全不同：
        #   * UAC 窗口里用了**其他管理员账户**（标准用户的唯一选择）—— helper 运行在
        #     另一个账户下。现在引擎会把 `--state-dir` 显式下发（见
        #     `elevated_client.helper_argv`），所以正常不该再出现；若仍出现，说明
        #     那个账户连该目录都读不了（权限被改过），需要改用当前账户批准。
        #   * 安全软件拦截了提权子进程 —— 它不会出现在任何日志里，只能靠用户放行。
        return (
            False,
            f"提权助手已启动但未就绪（{wait_reason or '未知原因'}）。"
            "请在 UAC 窗口里用**当前登录账户**允许一次；若窗口里输入的是其他管理员账户，"
            "助手会以该账户身份运行，可能读不到本机的共享密钥与端口文件。"
            "另外请检查安全软件是否拦截了提权进程。",
            False,
        )

    # 就绪后必须清探测缓存：`_probe_restricted` 之前算出的"不可用"现在过期了。
    # 漏了这一步，用户会看到"授权成功了但档位还是灰的"。
    reset_probe()
    return True, "", False


def helper_stop() -> tuple[bool, str]:
    """请求提权 helper 自行退出。返回 `(ok, 原因)`。

    幂等：本来没在跑时返回 `(True, "")`。

    **不能**用 `TerminateProcess`：引擎是 Medium IL，helper 是 High IL，
    Medium 对 High 调 `PROCESS_TERMINATE` 会 err=5。所以只能请求它自己退
    （见 `elevated_client.stop` 与 helper 的 `/v1/shutdown`）。
    """
    client = _helper_client()
    if client is None:
        return False, "helper 客户端不可用"
    ok, reason = client.stop()
    reset_probe()
    return ok, reason


def helper_status() -> dict[str, Any]:
    """helper 的完整状态快照，供设置页展示。

    字段刻意分开，因为它们的**处置方式不同**：

    * `available` —— 现在能不能用（`restricted` 可不可选就看它）；
    * `detail` —— 不能用的具体原因（缺端口文件 / 无响应 / 没提权）；
    * `engine_elevated` —— 引擎自己是否提权。为真时 `restricted` 走本地路径，
      不需要 helper；为假才需要这个助手，界面的说明要跟着变。
    """
    ok, reason = helper_available()
    return {
        "available": ok,
        "detail": "" if ok else (reason or "提权助手不可用"),
        "engine_elevated": is_admin(),
        # helper 只在 Windows 上有意义；非 Windows 直接不可用（`helper_available`
        # 已经这么判了），这里多给一个字段让前端不必自己判平台。
        "supported": bool(win.IS_WINDOWS),
    }


def _launch_restricted_local(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_output: int,
) -> LaunchResult:
    """在本进程内直接用 `CreateProcessAsUserW` 建进程。

    需要 `SeIncreaseQuotaPrivilege`，因此只在引擎本身已提权时才可能成功。
    非提权下稳定 `err=5` —— 这是本机实测结论，不是推断。
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
        # 用**实际**桌面名：名字被占用时 `ensure_desktop` 会退回带 pid 的名字，
        # 这里若仍写常量，子进程就会被丢到一个不存在（或不属于本进程）的桌面上。
        si.lpDesktop = f"WinSta0\\{_desktop_name}"

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

    ## 为什么它不能走提权 helper

    helper 是**一次性**执行模型（收一条请求 → 起进程 → 读完两根管道 → 回结果），
    而常驻会话要求 stdin 在进程存活期间可以增量写入 —— 两者不兼容。

    这带来一个真实的限制：**引擎未提权时，`restricted` 档的 `shell_exec` 可用
    （走 helper），但 `persistent_shell` / `run_code` 不可用**（它们要常驻会话）。
    这个差异必须如实报出来，而不是让调用方撞上 `err=5` —— 那会把"设计限制"
    伪装成"机制坏了"。所以这里在动手前先判一次，给出可操作的原因。
    """
    if not win.IS_WINDOWS:
        return None, "not windows"
    if not argv:
        return None, "empty argv"

    if not is_admin():
        helper_ok, _helper_reason = helper_available()
        if helper_ok:
            # helper 在跑说明 restricted 档整体是"可达"的，但这一条路只能一次性执行。
            return None, (
                "常驻受限会话需要在引擎进程内直接建进程（CreateProcessAsUserW 需要 "
                "SeIncreaseQuotaPrivilege），而提权 helper 只提供一次性执行；"
                "引擎当前未提权"
            )

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
        si.lpDesktop = f"WinSta0\\{_desktop_name}"

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
    global _smoke_ok, _smoke_error, _desktop_handle, _desktop_error, _desktop_name
    global _appcontainer_ok, _appcontainer_error
    _smoke_ok = None
    _smoke_error = ""
    _desktop_handle = None
    _desktop_error = ""
    # 名字一并复位：上次可能退到了带 pid 的名字，重新探测应当从首选名字开始。
    _desktop_name = win_token.DESKTOP_NAME
    # AppContainer 的探测结果同样要清：重新探测的前提是"上次的结论不再可信"。
    _appcontainer_ok = None
    _appcontainer_error = ""
    # helper 的探测缓存也必须清：它正是"用户刚点了启用助手"之后要让
    # `restricted` 从不可选变成可选的那个开关。漏在这里，用户会看到
    # "授权成功了但档位还是灰的"。
    client = _helper_client()
    if client is not None:
        try:
            client.reset_probe()
        except Exception as exc:  # noqa: BLE001 — 复位失败不应让整个探测失效
            logger.debug("reset helper probe failed: %s", exc)


__all__ = [
    "LaunchResult",
    "RestrictedSession",
    "appcontainer_env",
    "appcontainer_exec_error",
    "appcontainer_exec_ready",
    "appcontainer_smoke_test",
    "build_env_block",
    "desktop_name",
    "desktop_ready",
    "ensure_desktop",
    "exec_error",
    "exec_ready",
    "helper_available",
    "helper_start",
    "helper_status",
    "helper_stop",
    "is_admin",
    "launch_appcontainer",
    "launch_restricted",
    "open_appcontainer_session",
    "open_restricted_session",
    "reset_probe",
    "smoke_test",
]
