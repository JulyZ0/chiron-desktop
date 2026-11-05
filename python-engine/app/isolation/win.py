"""win_isolation — Windows 原生执行隔离原语（零提权可用）。

## 为什么需要这个模块

README 曾承诺"工具执行下沉到受限容器"，但本仓库是 **Electron 桌面端 + 内嵌 PG/Redis**，
用户机器上不该要求装 Docker。所以隔离必须落在 **Windows 原生机制**上。本模块封装三个
实测可用的原语，按强度递增排列：

| 机制 | 零提权 | 挡住什么 | 挡不住什么 |
|---|---|---|---|
| `job` | ✅ | 内存/CPU 打爆、进程数失控、子孙进程残留 | 读宿主文件、外连网络 |
| `low_il` | ✅ | 以上 + **写**高完整性对象（用户目录、注册表） | 读宿主文件（默认可读）、网络 |
| `appcontainer` | ⚠️ 需 ACL 授权 | 以上 + 文件系统按包 SID 白名单、可断网 | 授权过的路径 |

`appcontainer` 需要给目标目录/可执行文件用 `icacls` 授权包 SID，否则子进程会以
`STATUS_DLL_NOT_FOUND (0xC0000135)` 退出 —— 这是实测结论，不是推测。

## 可撤销

所有机制都是**进程级**的：Job 句柄属于本进程，子进程退出/句柄关闭即失效；Low IL 只改
子进程自己的令牌；AppContainer 只建一个 profile（可 `revoke_all()` 删除）。不写注册表、
不建系统账户、不改系统 ACL —— 除非用户显式调用 `grant_appcontainer_path()`。

## 平台

仅 Windows。非 win32 平台所有函数返回 `False` 并保持静默 —— POSIX 侧继续走
`app/tools/sandbox.py` 的 `setrlimit` 路径（`_rlimit_kwargs`）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ── 常量 ────────────────────────────────────────────────────────────
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_DUPLICATE = 0x0002
TokenIntegrityLevel = 25

#: 完整性级别 RID：Low = 0x1000，Medium = 0x2000
INTEGRITY_LOW = "S-1-16-4096"
INTEGRITY_MEDIUM = "S-1-16-8192"


@dataclass(frozen=True)
class ResourceLimits:
    """Job Object 的资源上限。0 / None 表示该项不限制。"""

    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 60
    active_processes: int = 16

    def to_flags(self) -> tuple[int, JOBOBJECT_EXTENDED_LIMIT_INFORMATION]:
        flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if self.memory_bytes:
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY
            info.ProcessMemoryLimit = self.memory_bytes
            info.JobMemoryLimit = self.memory_bytes
        if self.cpu_seconds:
            flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
            # 单位 100ns
            info.BasicLimitInformation.PerProcessUserTimeLimit = int(self.cpu_seconds * 10_000_000)
        if self.active_processes:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = self.active_processes
        info.BasicLimitInformation.LimitFlags = flags
        return flags, info


# ── 结构体（顺序与 Windows SDK 一致；64 位对齐由 ctypes 自动处理）──────
class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):  # noqa: N801 — 沿用 Windows SDK 原名，便于对照文档
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def _load_dlls() -> tuple[Any, Any, Any] | None:
    """惰性加载 DLL 并声明返回类型（避免句柄被截断成 c_int）。"""
    if not IS_WINDOWS:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
    except OSError:  # pragma: no cover
        return None

    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.GetCurrentProcess.argtypes = []
    k32.CreateJobObjectW.restype = wt.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.SetInformationJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]

    advapi.OpenProcessToken.restype = wt.BOOL
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.SetTokenInformation.restype = wt.BOOL
    advapi.ConvertStringSidToSidW.restype = wt.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]

    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wt.LPCWSTR]
    return k32, advapi, userenv


_DLLS = _load_dlls()


# ── Job Object ──────────────────────────────────────────────────────
class JobObject:
    """进程组资源限制。

    用法::

        with JobObject(ResourceLimits()) as job:
            proc = subprocess.Popen(...)
            job.assign(proc.pid)
            proc.wait()          # 超内存会被内核杀掉
        # 退出即杀光整棵进程树（KILL_ON_JOB_CLOSE）

    设计说明：**事后附加**（先 spawn 再 assign）而不是 CreateProcess 时挂载，这样
    既能配合 `asyncio.create_subprocess_exec`，也不需要改写所有既有调用点。代价是
    子进程在 assign 之前的极短窗口内不受限 —— 对"防误伤"足够，对"防有心绕过"不够，
    所以 `appcontainer` 档位才用 attribute-list 在创建时就挂上。
    """

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()
        self._handle: Any = None
        self.assigned: list[int] = []

    def __enter__(self) -> JobObject:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> bool:
        if not IS_WINDOWS or _DLLS is None:
            return False
        k32 = _DLLS[0]
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObjectW failed: %s", ctypes.get_last_error())
            return False
        _, info = self.limits.to_flags()
        ok = k32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("SetInformationJobObject failed: %s", ctypes.get_last_error())
            k32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def assign(self, pid: int) -> bool:
        """把进程（及其后续子孙）纳入本 job。"""
        if not self._handle or not IS_WINDOWS or _DLLS is None:
            return False
        k32 = _DLLS[0]
        # asyncio 的 Process 没有 Popen 句柄可移植地取出，统一按 pid 重开
        hproc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            logger.debug("OpenProcess(%s) failed: %s", pid, ctypes.get_last_error())
            return False
        try:
            ok = bool(k32.AssignProcessToJobObject(self._handle, hproc))
            if ok:
                self.assigned.append(pid)
            else:
                # 已在 job 中（Windows 8+ 支持嵌套，但显式报错便于排查）
                logger.debug(
                    "AssignProcessToJobObject(%s) failed: %s", pid, ctypes.get_last_error()
                )
            return ok
        finally:
            k32.CloseHandle(hproc)

    def close(self) -> None:
        if self._handle and IS_WINDOWS and _DLLS is not None:
            _DLLS[0].CloseHandle(self._handle)
            self._handle = None


# ── 完整性级别 ──────────────────────────────────────────────────────
def set_integrity_level(sid_string: str = INTEGRITY_LOW) -> bool:
    """把**当前进程**的令牌完整性级别设为低（不可逆地降低自身权限）。

    只能在子进程里调用 —— 判断依据是"降级后无法再恢复"，这正是我们要的语义。
    实测：设为 Low 后写 `%USERPROFILE%` 会被拒绝（Access Denied）。
    """
    if not IS_WINDOWS or _DLLS is None:
        return False
    k32, advapi, _ = _DLLS
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(
        k32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_ADJUST_DEFAULT, ctypes.byref(token)
    ):
        logger.warning("OpenProcessToken failed: %s", ctypes.get_last_error())
        return False
    try:
        sid = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(sid_string, ctypes.byref(sid)):
            logger.warning("ConvertStringSidToSidW failed: %s", ctypes.get_last_error())
            return False
        try:
            label = _TOKEN_MANDATORY_LABEL()
            label.Label.Sid = sid
            label.Label.Attributes = 0x00000020  # SE_GROUP_INTEGRITY
            # SID 长度：Low/Medium 均为 12 字节（S-1-16-xxxx）
            sid_len = 12
            ok = advapi.SetTokenInformation(
                token,
                TokenIntegrityLevel,
                ctypes.byref(label),
                ctypes.sizeof(label) + sid_len,
            )
            if not ok:
                logger.warning("SetTokenInformation failed: %s", ctypes.get_last_error())
            return bool(ok)
        finally:
            k32.LocalFree(sid) if hasattr(k32, "LocalFree") else None
    finally:
        k32.CloseHandle(token)


# ── AppContainer ────────────────────────────────────────────────────
APP_CONTAINER_PREFIX = "chiron-sandbox-"


def appcontainer_name(suffix: str = "default") -> str:
    """包名。带固定前缀便于 `revoke_all()` 精确收割，不误删他人 profile。"""
    safe = "".join(ch for ch in suffix if ch.isalnum() or ch in "-_") or "default"
    return f"{APP_CONTAINER_PREFIX}{safe}"


def ensure_appcontainer(name: str) -> tuple[bool, str, str]:
    """创建（或复用）AppContainer profile。

    Returns:
        `(是否可用, 包 SID 字符串, 错误说明)`。SID 需要交给 `grant_appcontainer_path`
        去给目录授权，否则子进程加载 DLL 会失败。
    """
    if not IS_WINDOWS or _DLLS is None:
        return False, "", "非 Windows 平台"
    _, advapi, userenv = _DLLS
    sid_ptr = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(name, name, name, None, 0, ctypes.byref(sid_ptr))
    code = hr & 0xFFFFFFFF
    if code == 0x800700B7:  # ERROR_ALREADY_EXISTS
        userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wt.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        hr2 = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid_ptr))
        if hr2 != 0:
            return False, "", f"DeriveSid hr=0x{hr2 & 0xFFFFFFFF:08X}"
    elif code != 0:
        return False, "", f"CreateAppContainerProfile hr=0x{code:08X}"

    advapi.ConvertSidToStringSidW.restype = wt.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    out = wt.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        return False, "", "ConvertSidToStringSidW failed"
    return True, out.value or "", ""


def grant_appcontainer_path(sid: str, path: str, *, modify: bool = True) -> tuple[bool, str]:
    """给 AppContainer 包 SID 授权某个路径。

    这是 `appcontainer` 档位的**必需前置**：不授权，子进程连 python.exe 都加载不了。
    用 `icacls` 而不是直接改 DACL —— 可读、可撤销（`remove_appcontainer_path`）、
    且不需要写几百行 ACL 代码。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台"
    if not sid:
        return False, "缺少包 SID"
    right = "(OI)(CI)(M)" if modify else "(OI)(CI)(RX)"
    try:
        import subprocess

        r = subprocess.run(
            ["icacls", path, "/grant", f"*{sid}:{right}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"icacls 执行失败: {exc}"
    if r.returncode != 0:
        return False, f"icacls 返回 {r.returncode}: {(r.stdout or r.stderr).strip()[:200]}"
    return True, ""


def remove_appcontainer_path(sid: str, path: str) -> tuple[bool, str]:
    """撤销授权（`grant_appcontainer_path` 的逆操作）。"""
    if not IS_WINDOWS or not sid:
        return False, "非 Windows 平台或缺少 SID"
    try:
        import subprocess

        r = subprocess.run(
            ["icacls", path, "/remove", f"*{sid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"icacls 执行失败: {exc}"
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:200]


# ── 目录完整性标签（low_il 档位的必需前置）────────────────────────────
#
# 实测（本机，非管理员）：
#   * 未打标签时 Low IL 子进程连 workspace 都写不了（WRITE / MKDIR 全 DENIED）
#     —— 这会让 run_code / persistent_shell 在 low_il 档位下完全不可用；
#   * `icacls <dir> /setintegritylevel (OI)(CI)L` 之后：工作区可写，
#     而**越界写入上级目录仍被拒**（ESCADE=denied），引擎自身（Medium）也照常可写；
#   * 给 Low IL SID 授写 ACE **无效** —— 完整性检查在 DACL 之前，降级才是正解。
INTEGRITY_LABEL_LOW = "(OI)(CI)L"
INTEGRITY_LABEL_MEDIUM = "(OI)(CI)M"


def set_path_integrity(path: str, label: str = INTEGRITY_LABEL_LOW) -> tuple[bool, str]:
    """给目录（及后代，靠 `OI/CI` 继承）打完整性标签。

    `L` = Low，`M` = Medium。用于让 Low IL 子进程能写自己的工作区，
    同时**不**放宽它写上级目录的能力（父目录标签不变）。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台"
    try:
        import subprocess

        r = subprocess.run(
            ["icacls", path, "/setintegritylevel", label],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"icacls 执行失败: {exc}"
    if r.returncode != 0:
        return False, f"icacls 返回 {r.returncode}: {(r.stdout or r.stderr).strip()[:200]}"
    return True, ""


def current_user_principal() -> str:
    """返回 `DOMAIN\\User`（icacls 认的写法）；取不到时退回裸用户名。"""
    user = (os.environ.get("USERNAME") or "").strip()
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    if user and domain:
        return f"{domain}\\{user}"
    return user


def grant_current_user_full_control(path: str) -> tuple[bool, str]:
    """给**当前用户**在自己工作区上授完全控制 —— 仅用于让完整性标签可写。

    ## 为什么需要这一步（实测根因，不是推测）

    工作区通常从父目录**继承** ACL，继承来的 ACE 对 `Authenticated Users` 只给
    「修改」（`M`），而 `M` **不含 `WRITE_OWNER`**；而修改完整性标签（SACL 里的
    mandatory label）恰好需要 `WRITE_OWNER`。于是：

    * 读 ACL 成功、所有者就是当前用户、进程也不是提权状态；
    * 但 `icacls <dir> /setintegritylevel L` 一律返回 `5（拒绝访问）`。

    表现就是 `low_il` 在这台机器上**静默降级成 `job`** —— 隔离承诺悄悄缩水。
    本机实测：先 `/grant:r "<user>:(OI)(CI)F"`，再打标签即成功。

    ## 安全性

    作用范围**只有传进来的这一个目录**（调用方保证是当前用户的工作区，不是
    `SANDBOX_ROOT`），且授的是"所有者对自己工作区的完全控制"，不越权到别人的
    工作区、更不碰系统目录。撤销用 `icacls <dir> /remove:g <user>`。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台"
    principal = current_user_principal()
    if not principal:
        return False, "无法确定当前用户（USERNAME 为空）"
    try:
        import subprocess

        r = subprocess.run(
            ["icacls", path, "/grant:r", f"{principal}:(OI)(CI)F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"icacls 授权失败: {exc}"
    if r.returncode != 0:
        return False, f"icacls 授权返回 {r.returncode}: {(r.stdout or r.stderr).strip()[:200]}"
    logger.info("工作区 %s 已给 %s 授完全控制（仅为可写完整性标签）", path, principal)
    return True, ""


def delete_appcontainer(name: str) -> tuple[bool, str]:
    """删除 profile —— 这是 `appcontainer` 档位的撤销动作。"""
    if not IS_WINDOWS or _DLLS is None:
        return False, "非 Windows 平台"
    _, _, userenv = _DLLS
    hr = userenv.DeleteAppContainerProfile(name)
    code = hr & 0xFFFFFFFF
    if code in (0, 0x80070002):  # 成功 / 本来就不存在
        return True, ""
    return False, f"hr=0x{code:08X}"


def capability_report() -> dict[str, Any]:
    """当前机器上各机制是否可用 —— 供启动自检与 `python -m app.isolation status`。

    只读：探测即建即删，不留残留。
    """
    report: dict[str, Any] = {
        "platform": sys.platform,
        "is_windows": IS_WINDOWS,
        "dlls_loaded": _DLLS is not None,
        "job_object": False,
        "low_integrity": False,
        "appcontainer": False,
        "appcontainer_sid": "",
        "notes": [],
    }
    if not IS_WINDOWS:
        report["notes"].append("非 Windows：使用 POSIX setrlimit 路径")
        return report
    if _DLLS is None:
        report["notes"].append("无法加载 kernel32/advapi32/userenv")
        return report

    # Job Object：建一个空 job 验证 API 可用
    job = JobObject()
    report["job_object"] = job.open()
    job.close()

    # Low IL：不能在本进程上试（会真的降级），只验证 token 可打开
    k32, advapi, _ = _DLLS
    token = wt.HANDLE()
    if advapi.OpenProcessToken(
        k32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_ADJUST_DEFAULT, ctypes.byref(token)
    ):
        report["low_integrity"] = True
        k32.CloseHandle(token)
    else:
        report["notes"].append(f"OpenProcessToken 失败: {ctypes.get_last_error()}")

    # AppContainer：建了立刻删
    probe_name = appcontainer_name("probe")
    ok, sid, err = ensure_appcontainer(probe_name)
    report["appcontainer"] = ok
    report["appcontainer_sid"] = sid
    if ok:
        delete_appcontainer(probe_name)
    elif err:
        report["notes"].append(f"AppContainer: {err}")

    return report


__all__ = [
    "APP_CONTAINER_PREFIX",
    "INTEGRITY_LOW",
    "INTEGRITY_MEDIUM",
    "IS_WINDOWS",
    "JobObject",
    "ResourceLimits",
    "appcontainer_name",
    "capability_report",
    "delete_appcontainer",
    "ensure_appcontainer",
    "grant_appcontainer_path",
    "remove_appcontainer_path",
    "set_integrity_level",
]
