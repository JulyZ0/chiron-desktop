"""引擎侧的提权 helper 客户端：懒启动、健康探测、失败降级。

## 这一层的职责边界

引擎**不**试图自己提权（`ShellExecuteW(runas)` 只在用户显式点击时调用一次，
见 `spawn_elevated`）。平时的每次 `restricted` 执行走的是：

    restrict 档执行 → available() → helper_exec() → 受限令牌进程

`available()` 有缓存，但缓存只在"成功"时长期有效：helper 是独立进程，可能被
用户关掉或崩溃，所以**失败不缓存**，否则一次瞬时失败会让整个会话再也用不上
`restricted` —— 那正是"看起来降级了、其实是再也不回升"的老问题。

## 为什么降级要如实说明

`restricted` 不可用时**不能**悄悄按 `low_il` 执行还报告 restricted：配置、状态
接口、日志三处都会声称比实际更强。所以这里把不可用原因交给调用方写进
`reasons`，由 `resolve_level` 统一对外呈现。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import subprocess
import sys
import threading

from app.isolation import elevated_protocol as proto
from app.isolation import win
from app.isolation.win_launch import LaunchResult

logger = logging.getLogger(__name__)

#: 探测结果的缓存。`:data:`None` 表示"还没探测过"。
#:
#: 只缓存**成功**：helper 是外部进程，随时可能消失，把失败也缓存住会让一次
#: 瞬时故障变成永久降级。
_ready: bool | None = None
_ready_reason = ""
_lock = threading.Lock()


def reset_probe() -> None:
    """清缓存，供档位切换后重新探测（与 `win_launch.reset_probe` 同语义）。"""
    global _ready, _ready_reason
    with _lock:
        _ready = None
        _ready_reason = ""


# ── 共享密钥 ─────────────────────────────────────────────


def ensure_key() -> tuple[bytes | None, str]:
    """取得共享密钥，不存在则生成。返回 `(key, 警告)`。

    密钥**只生成一次**：`write_key` 已存在时不覆盖。换钥匙会让正在运行的
    提权 helper 立刻鉴权失败，那个后果比留一把旧钥匙严重。
    """
    existing = proto.read_key()
    if existing:
        return existing, ""
    key = secrets.token_bytes(proto.KEY_BYTES)
    warning = proto.write_key(key)
    if warning:
        logger.warning("helper 密钥写入告警: %s", warning)
    # 并发下另一个线程可能先写成功，以磁盘上的为准。
    return proto.read_key() or key, warning


# ── 健康探测 ─────────────────────────────────────────────


def _read_port() -> int:
    try:
        raw = proto.port_path().read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return 0


def _ping(port: int, key: bytes, timeout: float) -> tuple[bool, bool, str]:
    """探测 helper。返回 `(ok, elevated, detail)`。"""
    conn = http.client.HTTPConnection(proto.LOOPBACK, port, timeout=timeout)
    try:
        conn.request(
            "GET",
            "/v1/ping",
            headers={proto.SIGNATURE_HEADER: proto.sign(key, b"")},
        )
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return False, False, f"ping HTTP {resp.status}"
        data = json.loads(body)
        if int(data.get("protocol", -1)) != proto.PROTOCOL_VERSION:
            # 版本不一致要拒绝：半懂不懂地解析"起进程"指令比重启 helper 危险。
            return False, False, (
                f"helper 协议版本 {data.get('protocol')} 与引擎 {proto.PROTOCOL_VERSION} 不一致"
            )
        return True, bool(data.get("elevated")), ""
    except (OSError, ValueError) as exc:
        return False, False, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def available() -> tuple[bool, str]:
    """helper 是否可用于 `restricted` 执行。返回 `(ok, 原因)`。"""
    global _ready, _ready_reason
    if not win.IS_WINDOWS:
        return False, "非 Windows 平台"
    if _ready:
        return True, ""

    key, _warn = ensure_key()
    if not key:
        return False, "无法建立 helper 共享密钥"

    port = _read_port()
    if not port:
        reason = "提权 helper 未启动（缺少端口文件）"
        with _lock:
            _ready_reason = reason
        return False, reason

    ok, elevated, detail = _ping(port, key, proto.PING_TIMEOUT_SECONDS)
    if not ok:
        # 端口文件可能是上一次运行残留的，清掉以免每次都白试。
        _clear_port_file()
        reason = f"提权 helper 无响应（{detail}）"
        with _lock:
            _ready_reason = reason
        return False, reason
    if not elevated:
        # helper 活着但没提权：`CreateProcessAsUserW` 仍会 err=5。
        # 如实报告，不要让用户以为"helper 起了就等于好了"。
        reason = "提权 helper 已启动但未获得管理员权限（UAC 未同意）"
        with _lock:
            _ready_reason = reason
        return False, reason

    with _lock:
        _ready = True
        _ready_reason = ""
    return True, ""


def unavailable_reason() -> str:
    """`restricted` 走 helper 不可用的具体原因（供 `reasons` 使用）。"""
    ok, reason = available()
    return "" if ok else (reason or _ready_reason or "提权 helper 不可用")


def _clear_port_file() -> None:
    try:
        proto.port_path().unlink(missing_ok=True)
    except OSError:
        pass


# ── 停止（由 helper 自己退） ───────────────────────────────


def stop(timeout: float = 10.0) -> tuple[bool, str]:
    """请求 helper 自行退出。返回 `(ok, 原因)`。

    ## 为什么不是 `TerminateProcess`

    引擎跑在 Medium IL，helper 跑在 High IL。Medium 进程对 High 进程调
    `OpenProcess(PROCESS_TERMINATE)` 会拿到 `err=5` —— **引擎无权杀它**。
    所以"停止"只能是"请求它自己退"，由 helper 侧的 `/v1/shutdown` 关掉自己的
    服务循环。这也是为什么 `available()` 只缓存成功：helper 消失后下一次探测
    必须立刻如实变回不可用。

    幂等：本来就没起 helper（没有端口文件）时返回 `(True, "")` ——
    "它已经不在跑了"和"刚把它停了"对调用方是同一件事。
    """
    port = _read_port()
    if not port:
        reset_probe()
        return True, ""

    key, _warn = ensure_key()
    if not key:
        return False, "无法建立 helper 共享密钥"

    conn = http.client.HTTPConnection(proto.LOOPBACK, port, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/v1/shutdown",
            body=b"",
            headers={
                "Content-Length": "0",
                proto.SIGNATURE_HEADER: proto.sign(key, b""),
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            detail = raw[:200].decode("utf-8", errors="replace")
            return False, f"helper 拒绝退出（HTTP {resp.status}）：{detail}"
    except (OSError, ValueError) as exc:
        # 连不上通常意味着它已经退出了（端口文件是残留）。
        reset_probe()
        _clear_port_file()
        return True, f"helper 已不在运行（{type(exc).__name__}）"
    finally:
        conn.close()

    # 等它真的退出：`shutdown()` 之后 `serve_forever()` 才会返回并清端口文件。
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _read_port():
            break
        time.sleep(0.2)

    reset_probe()
    _clear_port_file()
    return True, ""


# ── 执行 ─────────────────────────────────────────────────


def helper_exec(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_output: int,
) -> LaunchResult:
    """把请求交给 helper，由它用受限令牌起进程。

    失败一律返回 `spawn_error` 非空的 `LaunchResult` —— 调用方据此**拒绝执行**，
    绝不退回无隔离路径。
    """
    key, _warn = ensure_key()
    if not key:
        return LaunchResult(None, "", "", "无法建立 helper 共享密钥")
    port = _read_port()
    if not port:
        return LaunchResult(None, "", "", "提权 helper 未启动")

    payload = json.dumps(
        {
            "argv": list(argv),
            "cwd": cwd,
            "env": dict(env),
            "timeout": float(timeout),
            "max_output": int(max_output),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    signature = proto.sign(key, payload)

    # 客户端超时要**大于**服务端执行超时：否则 helper 还在正常执行，引擎先断开，
    # 结果是一个"看起来超时、其实命令成功了"的假故障。
    client_timeout = min(timeout, 300.0) + 15.0
    conn = http.client.HTTPConnection(proto.LOOPBACK, port, timeout=client_timeout)
    try:
        conn.request(
            "POST",
            "/v1/exec",
            body=payload,
            headers={
                "Content-Type": "application/json",
                proto.SIGNATURE_HEADER: signature,
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            detail = raw[:500].decode("utf-8", errors="replace")
            return LaunchResult(None, "", "", f"helper HTTP {resp.status}: {detail}")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        # helper 中途消失：清掉缓存，让下一次调用重新探测而不是继续撞死端口。
        reset_probe()
        return LaunchResult(None, "", "", f"helper 调用失败: {type(exc).__name__}: {exc}")
    finally:
        conn.close()

    return LaunchResult(
        exit_code=data.get("exit_code"),
        stdout=data.get("stdout") or "",
        stderr=data.get("stderr") or "",
        spawn_error=data.get("spawn_error") or "",
    )


# ── 启动（UAC） ──────────────────────────────────────────


def helper_argv(parent_pid: int, state_dir: str | None = None) -> list[str]:
    """helper 的命令行。

    用 `-m` 而不是文件路径：helper 由**提权**进程启动，其工作目录由 UAC 决定，
    文件路径会解析到别处。`-m` 靠的是**子进程 cwd 在 `sys.path` 里**，
    所以调用方必须把 `working_dir` 指到引擎根（见 `spawn_elevated`）——
    光拼出这行 argv 并不足以让 `app` 包解析成功。

    `--state-dir` 是**必需**的：标准用户（非管理员）走 UAC 时必须输入另一个
    管理员账户的凭据，helper 因此在**另一个账户**下运行，它的 `%LOCALAPPDATA%`
    不是引擎的那个 —— 读不到引擎写好的共享密钥，`serve()` 直接返回 2，
    引擎侧只看到"helper 已启动但未就绪"。把引擎自己的状态目录显式传过去，
    这条路上的两个账户才共享同一个密钥/端口文件。

    参数里只有**路径**，没有密钥：密钥依旧只能从那个目录读，而那个目录本来
    就只有引擎所在用户可写，安全边界不变（见 `proto.helper_state_dir`）。
    """
    argv = [
        sys.executable,
        "-m",
        "app.isolation.elevated_helper",
        f"--parent-pid={parent_pid}",
    ]
    if state_dir:
        argv.append(f"--state-dir={state_dir}")
    return argv


def spawn_elevated(parent_pid: int | None = None) -> tuple[bool, str, bool]:
    """以管理员权限启动 helper（触发 UAC）。返回 `(ok, 原因, 是否用户取消)`。

    **只应由用户显式操作触发**（设置页按钮 → 引擎接口）。引擎自己在启动时静默
    弹 UAC 是不可接受的：用户不知道自己在同意什么，而同意的是"提权执行通道"。

    实现用 `ShellExecuteW(..., "runas", ...)` 而不是 `subprocess`：
    `subprocess` 无法请求提权，只能继承当前（未提权）令牌 —— 那样起出来的
    helper 依然 `err=5`，白弹一次 UAC。

    第三个返回值单独标出"用户取消了 UAC"：这是**用户的正常选择**，不是故障。
    界面必须把它与"真的起不来"分开说 —— 一律报"启用失败"，用户会以为程序坏了，
    然后反复重试同一个他刚刚主动拒绝的弹窗。
    """
    if not win.IS_WINDOWS:
        return False, "非 Windows 平台", False
    pid = parent_pid if parent_pid is not None else os.getpid()
    key, warn = ensure_key()
    if not key:
        return False, "无法建立 helper 共享密钥", False

    argv = helper_argv(pid, str(proto.helper_state_dir()))
    # 提权进程的工作目录由 UAC 决定，所以显式指到引擎根，`-m app...` 才解析得到。
    working_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    # `lpParameters` 只放**参数**，可执行文件本身走 `lpFile`。`list2cmdline` 负责
    # 加引号 —— `C:\Program Files\Chiron` 这类带空格的路径必需。
    parameters = subprocess.list2cmdline(argv[1:])

    try:
        import ctypes
        import ctypes.wintypes  # noqa: F401 — 不是自动导入的子模块

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        shell32.ShellExecuteW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        # nShowCmd=0 即 SW_HIDE：helper 是后台服务，不该弹窗抢焦点。
        # 需要排障时由用户手动以 --verbose 在前台运行。
        result = shell32.ShellExecuteW(
            None, "runas", argv[0], parameters, working_dir, 0
        )
    except Exception as exc:  # noqa: BLE001 — 起不来要如实回传
        return False, f"启动提权 helper 失败: {type(exc).__name__}: {exc}", False

    # ShellExecuteW 返回值 >32 表示成功；<=32 是错误码（5 = 用户拒绝 UAC）。
    code = int(result or 0)
    if code <= 32:
        # 用户点"否"在**返回值**上的表现是 5（SE_ERR_ACCESSDENIED / 拒绝访问），
        # 不适配 UAC 策略时也可能是 `ERROR_CANCELLED(1223)` —— 但后者只出现在
        # `GetLastError` 里，绝不会出现在返回值上（>32 一律按成功解释）。
        # 所以两个来源都看，但都只在失败分支里判：把 1223 当返回值判会把一次
        # 失败读成成功。
        cancelled = code == 5
        if not cancelled:
            try:
                cancelled = int(ctypes.get_last_error()) == 1223
            except Exception:  # noqa: BLE001 — 取不到 last error 就按"非取消"
                cancelled = False
        hint = "（用户取消了 UAC 授权）" if cancelled else ""
        return False, f"启动提权 helper 被拒绝，ShellExecuteW={code}{hint}", cancelled

    reset_probe()
    return True, "", False


def wait_ready(timeout: float = 20.0) -> tuple[bool, str]:
    """等 helper 就绪（UAC 同意后它需要一点时间绑定端口）。"""
    import time

    deadline = time.monotonic() + timeout
    reason = ""
    while time.monotonic() < deadline:
        ok, why = available()
        if ok:
            return True, ""
        reason = why
        time.sleep(0.4)
    return False, reason or "提权 helper 启动超时"


__all__ = [
    "available",
    "ensure_key",
    "helper_argv",
    "helper_exec",
    "reset_probe",
    "spawn_elevated",
    "stop",
    "unavailable_reason",
    "wait_ready",
]
