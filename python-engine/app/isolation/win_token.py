"""win_token — L2 档：受限令牌（restricting SIDs）+ 可撤销目录授权。

## 为什么这一档比 `low_il` 更适合做默认值

`low_il` 靠 `SetTokenInformation(TokenIntegrityLevel)` 把**整个目录**打成 Low。
副作用有两处，都是对用户资产的实质改动：目录从父级继承的 ACE 不够用（缺
`WRITE_OWNER`），所以必须先给当前用户授完全控制才能打标签；而"恢复原状"没有
对称操作（`set_path_integrity` 打回去也换不回继承来的原始 ACE）。

本模块走另一条路，与 Reasonix 的实现同源：

1. `CreateRestrictedToken` 造出带 `WRITE_RESTRICTED` 的令牌，restricting SIDs 只留
   **合成 SID + logon SID + Everyone**；
2. 在需要写的根目录上加**一条** ACE 给合成 SID；
3. 系统对每次访问做两轮检查（enabled SIDs 与 restricting SIDs），两者都放行才通过。

撤销 = 删掉那条 ACE。精确、可逆、不留继承树痕迹。

## 官方文档已确认的三个边界

- `WRITE_RESTRICTED`（0x8）：restricting SIDs **只在评估写访问时**参与检查
  （`CreateRestrictedToken` 原文："considered only when evaluating write access"）。
  ⇒ **删除与重命名不受此约束**，只能在策略层拦（`rm`/`del`/`mv`/`rd` 及等价形态）。
- 受限令牌必须是当前令牌的**受限版本**时，`CreateProcessAsUser` **不需要**
  `SE_ASSIGNPRIMARYTOKEN_NAME` 特权 —— 这是零提权可行的依据。
- 官方 Warning：受限进程应运行在**非默认桌面**，否则可用 `SendMessage`/
  `PostMessage` 攻击默认桌面上的非受限应用。Reasonix 未处理这一点；本项目是
  Electron 多进程，风险更直接 —— 见 `desktop_isolation_ready()`，未就绪时
  `resolve_level` 会把本档判为不可用而不是假装隔离。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from app.isolation import win as _win_mod

#: 用子模块直接导入而非 `from app.isolation import win`：本模块由 `__init__.py`
#: 反向导入，包属性在那一刻可能尚未全部就绪。子模块路径不依赖包属性顺序。
win = _win_mod

logger = logging.getLogger(__name__)

#: 合成 SID：`WinNullSid` == `S-1-0-0`。
#:
#: 选它是因为**没有任何默认 ACL 会授权给 S-1-0-0**，所以"能访问"必然来自我们显式
#: 加的那条 ACE —— 白名单语义天然成立，不需要额外排除逻辑。
SYNTHETIC_SID = "S-1-0-0"

#: `CreateWellKnownSid` 的 WellKnownSidType
WIN_NULL_SID = 0
WIN_WORLD_SID = 1

#: `CreateRestrictedToken` 的 Flags
DISABLE_MAX_PRIVILEGE = 0x1
WRITE_RESTRICTED = 0x8

#: `SID_AND_ATTRIBUTES.Attributes`
SE_GROUP_ENABLED = 0x4

#: `TokenLogonSid`（`TOKEN_INFORMATION_CLASS`）
TOKEN_LOGON_SID = 28

#: `TokenUser`（`TOKEN_INFORMATION_CLASS`）
TOKEN_USER = 1

#: `TokenElevation`（`TOKEN_INFORMATION_CLASS`）
TOKEN_ELEVATION = 20

#: 打开令牌所需的最小访问位集合。
#:
#: `TOKEN_DUPLICATE` 是 `CreateRestrictedToken` 对源令牌的硬要求；
#: `TOKEN_QUERY` 用于读 `TokenUser` / `TokenLogonSid` / `TokenElevation`；
#: `TOKEN_ASSIGN_PRIMARY` 用于随后把派生出的令牌塞进 `CreateProcessAsUserW`。
TOKEN_DUPLICATE = 0x0002
TOKEN_ASSIGN_PRIMARY = 0x0001

#: `OpenProcess` 的最小访问位：只要进程信息，不要内存/线程。
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: 降级理由的字面量（写进协议响应、日志与 `reasons`，因此必须稳定）。
#:
#: 含义：**拿不到调用方（目标用户）的令牌**，只能退回 helper 自身令牌。此时
#: 限制性 SID 仍然生效（写访问依旧被 restricting SIDs 约束），但**用户身份**
#: 是 helper 的身份（通常是管理员）—— 这比"目标用户 + 限制性 SID"弱，
#: 所以必须逐次记录，而不是默默发生。
TOKEN_TARGET_UNAVAILABLE = "token_target_unavailable"

#: `ConvertSidToStringSidW` 返回值的释放方式
_LOCAL_FREE = None

#: 官方建议的独立桌面名。未创建时 `desktop_isolation_ready()` 返回 False。
DESKTOP_NAME = "ChironSandbox"

#: 执行层是否已能把子进程**以受限令牌**拉起。
#: `restricted` 档的执行层是否可用 —— **以实测为准**，不是常量开关。
#:
#: 历史：这里曾是一个硬编码的 `EXEC_READY = False`。那时执行层还没接上，
#: 提前置 True 会让配置与 UI 都声称挡得住写，而实际只有 Job Object。
#: 现在执行层在 `win_launch`（`CreateProcessAsUserW` + 可继承管道 + 独立桌面），
#: 这里的判定改为**真的起一个受限进程并取回输出**（`win_launch.exec_ready()`）。
#:
#: 实测限制（本机零点提权账户）：`CreateProcessAsUserW` 需要
#: `SeIncreaseQuotaPrivilege`，普通用户令牌未分配该特权 ⇒ 稳定 `err=5`；
#: 退路 `CreateProcessWithTokenW` 需要 `SeImpersonatePrivilege`，同样未分配 ⇒ `err=1314`。
#: 因此引擎**必须**以管理员身份运行才能启用本档，否则 `resolve_level` 降级到 `low_il`
#: 并在 `reasons` 里写明原因。
def exec_ready() -> bool:
    """执行层是否真能把子进程以受限令牌拉起（带缓存）。"""
    if not win.IS_WINDOWS:
        return False
    from app.isolation import win_launch  # 延迟导入：win_launch 反向依赖本模块

    return win_launch.exec_ready()


def exec_unavailable_reason() -> str:
    """`exec_ready()` 为假时的具体原因，用于 `reasons` 与 UI 提示。"""
    if not win.IS_WINDOWS:
        return "非 Windows 平台"
    from app.isolation import win_launch  # 延迟导入

    if win_launch.exec_ready():
        return ""
    detail = win_launch.exec_error()

    # 提示必须指向**正确的做法**。此前这里写"以管理员身份运行引擎"，那是把整个
    # 引擎提权 —— 而引擎承载模型输出，正是最不该提权的进程。现在的正解是只提权
    # 那个瘦 helper，所以文案跟着改，否则用户会按旧提示去做错事。
    helper_ok, helper_reason = win_launch.helper_available()
    if not helper_ok:
        return (
            f"受限令牌执行层不可用（{detail or '未知原因'}）；"
            f"提权 helper 不可用（{helper_reason or '未启动'}）——"
            "请在设置页启用「受限令牌执行助手」（会请求一次管理员授权）"
        )
    return f"受限令牌执行层不可用（{detail or '未知原因'}）"


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _FILETIME(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名
    """`GetProcessTimes` 的输出。只用来识别 pid 复用，不参与任何安全判定。"""

    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    def as_int(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


class _TOKEN_GROUPS(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [
        ("GroupCount", wt.DWORD),
        ("Groups", win._SID_AND_ATTRIBUTES * 1),  # noqa: SLF001 — 变长尾部，按 1 声明后手动索引
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


def _advapi() -> Any:
    """advapi32 句柄；加载失败返回 None（调用方按"机制不可用"处理）。"""
    try:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
    except OSError:  # pragma: no cover - 仅非 Windows
        return None
    lib.CreateWellKnownSid.restype = wt.BOOL
    lib.CreateWellKnownSid.argtypes = [
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wt.DWORD),
    ]
    lib.CreateRestrictedToken.restype = wt.BOOL
    lib.CreateRestrictedToken.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wt.HANDLE),
    ]
    lib.GetTokenInformation.restype = wt.BOOL
    lib.GetTokenInformation.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    lib.ConvertSidToStringSidW.restype = wt.BOOL
    lib.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wt.LPWSTR),
    ]
    lib.CreateProcessAsUserW.restype = wt.BOOL
    lib.CreateProcessAsUserW.argtypes = [
        wt.HANDLE,
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    return lib


def _k32() -> Any:
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.OpenProcessToken.restype = wt.BOOL
    lib.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    lib.GetCurrentProcess.restype = wt.HANDLE
    lib.CloseHandle.restype = wt.BOOL
    lib.CloseHandle.argtypes = [wt.HANDLE]
    lib.WaitForSingleObject.restype = wt.DWORD
    lib.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    lib.GetExitCodeProcess.restype = wt.BOOL
    lib.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    lib.LocalFree.restype = ctypes.c_void_p
    lib.LocalFree.argtypes = [ctypes.c_void_p]
    # `OpenProcess` 的返回类型**必须**显式声明成 HANDLE。
    # 不声明时 ctypes 按 `c_int`（32 位）解析返回值，64 位句柄会被截断：
    # 在 `elevated_helper._parent_alive` 里表现为 `GetExitCodeProcess(截断后的句柄)`
    # 失败 → 判定"父进程已死" → 提权 helper 启动约 2 秒后自杀，
    # 用户看到的是"助手启用成功但档位仍不可用"，日志里只有一句"父进程已退出"。
    lib.OpenProcess.restype = wt.HANDLE
    lib.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    # 进程创建时间：用于识别 **pid 复用**。helper 以"父进程 pid"作为调用方身份，
    # 而 pid 在父进程退出后会被系统回收再分配 —— 不校验创建时间，就可能在
    # 引擎死后把"恰好拿到同一个 pid 的另一个进程"当成调用方。
    lib.GetProcessTimes.restype = wt.BOOL
    lib.GetProcessTimes.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    return lib


def _well_known_sid(sid_type: int) -> bytes | None:
    """返回 SID 的原始字节（调用方自行持有 buffer 保证指针有效）。"""
    adv = _advapi()
    if adv is None:
        return None
    size = wt.DWORD(68)  # SECURITY_MAX_SID_SIZE
    buf = ctypes.create_string_buffer(size.value)
    if not adv.CreateWellKnownSid(sid_type, None, buf, ctypes.byref(size)):
        logger.debug("CreateWellKnownSid(%s) failed err=%s", sid_type, ctypes.get_last_error())
        return None
    return buf.raw[: size.value]


def _sid_to_string(sid_ptr: int) -> str:
    adv = _advapi()
    k32 = _k32()
    if adv is None or k32 is None or not sid_ptr:
        return ""
    out = wt.LPWSTR()
    if not adv.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(out)):
        return ""
    try:
        return out.value or ""
    finally:
        k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def token_user_sid(handle: Any) -> str:
    """任意令牌句柄的**用户 SID**（字符串形式），取不到返回空串。

    从 `current_user_sid` 抽出来，因为这里需要读的是**别人的**令牌：helper 要以
    调用方（引擎）的令牌造受限令牌，判定"现在的子进程到底是谁"也要读子进程令牌。
    """
    adv = _advapi()
    if adv is None or not handle:
        return ""
    try:
        need = wt.DWORD(0)
        adv.GetTokenInformation(handle, TOKEN_USER, None, 0, ctypes.byref(need))
        if need.value == 0:
            return ""
        buf = ctypes.create_string_buffer(need.value)
        if not adv.GetTokenInformation(handle, TOKEN_USER, buf, need.value, ctypes.byref(need)):
            return ""
        # TOKEN_USER 就是一个 SID_AND_ATTRIBUTES，Sid 在偏移 0
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(win._SID_AND_ATTRIBUTES)).contents.Sid  # noqa: SLF001
        return _sid_to_string(int(sid_ptr or 0))
    except Exception:  # noqa: BLE001 — 读不到就是"身份未知"，由调用方按不可用处理
        logger.debug("token_user_sid failed", exc_info=True)
        return ""


def token_elevated(handle: Any) -> bool:
    """令牌是否处于**提权**状态（`TokenElevation`）。"""
    adv = _advapi()
    if adv is None or not handle:
        return False
    try:
        value = wt.DWORD()
        size = wt.DWORD()
        if not adv.GetTokenInformation(
            handle,
            TOKEN_ELEVATION,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(size),
        ):
            return False
        return bool(value.value)
    except Exception:  # noqa: BLE001
        return False


def is_administrator_identity(user_sid: str, elevated: bool = False) -> bool:
    """这条"身份"是不是管理员。

    ## 为什么要一个单独的名字

    P0-1 的不变量就是"**受限进程的 token 用户不得是管理员**"。把判定写成函数
    而不是散落在调用方里，是为了让它同时被运行时（日志/降级记录）与测试
    （结构断言）使用 —— 两边各写一份，迟早会有一边被改松。

    判定取两条**任一**成立：

    * `elevated` 为真 —— 令牌本身已提权（UAC 已同意），这就是 High IL 的管理员；
    * SID 的 RID 为 `500` —— 内置 Administrator 账户，即使未提权也不该作为
      沙箱子进程的身份（它与普通管理员账户的 SID 形状不同，是唯一可靠标识）。

    刻意**不**用"是否在 `BUILTIN\\Administrators` 组里"：UAC 下的普通用户令牌
    也带这个组（deny-only），用它判定会把所有管理员账户的普通会话误报成
    "以管理员运行"，而这个函数恰恰要用来区分这两种情况。
    """
    if elevated:
        return True
    return user_sid.endswith("-500")


def process_identity(pid: int) -> tuple[str, bool]:
    """进程的 (用户 SID, 是否提权)。打不开返回 `("", False)`。

    用 pid 而不是进程句柄：调用点有两种（helper 查调用方、启动后回查子进程），
    而 `PROCESS_INFORMATION` 里本来就有 pid；统一走 pid 少一条句柄生命周期。
    """
    k32 = _k32()
    if k32 is None or pid <= 0:
        return "", False
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        logger.debug("process_identity: OpenProcess(%s) 失败 err=%s", pid, ctypes.get_last_error())
        return "", False
    try:
        token = wt.HANDLE()
        if not k32.OpenProcessToken(handle, win.TOKEN_QUERY, ctypes.byref(token)):
            return "", False
        try:
            return token_user_sid(token), token_elevated(token)
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(handle)


def process_creation_time(pid: int) -> int | None:
    """进程创建时间（FILETIME 的 64 位整数）。取不到返回 `None`。"""
    k32 = _k32()
    if k32 is None or pid <= 0:
        return None
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not k32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return created.as_int()
    finally:
        k32.CloseHandle(handle)


def open_process_token(pid: int, access: int | None = None) -> tuple[Any, str]:
    """打开**另一个进程**的主令牌。返回 `(handle, 错误)`。

    这是"以调用方身份降权"的第一步：helper 是提权进程，能打开调用方（Medium IL）
    的令牌，随后用 `CreateRestrictedToken` 派生出**同一用户**的受限令牌。

    访问位默认按 `CreateRestrictedToken` 的要求给足：少 `TOKEN_DUPLICATE` 会
    稳定 `ERROR_ACCESS_DENIED(5)`，而错误信息完全不提是哪个位缺。
    """
    k32 = _k32()
    if k32 is None:
        return None, "kernel32 不可用"
    if pid <= 0:
        return None, "pid 无效"
    wanted = (
        win.TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY if access is None else access
    )
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None, f"OpenProcess(pid={pid}) 失败 err={ctypes.get_last_error()}"
    try:
        token = wt.HANDLE()
        if not k32.OpenProcessToken(handle, wanted, ctypes.byref(token)):
            return None, f"OpenProcessToken(pid={pid}) 失败 err={ctypes.get_last_error()}"
        return token, ""
    finally:
        k32.CloseHandle(handle)


def current_user_sid() -> str:
    """当前进程令牌的**用户 SID**（字符串形式），取不到返回空串。

    独立桌面的 DACL 必须显式包含它，否则连**创建者自己**都打不开那个桌面。

    这不是推测，是本机实测的失败原因：

        SDDL = "D:(A;;GA;;;S-1-0-0)(A;;GA;;;SY)(A;;GA;;;BA)"
        CreateDesktopW(新名字) -> 成功
        CreateDesktopW/OpenDesktopW(同名) -> err=5 (ACCESS_DENIED)

    即 `S-1-0-0` 是 NULL SID，而 NULL SID 在访问检查里**不匹配任何主体**
    （它只对"匿名"这一抽象主体有意义），于是那份 DACL 实际上只授权了
    SYSTEM 与 Administrators。UAC 下的普通令牌里 `BUILTIN\\Administrators`
    是 deny-only 组，提权后也不是"当前用户"，两种情况都过不了检查 ——
    所以"以管理员身份运行"救不了它，只会换一个同样打不开的身份。
    """
    adv = _advapi()
    k32 = _k32()
    if adv is None or k32 is None:
        return ""
    own = wt.HANDLE()
    if not k32.OpenProcessToken(
        k32.GetCurrentProcess(), win.TOKEN_QUERY, ctypes.byref(own)
    ):
        logger.debug("current_user_sid: OpenProcessToken failed err=%s", ctypes.get_last_error())
        return ""
    try:
        return token_user_sid(own)
    finally:
        k32.CloseHandle(own)


def _logon_sid(token: Any) -> tuple[bytes | None, str]:
    """取当前令牌的 logon SID。

    受限令牌**必须**保留 logon SID，否则进程访问不了自己的窗口站/桌面，
    表现为启动即失败（不是"权限不足"），极难排查 —— 所以取不到就判机制不可用。
    """
    adv = _advapi()
    if adv is None:
        return None, ""
    need = wt.DWORD(0)
    adv.GetTokenInformation(token, TOKEN_LOGON_SID, None, 0, ctypes.byref(need))
    if need.value == 0:
        return None, ""
    buf = ctypes.create_string_buffer(need.value)
    if not adv.GetTokenInformation(token, TOKEN_LOGON_SID, buf, need, ctypes.byref(need)):
        return None, ""
    groups = ctypes.cast(buf, ctypes.POINTER(_TOKEN_GROUPS)).contents
    if groups.GroupCount < 1:
        return None, ""
    sid_ptr = groups.Groups[0].Sid
    if not sid_ptr:
        return None, ""
    text = _sid_to_string(int(sid_ptr))
    if not text:
        return None, ""
    # SID 长度由 SubAuthorityCount（第 2 字节）决定：8 + 4 * count
    head = ctypes.string_at(sid_ptr, 8)
    length = 8 + 4 * head[1]
    return ctypes.string_at(sid_ptr, length), text


def create_restricted_token_from(source: Any | None = None) -> tuple[Any, str] | None:
    """基于**任一**令牌造受限令牌。成功返回 `(token_handle, synthetic_sid)`。

    `source=None` 等价于旧行为：拿**本进程**的令牌派生。传句柄则是 P0-1 的关键 ——
    helper 用**调用方（引擎）的令牌**派生受限令牌，于是子进程运行在调用方的
    用户/完整性级别下（而非 helper 的提权令牌），限制性 SID 只额外约束写访问。

    调用方负责 `CloseHandle(token)`；`source` 由调用方持有与关闭。
    """
    adv, k32 = _advapi(), _k32()
    if adv is None or k32 is None or not win.IS_WINDOWS:
        return None

    own: Any = None
    if source is None:
        own = wt.HANDLE()
        if not k32.OpenProcessToken(
            k32.GetCurrentProcess(), win.TOKEN_QUERY | win.TOKEN_DUPLICATE, ctypes.byref(own)
        ):
            logger.debug("OpenProcessToken failed err=%s", ctypes.get_last_error())
            return None
        source = own

    try:
        logon_bytes, logon_text = _logon_sid(source)
        everyone = _well_known_sid(WIN_WORLD_SID)
        synthetic = _well_known_sid(WIN_NULL_SID)
        if not everyone or not synthetic:
            return None
        if not logon_bytes:
            # 没有 logon SID 时不造令牌：造出来也用不了（启动即失败）
            logger.info("win_token: logon SID unavailable, restricted token not created")
            return None

        # buffer 必须存活到 CreateRestrictedToken 返回，否则 SID 指针悬空
        bufs = [
            ctypes.create_string_buffer(everyone),
            ctypes.create_string_buffer(logon_bytes),
            ctypes.create_string_buffer(synthetic),
        ]
        arr = (win._SID_AND_ATTRIBUTES * len(bufs))()  # noqa: SLF001
        for i, b in enumerate(bufs):
            arr[i].Sid = ctypes.cast(b, ctypes.c_void_p)
            # **必须为 0**：官方结构体文档写的是 `SID_AND_ATTRIBUTES`，但
            # `CreateRestrictedToken` 的 `SidsToRestrict` 不接受任何 attribute 位 ——
            # 传 `SE_GROUP_ENABLED(4)` 会稳定返回 `ERROR_INVALID_PARAMETER(87)`，
            # 且错误信息完全不提示是这里。本机实测：attrs=4 全失败、attrs=0 全成功。
            arr[i].Attributes = 0

        new = wt.HANDLE()
        ok = adv.CreateRestrictedToken(
            source,
            WRITE_RESTRICTED | DISABLE_MAX_PRIVILEGE,
            0,
            None,
            0,
            None,
            len(bufs),
            ctypes.cast(arr, ctypes.c_void_p),
            ctypes.byref(new),
        )
        if not ok:
            logger.info("CreateRestrictedToken failed err=%s", ctypes.get_last_error())
            return None
        logger.debug("restricted token created (logon=%s)", logon_text)
        return new, SYNTHETIC_SID
    except Exception:  # noqa: BLE001 — 任何异常都按"机制不可用"
        logger.warning("create_restricted_token unexpected error", exc_info=True)
        return None
    finally:
        if own is not None and own:
            k32.CloseHandle(own)


def create_restricted_token() -> tuple[Any, str] | None:
    """本进程令牌的受限版本（历史入口，语义不变）。

    调用方负责 `CloseHandle(token)`。失败即"机制不可用"，由 `resolve_level`
    判为 `below_floor` —— 绝不静默退回无隔离执行。
    """
    return create_restricted_token_from(None)


@dataclass(frozen=True)
class TargetToken:
    """一次受限令牌的"身份来源"描述。

    这是 P0-1 不变量在数据上的落点：`source` 说明令牌派生自谁的令牌
    （`"caller"` = 调用方用户，`"self"` = 本进程/helper 自身），`reason` 说明
    为什么退回了较弱的那条路。
    """

    token: Any = None
    user_sid: str = ""
    elevated: bool = False
    source: str = "self"
    reason: str = ""
    detail: str = ""


def _self_target(reason: str = "", detail: str = "", token: Any = None) -> TargetToken:
    """退回"本进程令牌"的结果（含降级理由）。"""
    sid, elevated = ("", False)
    if token:
        # 拿不到 SID 不影响可用性，只是报告里少一个字段
        sid, elevated = token_user_sid(token), token_elevated(token)
    return TargetToken(
        token=token,
        user_sid=sid,
        elevated=elevated,
        source="self",
        reason=reason,
        detail=detail,
    )


def restricted_token_for_caller(
    pid: int | None, *, expect_creation_time: int | None = None
) -> TargetToken:
    """为**调用方**（`pid`）造受限令牌；拿不到就退回本进程令牌并记录理由。

    ## 为什么这件事必须做（P0-1）

    受限令牌的 restricting SIDs **只在评估写访问时**参与检查（`WRITE_RESTRICTED`）。
    如果受限令牌派生自 helper 自己的**提权**令牌，子进程虽然写不了大部分地方，
    但读遍全盘、且"用户"仍是管理员 —— 隔离强度取决于"没有可用的提权路径"，
    这不该是设计前提。派生自调用方令牌之后，子进程的**用户身份**与调用方一致
    （通常是 Medium IL 的普通用户），这才是"降权"的本义。

    ## 为什么 pid 不能由请求方随意指定

    本函数只认**helper 启动时记录的父进程 pid**（见 `elevated_helper`），
    请求体里的 pid 仅用于交叉校验。否则攻击者控制引擎后可以让 helper
    打开任意进程的令牌 —— 那等于给引擎一个"以任意用户身份起进程"的能力，
    比原来的问题更糟。

    ## 降级

    目标令牌不可得（进程已退出 / 跨账户被拒 / pid 复用）时**不抛异常、也不拒绝
    执行**：退回本进程令牌，`reason=token_target_unavailable`，由调用方写进日志
    与状态。执行仍然会走到"受限令牌 + 独立桌面"，只是身份没有降到调用方。
    """
    if pid is None or pid <= 0 or int(pid) == os.getpid():
        made = create_restricted_token_from(None)
        if made is None:
            return TargetToken(detail="无法基于本进程令牌创建受限令牌")
        return _self_target(token=made[0])

    handle, detail = open_process_token(int(pid))
    if handle is None:
        logger.warning(
            "无法打开调用方令牌（pid=%s）：%s —— 退回本进程令牌（%s）",
            pid,
            detail,
            TOKEN_TARGET_UNAVAILABLE,
        )
        made = create_restricted_token_from(None)
        return _self_target(TOKEN_TARGET_UNAVAILABLE, detail, made[0] if made else None)

    try:
        if expect_creation_time is not None:
            actual = process_creation_time(int(pid))
            if actual is None or actual != expect_creation_time:
                detail = f"调用方 pid={pid} 的创建时间不匹配（可能已被复用）"
                logger.warning("%s —— 退回本进程令牌（%s）", detail, TOKEN_TARGET_UNAVAILABLE)
                made = create_restricted_token_from(None)
                return _self_target(TOKEN_TARGET_UNAVAILABLE, detail, made[0] if made else None)

        sid, elevated = token_user_sid(handle), token_elevated(handle)
        made = create_restricted_token_from(handle)
        if made is None:
            detail = f"无法基于调用方令牌创建受限令牌（pid={pid}）"
            logger.warning("%s —— 退回本进程令牌（%s）", detail, TOKEN_TARGET_UNAVAILABLE)
            fallback = create_restricted_token_from(None)
            return _self_target(TOKEN_TARGET_UNAVAILABLE, detail, fallback[0] if fallback else None)
        return TargetToken(
            token=made[0],
            user_sid=sid,
            elevated=elevated,
            source="caller",
            reason="",
            detail="",
        )
    finally:
        k32 = _k32()
        if k32 is not None:
            k32.CloseHandle(handle)


def probe() -> bool:
    """`resolve_level` 用：能否造出受限令牌。不在本进程真降级。"""
    made = create_restricted_token()
    if made is None:
        return False
    token, _ = made
    _k32().CloseHandle(token)
    return True


def desktop_isolation_ready() -> bool:
    """受限进程是否运行在独立桌面上（`ChironSandbox`）。

    微软官方要求：受限进程若运行在默认桌面，仍可对同桌面的非受限进程发
    `SendMessage`/`PostMessage`。chiron 是 Electron 应用，主进程窗口就在默认桌面上，
    不隔离桌面这条攻击面就是敞开的。

    实现放在 `win_launch`（需要 user32 的 `CreateDesktopW`），这里只做委托，
    避免两个模块各持一份桌面句柄。非 Windows 返回 False。
    """
    if not win.IS_WINDOWS:
        return False
    from app.isolation import win_launch  # 延迟导入：win_launch 反向依赖本模块

    return win_launch.desktop_ready()


# ── 目录授权（一条 ACE，可精确撤销）───────────────────────────────


def grant_path(path: str, sid: str = SYNTHETIC_SID, *, modify: bool = True) -> tuple[bool, str]:
    """给合成 SID 授 `path` 的 (OI)(CI) 权限。用 icacls，与 win.py 的既有做法一致。"""
    if not win.IS_WINDOWS:
        return False, "not windows"
    perm = "(OI)(CI)M" if modify else "(OI)(CI)R"
    try:
        proc = subprocess.run(
            ["icacls", path, "/grant", f"*{sid}:{perm}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, ""


def revoke_path(path: str, sid: str = SYNTHETIC_SID) -> tuple[bool, str]:
    """撤销授权。这就是"随时可撤销"的实现 —— 删一条 ACE，不动继承树。"""
    if not win.IS_WINDOWS:
        return False, "not windows"
    try:
        proc = subprocess.run(
            ["icacls", path, "/remove", f"*{sid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    # 没有该 ACE 时 icacls 返回非 0，但状态已经是"已撤销"——按幂等处理
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        if "0 successfully processed" in msg or "找不到" in msg:
            return True, ""
        return False, msg
    return True, ""


def grant_report(paths: list[str]) -> dict[str, Any]:
    """当前授权清单，供 `GET /v1/system/isolation` 展示"磁盘上改了什么"。"""
    return {
        "synthetic_sid": SYNTHETIC_SID,
        "paths": list(paths),
        "desktop_isolation": desktop_isolation_ready(),
    }


__all__ = [
    "DESKTOP_NAME",
    "SYNTHETIC_SID",
    "TOKEN_TARGET_UNAVAILABLE",
    "TargetToken",
    "create_restricted_token",
    "create_restricted_token_from",
    "current_user_sid",
    "desktop_isolation_ready",
    "grant_path",
    "grant_report",
    "is_administrator_identity",
    "open_process_token",
    "probe",
    "process_creation_time",
    "process_identity",
    "restricted_token_for_caller",
    "revoke_path",
    "token_elevated",
    "token_user_sid",
]
