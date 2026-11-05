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
import subprocess
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
    return (
        f"受限令牌执行层不可用（{detail or '未知原因'}）；"
        "需要以管理员身份运行引擎以获得 SeIncreaseQuotaPrivilege"
    )


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


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


def create_restricted_token() -> tuple[Any, str] | None:
    """造受限令牌。成功返回 `(token_handle, synthetic_sid)`，失败返回 `None`。

    调用方负责 `CloseHandle(token)`。失败即"机制不可用"，由 `resolve_level`
    判为 `below_floor` —— 绝不静默退回无隔离执行。
    """
    adv, k32 = _advapi(), _k32()
    if adv is None or k32 is None or not win.IS_WINDOWS:
        return None

    own = wt.HANDLE()
    if not k32.OpenProcessToken(
        k32.GetCurrentProcess(), win.TOKEN_QUERY | win.TOKEN_DUPLICATE, ctypes.byref(own)
    ):
        logger.debug("OpenProcessToken failed err=%s", ctypes.get_last_error())
        return None

    try:
        logon_bytes, logon_text = _logon_sid(own)
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
            own,
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
        k32.CloseHandle(own)


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
    "create_restricted_token",
    "desktop_isolation_ready",
    "grant_path",
    "grant_report",
    "probe",
    "revoke_path",
]
