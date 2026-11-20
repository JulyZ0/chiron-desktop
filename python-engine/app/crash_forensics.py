"""引擎异常退出取证：把"崩了"变成一份可读的现场文件。

## 为什么需要这个模块

真机事故的形态是：界面上的引擎**直接消失**，Electron 的 `main.log` 只留下

    python-engine 运行中退出（退出码 1073807364）

一行。`1073807364` = `0x40010004`（`DBG_TERMINATE_PROCESS`）—— 这是 Windows 上
`TerminateProcess`（Job Object 收树、超时强杀）的退出码，**不是** Python 异常，
所以既没有 traceback，也没有栈。用户与反馈渠道能拿到的证据到此为止。

因此这里做三件事，且都**不依赖日志系统**（日志本身可能就是坏掉的那一环）：

1. **Python 层异常**走 `sys.excepthook` / `threading.excepthook` / asyncio 未处理异常
   钩子，落一份 `engine-crash-<ts>.log`：退出类型、Python/平台版本、活跃 run 与会话、
   最近 20 条事件摘要、关键 env 的**名字**（绝不写值）。
2. **原生崩溃**（段错误 / abort / 栈溢出）走 `faulthandler`，写
   `engine-faulthandler.log`。它必须拿到一个真实的 fileno 才能开，因此**不能**用
   每次启动都新建的指纹文件：那会在每个正常启动的进程里留下一个空文件，而"原子写"
   用的 `os.replace` 又会把 faulthandler 已经握住的 fd 摘掉（改名后 fd 指向被替换掉
   的旧文件，之后的 dump 全部丢失）。一个稳定的追加文件没有这两个问题。
3. **正常退出**（atexit / SIGTERM / SIGINT）写 `engine-exit.log` 一行
   `{ts, reason, code, uptime_s}`。它是崩溃取证的**对照组**：没有这一行却看到
   "运行中退出"，说明进程是被外力杀的（Job Object / TerminateProcess），
   与代码里的未捕获异常无关 —— 这正是 `1073807364` 那种情况的判据。

## 落盘目录

优先级（第一个可用者）：

1. 环境变量 `CHIRON_CRASH_LOG_DIR`（测试与排障用，避免把用例写到真实用户目录）；
2. `settings.log_dir`（桌面壳在 `buildEngineEnv` 里下发的 `LOG_DIR`，打包态是
   `<dataDir>/logs`）；
3. `<settings.data_dir>/logs`；
4. `%LOCALAPPDATA%/chiron/logs`（非 Windows 用 `~/.local/share/chiron/logs`）。

## 写入约定

- 报告用**原子写**（同目录临时文件 + `os.replace`），半截文件比没有文件更糟：
  Electron 侧会把最后一行当作"错误摘要"透传给用户。
- **任何一步失败都不阻断主流程**：取证代码自己抛异常会把"崩溃"变成"崩溃 + 二次异常"，
  而且发生在解释器已处于异常状态的时候。所有入口都包在 `try/except Exception` 里。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 报告里保留的事件条数。20 条足够覆盖"一条消息的完整回合"，再多多半是噪声。
EVENT_TAIL = 20
#: 内存里保留的事件总数（报告只取尾部 20 条）。
EVENT_BUFFER = 200
#: 单个事件字段值的截断长度。
_FIELD_LIMIT = 160
#: 报告里单个会话条目的截断长度（会话 id 是 UUID，这里只为防超长注入）。
_SESSION_LIMIT = 120

#: 关键 env 的**名字**白名单。只记录"是否已配置"，永不写值：
#: 这些键里有的是凭据（`APP_SECRET` / `*_API_KEY`），有的含 DSN 内嵌口令
#: （`POSTGRES_DSN`），一旦进了崩溃日志就等于把密钥写到了磁盘上并可能被用户随手贴出去。
KEY_ENV_NAMES = (
    "APP_SECRET",
    "POSTGRES_DSN",
    "DATABASE_URL",
    "REDIS_URL",
    "INTERNAL_TOKEN",
    "JWT_SECRET",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LOG_DIR",
    "DATA_DIR",
    "CHIRON_DATA_DIR",
    "GATEWAY_INTERNAL_URL",
    "KNOWLEDGE_SERVICE_URL",
    "SANDBOX_BACKEND",
    "SANDBOX_ROOT",
    "CHIRON_RESOURCES_DIR",
    "HTTP_HOST",
    "HTTP_PORT",
    "DEGRADED_MODE",
    "OTEL_ENDPOINT",
)

#: 事件字段里出现这些字样时按凭据处理（值一律替换为 `<redacted>`）。
_REDACT_HINTS = ("secret", "token", "password", "passwd", "apikey", "api_key", "dsn")

# ── 模块状态 ──

_installed = False
_start_monotonic = time.monotonic()
_crash_dir: Path | None = None
_faulthandler_file: Any = None
_lock = threading.Lock()
#: 我们装过的信号处理者（按信号编号）：用来判断"当前最外层的还是不是我们"。
_signal_handlers: dict[int, Any] = {}

#: 最近事件环形缓冲（多线程 emit，报告在异常路径上读）。
_events: deque[dict[str, str]] = deque(maxlen=EVENT_BUFFER)

#: 进程内登记的活跃 run（`main.py` 之外的路径也能用；`app.main._ACTIVE_RUNTIMES`
#: 是权威来源，这里只作补充）。
_active_runs: dict[str, str] = {}

#: 正常退出的收尾状态：`atexit` 读它决定 `engine-exit.log` 那一行怎么写。
_exit_state: dict[str, Any] = {"reason": "atexit", "code": None, "signal": None}
_exit_written = False


# ── 目录解析 ──


def crash_log_dir() -> Path:
    """崩溃/退出日志目录（见模块文档的优先级）。"""
    global _crash_dir
    if _crash_dir is not None:
        return _crash_dir

    override = os.environ.get("CHIRON_CRASH_LOG_DIR", "").strip()
    if override:
        return Path(override)

    log_dir = ""
    data_dir = ""
    try:
        # 延迟导入：本模块要在"引擎还没起来"时就能用，不能被重型依赖拖住。
        from app.config import settings as _settings

        log_dir = str(getattr(_settings, "log_dir", "") or "").strip()
        data_dir = str(getattr(_settings, "data_dir", "") or "").strip()
    except Exception:  # pragma: no cover — 配置坏了也要能取证
        log_dir = data_dir = ""

    if log_dir:
        return Path(log_dir)
    if data_dir:
        return Path(data_dir) / "logs"
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "chiron" / "logs"
    # 非 Windows / 没有 LOCALAPPDATA：退回 XDG 口径的本地数据目录。
    return Path.home() / ".local" / "share" / "chiron" / "logs"


def _ensure_dir() -> Path:
    d = crash_log_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 原子写 ──


def _atomic_write(path: Path, text: str) -> bool:
    """同目录临时文件 + `os.replace`。失败返回 False，**绝不抛**。"""
    tmp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".engine-crash-", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
        return True
    except Exception:  # pragma: no cover — 磁盘满/权限/杀软锁文件
        logger.warning("crash report write failed: %s", path, exc_info=True)
        return False
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ── 事件摘要 ──


def _redact(key: str, value: Any) -> str:
    lowered = key.lower()
    if any(hint in lowered for hint in _REDACT_HINTS):
        return "<redacted>"
    text = str(value)
    if len(text) > _FIELD_LIMIT:
        text = text[:_FIELD_LIMIT] + "…"
    # 换行会破坏"一行一条事件"的格式，而报告要能被 `tail -1` 直接读懂。
    return text.replace("\r", " ").replace("\n", " ")


def record_event(kind: str, **fields: Any) -> None:
    """登记一条事件摘要（供崩溃报告取尾部 20 条）。失败不阻断。"""
    try:
        parts = [f"{k}={_redact(k, v)}" for k, v in fields.items() if v is not None]
        line = f"{datetime.now(UTC).isoformat(timespec='milliseconds')} {kind}"
        if parts:
            line += " " + " ".join(parts)
        with _lock:
            _events.append({"line": line})
    except Exception:  # pragma: no cover — 取证不能影响业务路径
        pass


def set_active_run(session_id: str, run_token: str = "") -> None:
    """登记一个活跃 run（崩溃报告会列出它）。"""
    try:
        sid = str(session_id or "").strip()
        if not sid:
            return
        with _lock:
            _active_runs[sid] = str(run_token or "")
    except Exception:  # pragma: no cover
        pass


def clear_active_run(session_id: str) -> None:
    try:
        with _lock:
            _active_runs.pop(str(session_id or "").strip(), None)
    except Exception:  # pragma: no cover
        pass


def _active_scope() -> list[str]:
    """活跃会话快照：先看 `app.main._ACTIVE_RUNTIMES`，再看进程内登记表。

    只读 `sys.modules` 里**已经导入**的 `app.main`：在异常钩子里再 import 一次
    入口模块会触发循环导入/二次初始化，而取证代码绝不能再制造新故障。
    """
    seen: dict[str, str] = {}
    try:
        main_mod = sys.modules.get("app.main")
        runtimes = getattr(main_mod, "_ACTIVE_RUNTIMES", None) if main_mod else None
        if isinstance(runtimes, dict):
            for sid, value in list(runtimes.items())[:50]:
                token = ""
                if isinstance(value, tuple) and len(value) >= 3:
                    token = str(value[2] or "")[:8]
                seen[str(sid)] = token
    except Exception:  # pragma: no cover
        pass
    try:
        with _lock:
            for sid, token in list(_active_runs.items())[:50]:
                seen.setdefault(sid, str(token or "")[:8])
    except Exception:  # pragma: no cover
        pass
    return [f"session={sid} run={token or '-'}" for sid, token in seen.items()]


# ── 报告拼装 ──


def _safe_getenv_names() -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    for name in KEY_ENV_NAMES:
        if os.environ.get(name):
            present.append(name)
        else:
            missing.append(name)
    return present, missing


def _safe_argv0() -> str:
    """只留可执行文件名 —— argv 里可能带 `--api-key=` 之类的参数。"""
    try:
        return Path(sys.argv[0] or "").name or "-"
    except Exception:  # pragma: no cover
        return "-"


def build_report(kind: str, *, code: Any = None, signal_name: str = "", tb_text: str = "") -> str:
    """拼一份崩溃报告（纯函数，便于用例断言"不含敏感值"）。"""
    lines: list[str] = [
        "=== Chiron python-engine crash report ===",
        f"time: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"pid: {os.getpid()}",
        f"exit-type: {kind}",
        f"exit-code: {code if code is not None else '-'}",
        f"signal: {signal_name or '-'}",
        f"uptime-s: {round(time.monotonic() - _start_monotonic, 3)}",
        f"python: {sys.version.splitlines()[0] if sys.version else '-'}",
        f"platform: {sys.platform}",
        f"argv0: {_safe_argv0()}",
    ]
    try:
        present, missing = _safe_getenv_names()
        lines.append("env-set(names-only): " + (", ".join(present) if present else "-"))
        lines.append("env-unset(names-only): " + (", ".join(missing) if missing else "-"))
    except Exception:  # pragma: no cover
        lines.append("env-set(names-only): -")

    try:
        scope = _active_scope()
        lines.append("active-runs: " + ("; ".join(scope) if scope else "-"))
    except Exception:  # pragma: no cover
        lines.append("active-runs: -")

    lines.append(f"--- last {EVENT_TAIL} events ---")
    try:
        with _lock:
            tail = list(_events)[-EVENT_TAIL:]
        if tail:
            lines.extend(item.get("line", "")[:_FIELD_LIMIT * 2] for item in tail)
        else:
            lines.append("-")
    except Exception:  # pragma: no cover
        lines.append("-")

    if tb_text:
        lines.append("--- traceback ---")
        lines.append(tb_text.rstrip("\n"))
    lines.append("=== end ===")
    return "\n".join(lines) + "\n"


def write_report(kind: str, *, code: Any = None, signal_name: str = "", tb_text: str = "") -> str:
    """落盘一份崩溃报告，返回文件路径（失败返回空串）。绝不抛。"""
    try:
        d = _ensure_dir()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = d / f"engine-crash-{stamp}.log"
        if path.exists():
            # 同一秒内二次崩溃（异常钩子 + 线程钩子）也不能互相覆盖：报告是证据。
            path = d / f"engine-crash-{stamp}-{os.getpid()}.log"
        text = build_report(kind, code=code, signal_name=signal_name, tb_text=tb_text)
        if not _atomic_write(path, text):
            return ""
        logger.error("engine crash report written: %s (type=%s)", path, kind)
        return str(path)
    except Exception:  # pragma: no cover
        return ""


# ── 钩子安装 ──


def _format_tb(exc_type: Any, exc_value: Any, exc_tb: Any) -> str:
    try:
        return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:  # pragma: no cover
        return ""


def _install_excepthook() -> None:
    prev = sys.excepthook

    def hook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        try:
            # KeyboardInterrupt 是"用户/系统让它停"，不是崩溃：正常退出由 atexit 记一行。
            if not issubclass(exc_type, KeyboardInterrupt):
                write_report(
                    "uncaught-exception",
                    tb_text=_format_tb(exc_type, exc_value, exc_tb),
                )
        except Exception:  # pragma: no cover
            pass
        try:
            if prev is not None and prev is not hook:
                prev(exc_type, exc_value, exc_tb)
        except Exception:  # pragma: no cover
            pass

    sys.excepthook = hook


def _install_thread_hook() -> None:
    if not hasattr(threading, "excepthook"):
        return
    prev = threading.excepthook

    def hook(args: Any) -> None:
        try:
            exc_type = getattr(args, "exc_type", None)
            if exc_type is not None and not issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
                write_report(
                    "thread-uncaught",
                    tb_text=_format_tb(
                        exc_type,
                        getattr(args, "exc_value", None),
                        getattr(args, "exc_traceback", None),
                    )
                    + f"thread: {getattr(args, 'thread', None)}\n",
                )
        except Exception:  # pragma: no cover
            pass
        try:
            if prev is not None and prev is not hook:
                prev(args)
        except Exception:  # pragma: no cover
            pass

    threading.excepthook = hook


def install_asyncio_hook(loop: Any = None) -> bool:
    """给事件循环挂"未处理异常"钩子。返回是否装上（拿不到 loop 时 False）。

    未处理的任务异常**不等于进程退出**，但它是"引擎行为异常"最接近真因的证据
    （比如某次工具调用在后台任务里炸了、回合永远停在 running）。报告里会写
    `exit-type: asyncio-unhandled`，进程继续运行。
    """
    import asyncio

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except Exception:
            try:
                loop = asyncio.get_event_loop()
            except Exception:
                return False

    try:
        prev = loop.get_exception_handler()

        def handler(inner_loop: Any, context: dict[str, Any]) -> None:
            try:
                exc = context.get("exception")
                if exc is not None and not isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    write_report(
                        "asyncio-unhandled",
                        tb_text="".join(
                            traceback.format_exception(type(exc), exc, exc.__traceback__)
                        )
                        + f"loop-message: {context.get('message', '')}\n",
                    )
            except Exception:  # pragma: no cover
                pass
            try:
                if callable(prev):
                    prev(inner_loop, context)
            except Exception:  # pragma: no cover
                pass

        loop.set_exception_handler(handler)
        return True
    except Exception:  # pragma: no cover
        return False


def install_signal_handlers() -> None:
    """挂 SIGTERM / SIGINT / SIGBREAK：只**记录**退出原因，然后照旧交给上一个处理者。

    必须**链式**而不是覆盖：uvicorn 自己装了 SIGTERM/SIGINT 处理者做优雅关停
    （置 `should_exit`、等 lifespan 收尾）。覆盖它会让 `stopEngine()` 的 SIGTERM
    变成"没有优雅关停"，PG 连接、队列排空全部被跳过 —— 那正是这个模块要取证的事故
    的另一种成因。因此：先记 reason，再调 `prev`。

    为什么单独一个函数、并且**允许重复调用**：`uvicorn.run()` 在 `Server.run()` 里
    **后**装处理者，会盖掉 `main()` 阶段装的这一份。所以 lifespan 启动时（此时 uvicorn
    的处理者已在位）要再调一次，把我们的记录层套在它外面。判据不是"装过没有"，而是
    "当前最外层的处理者是不是我们"：是就跳过，不是就再套一层并链到新前驱。
    """
    try:
        import signal
    except Exception:  # pragma: no cover
        return

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            prev = signal.getsignal(sig)
        except Exception:  # pragma: no cover
            continue
        if prev is _signal_handlers.get(int(sig)):
            # 已经是我们的（且之后没人再覆盖过）：重复调用到此为止
            continue

        def handler(signum: int, frame: Any, _prev: Any = prev, _name: str = name) -> None:
            _exit_state.update(
                {
                    "reason": f"signal:{_name}",
                    "code": int(signum),
                    "signal": _name,
                }
            )
            if callable(_prev):
                _prev(signum, frame)
                return
            # 没有可链的处理者（SIG_DFL/SIG_IGN）：保持"收到信号会退出"的语义，
            # 走 SystemExit 让 atexit 把 `engine-exit.log` 写下最后一行。
            raise SystemExit(128 + int(signum))

        try:
            signal.signal(sig, handler)
            _signal_handlers[int(sig)] = handler
        except Exception:  # pragma: no cover — 非主线程 / 平台不支持
            continue


def _install_faulthandler() -> None:
    """原生崩溃（SIGSEGV/SIGABRT/符号栈溢出）的兜底 dump。"""
    global _faulthandler_file
    try:
        import faulthandler

        d = _ensure_dir()
        path = d / "engine-faulthandler.log"
        # 追加 + 行缓冲：dump 由 faulthandler 直接写 fd，Python 侧缓冲会导致丢内容。
        fh = path.open("a", encoding="utf-8", errors="replace", buffering=1)
        fh.write(
            f"--- boot {datetime.now(UTC).isoformat(timespec='seconds')} "
            f"pid={os.getpid()} python={sys.version.split()[0]} ---\n"
        )
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
        _faulthandler_file = fh
    except Exception:  # pragma: no cover — 只读目录等
        logger.warning("faulthandler install failed", exc_info=True)


def _install_atexit() -> None:
    import atexit

    def _on_exit() -> None:
        global _exit_written
        if _exit_written:
            return
        _exit_written = True
        try:
            d = _ensure_dir()
            record = {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "reason": _exit_state.get("reason") or "atexit",
                "code": _exit_state.get("code"),
                "signal": _exit_state.get("signal"),
                "uptime_s": round(time.monotonic() - _start_monotonic, 3),
            }
            line = json.dumps(record, ensure_ascii=False)
            with _lock:
                with (d / "engine-exit.log").open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
        except Exception:  # pragma: no cover — 退出路径上不允许再抛
            pass

    atexit.register(_on_exit)


def install(*, crash_dir: str | os.PathLike[str] | None = None) -> None:
    """装齐取证钩子（幂等）。在 `create_app()` / `main()` 最早期调用。"""
    global _installed, _crash_dir
    if _installed:
        return
    _installed = True
    if crash_dir is not None:
        _crash_dir = Path(crash_dir)
    # 目录尽早建出来：真崩的时候 mkdir 也可能失败（只读盘），早失败早告警。
    try:
        _ensure_dir()
    except Exception:  # pragma: no cover
        logger.warning("crash log dir unavailable: %s", crash_log_dir(), exc_info=True)
    _install_excepthook()
    _install_thread_hook()
    _install_atexit()
    _install_faulthandler()
    install_signal_handlers()
    record_event("forensics-installed", dir=str(crash_log_dir()))


def run_guarded(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """执行入口函数，捕获 `SystemExit` 并取证，然后原样向上抛。

    `sys.excepthook` **收不到** `SystemExit`：解释器把它当作"正常退出"直接处理。
    而真机上 `sys.exit(非零)` 恰恰是最常见的"无声退出"成因（uvicorn 起不来、
    前置校验失败走的就是这条路），所以入口必须自己包一层。
    退出码为 0 / None 视为正常退出，只写 `engine-exit.log`。
    """
    try:
        return fn(*args, **kwargs)
    except SystemExit as exc:
        code = exc.code
        # 先判定再改状态：`engine-exit.log` 要记下真实退出码，而崩溃报告的判据
        # 依赖"此刻还没被标成 system-exit"。
        is_crash = code not in (None, 0) and _exit_state.get("reason") == "atexit"
        _exit_state["code"] = code
        if is_crash:
            _exit_state["reason"] = "system-exit"
            try:
                write_report("system-exit", code=code, tb_text=_format_tb(*sys.exc_info()))
            except Exception:  # pragma: no cover
                pass
        raise
