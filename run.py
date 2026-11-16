#!/usr/bin/env python3
"""
chiron 一键运行脚本
支持 Linux / Windows / macOS

用法:
    python run.py start          # 启动所有服务
    python run.py start --bg     # 后台启动所有服务
    python run.py stop           # 停止所有服务
    python run.py restart        # 重启所有服务
    python run.py status         # 查看服务状态
    python run.py logs           # 查看日志
    python run.py build          # 编译 Go 服务
    python run.py setup          # 首次安装依赖
"""

import argparse
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()
PID_DIR = BASE_DIR / ".pids"
LOG_DIR = BASE_DIR / "logs"
WORKSPACE_DIR = BASE_DIR / "workspace"

SERVICES = {
    "gateway": {
        "name": "Go Gateway",
        "port": 8080,
        "cmd": [str(BASE_DIR / ("chiron.exe" if platform.system() == "Windows" else "chiron"))],
        "env": {
            "PORT": "8080",
            "STORAGE_BACKEND": "local",
            "STORAGE_ROOT": str(WORKSPACE_DIR),
        },
    },
    "xerj": {
        # XERJ 索引节点（可选）：只读代码/文档检索引擎，agent 经 MCP（`xerj mcp`）消费。
        # 它不是 agent —— 没有规划与工具调用循环，只提供符号级检索（见答复里的定位分析）。
        #
        # **optional=True**：二进制不在 PATH 上时**跳过**而不是让 start 整体失败 ——
        # 它是检索增强，不是运行前提。安装：https://xerj.org/docs/install
        #
        # 安全：`--insecure` 关闭鉴权（本地开发免 admin.key），因此**必须**配合 loopback
        # 绑定；xerj 自身也会拒绝 `--insecure` + 非 loopback 的组合。数据目录落在
        # 已被 .gitignore 忽略的 data/ 下。
        #
        # 放在 python-engine 之前：引擎的 MCP 连接在启动时建立，节点先就绪才能一次连上
        # （晚就绪也能靠 pool 轮询自愈，但首次会先报一轮失败）。
        #
        # **必须带 `-c`**：xerj 只认 `--config` 或 `XERJ_CONFIG`，**不会**自动读
        # `./xerj.toml`。而它的端口只能从 TOML 配（命令行没有端口开关），默认
        # native REST = 8080 与 Go 网关撞车 —— 见 xerj.toml 里的说明。
        "name": "XERJ 索引节点",
        "port": 9200,
        "cmd": [
            "xerj",
            "--insecure",
            "--bind", "127.0.0.1",
            "--config", str(BASE_DIR / "xerj.toml"),
            "--data-dir", str(BASE_DIR / "data" / "xerj"),
        ],
        "env": {},
        "optional": True,
    },
    "python-engine": {
        "name": "Python AI 引擎",
        "port": 8000,
        # `-u`（无缓冲）**不是可选的**：日志被重定向到文件时 stdout 是块缓冲，
        # 运行时日志会一直卡在进程内存里（实测 logs/python-engine.stdout.log 长期 0 字节），
        # 排查线上问题时等于"引擎没有日志" —— 只能靠 DB/Redis 反推。
        "cmd": [sys.executable, "-u", "-m", "app.main"],
        "cwd": str(BASE_DIR / "python-engine"),
        "env": {
            "HTTP_PORT": "8000",
        },
    },
    "mcp-broker": {
        # 独立进程：**唯一**持有危险 MCP server 连接与凭据的地方。
        # 引擎对未声明 read_only 的 server 不直连（app/mcp/client.py 的连接守卫），
        # 改由它执行 —— 凭据不出这个进程，引擎即使被注入也读不到。
        # 它执行前仍会向 Go 的 Tool Broker 要授权（判定权威在服务端）。
        "name": "MCP Broker",
        "port": 8001,
        "cmd": [sys.executable, "-u", "mcp_broker.py"],
        "cwd": str(BASE_DIR / "python-engine"),
        "env": {
            "MCP_BROKER_PORT": "8001",
        },
    },
}

DEFAULT_ENV = {}


def load_env_file():
    """从 .env 文件加载环境变量，覆盖 DEFAULT_ENV 中的默认值"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # 只覆盖 DEFAULT_ENV 中已有的键，或添加新的环境变量
            if key:
                DEFAULT_ENV[key] = value


# 启动时加载 .env 文件
load_env_file()

# ── 档位与自适应（多配置 + 参数选环境 + 缺失时自动检测）─────────────────────
#
# 同一个 .env 被 compose（容器主机名 postgres / sandbox-lb）与 run.py（本机直跑）共用，
# 而容器名在本机解析不到 ⇒ 网关与引擎双双 fail-closed 拒绝启动。详见 local_env_profile.py。
#
# **这里只定义、不执行**：档位来自 `--env` 命令行参数，而 argparse 在 main() 里、晚于模块级
# 代码。原先在模块级直接应用，导致 `--env` 永远取不到值（只能退回环境变量）。


def _apply_env_profile(profile: str | None) -> None:
    """应用档位与自适应。必须在 **argparse 之后、任何服务启动之前** 调用。

    `_build_env` 取的是 `DEFAULT_ENV`，所以只要在起进程前应用即可（status / logs 之类
    不读 env 的命令不调用它 —— 见 main() 里按命令收窄的那一段）。
    """
    try:
        # 显式把项目根放进 sys.path：`local_env_profile` 就在 run.py 旁边，但 sys.path[0]
        # 取决于**怎么调用**（`python run.py` 是脚本目录，`runpy` / `-m` 则可能是别处）。
        # 不保底的话 import 会静默失败 → 档位与自适应不生效 → 直接退回"容器地址在本机
        # 解析不了"的原始故障，且现场几乎看不出原因。
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        from local_env_profile import apply_profile

        for note in apply_profile(BASE_DIR, DEFAULT_ENV, profile=profile):
            print(f"  [env] {note}")
    except Exception as exc:  # noqa: BLE001
        # **显式指定的档位**出问题 ⇒ 拒绝启动：用户明确要求了它，静默忽略会让他以为生效了，
        # 实际却拿另一套配置去起服务 —— 最坏的情况是"看着起来了、连的却是别的库"。
        # 自动发现（profile 为 None）出问题则只提示、不阻塞：那是"尽力而为"。
        if profile:
            print(f"  [env] 档位 {profile!r} 无法加载：{exc}")
            raise SystemExit(2) from exc
        print(f"  [env] 档位/自适应跳过: {exc}")


# ── 工具函数 ──────────────────────────────────────────────────

def is_windows() -> bool:
    return platform.system() == "Windows"


def is_port_open(port: int, host: str = "localhost", timeout: float = 1.0) -> bool:
    """检查端口是否在监听"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def gateway_needs_rebuild(exe_path: Path) -> bool:
    """Go 网关源码（cmd/chiron + internal）是否比编译产物新。

    run.py start 只在 exe 缺失时自动构建；改动 Go 源码后若沿用旧 exe，
    会启动过期版本（例如数据库降级/安装模式修复未生效）。这里对比 mtime，
    任一 .go 文件比 exe 新则返回 True（提示重建）。
    """
    if not exe_path.exists():
        return True
    exe_mtime = exe_path.stat().st_mtime
    for root in (BASE_DIR / "cmd" / "chiron", BASE_DIR / "internal"):
        if not root.exists():
            continue
        try:
            for p in root.rglob("*.go"):
                if p.stat().st_mtime > exe_mtime:
                    return True
        except OSError:
            continue
    return False


def get_pid_file(service: str) -> Path:
    return PID_DIR / f"{service}.pid"


def read_pid(service: str) -> int | None:
    pf = get_pid_file(service)
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except ValueError:
            return None
    return None


def write_pid(service: str, pid: int):
    PID_DIR.mkdir(parents=True, exist_ok=True)
    get_pid_file(service).write_text(str(pid))


def remove_pid(service: str):
    pf = get_pid_file(service)
    if pf.exists():
        pf.unlink()


def is_process_running(pid: int) -> bool:
    """检查进程是否在运行"""
    try:
        if is_windows():
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def kill_process(pid: int):
    """终止进程"""
    try:
        if is_windows():
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            if is_process_running(pid):
                os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        pass


def get_log_file(service: str, stream: str = "stdout") -> Path:
    return LOG_DIR / f"{service}.{stream}.log"


def color(text: str, code: str) -> str:
    """ANSI 颜色输出"""
    if is_windows():
        # Windows 10+ 支持 ANSI
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return text
    return f"\033[{code}m{text}\033[0m"


def green(text: str) -> str:
    return color(text, "32")


def red(text: str) -> str:
    return color(text, "31")


def yellow(text: str) -> str:
    return color(text, "33")


def blue(text: str) -> str:
    return color(text, "34")


def bold(text: str) -> str:
    return color(text, "1")


def gray(text: str) -> str:
    return color(text, "90")


# ── 隔离档位可用性（Windows）──────────────────────────────────
#
# 为什么放在启动脚本而不是只在引擎里判：引擎侧的降级是"静默"的 —— 它写进
# `/v1/system/isolation` 的 reasons 与日志，但用户在终端只会看到"启动成功"。
# 于是出现 README 承诺落空的那类情形：配置写着 restricted，实际跑的是 low_il，
# 而启动输出里没有任何一句话提醒。启动脚本是唯一必定被看到的位置。


def is_elevated() -> bool:
    """当前进程是否持有提权（管理员）令牌。非 Windows 恒为 False。

    argtypes 必须显式声明：`ctypes.windll` 默认按 int 传参，64 位下 HANDLE 会被
    截断成 32 位，`OpenProcessToken` 因此返回 ERROR_INVALID_HANDLE(6) —— 一个
    看起来像"没有权限"、实际是类型声明错的错误。这正是本地探针首次踩的坑。
    """
    if not is_windows():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
        ):
            return False
        try:
            elevated = wintypes.DWORD()
            size = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token, 20, ctypes.byref(elevated),  # TokenElevation
                ctypes.sizeof(elevated), ctypes.byref(size),
            ):
                return False
            return bool(elevated.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 — 判定失败按"未提权"处理，提示偏保守
        return False


def print_isolation_notice() -> None:
    """启动前说明 Windows 隔离档位实际可用到哪一层。

    **不阻断启动**：未提权时降级到 `low_il` 仍在硬底线之上（写入工作区之外
    被拒），不是"没有隔离"，所以没有理由拦住服务。但必须把差距说出来 ——
    让用户以为开了 restricted 而实际没有，比坦白降级糟糕得多。
    """
    if not is_windows():
        return
    if is_elevated():
        print(green("  隔离：已提权，restricted 档（受限令牌 + 独立桌面）可启用"))
        return
    print(yellow("  隔离：当前终端未以管理员身份运行"))
    print(gray("     restricted 档不可用（创建受限进程需要 SeIncreaseQuotaPrivilege），"))
    print(gray("     将降级为 low_il；差距会写进 /v1/system/isolation 的 reasons 与引擎日志。"))
    print(gray("     启用方式：右键终端「以管理员身份运行」后重跑 python run.py start"))


# ── 服务管理 ──────────────────────────────────────────────────

class ServiceManager:
    def __init__(self):
        PID_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        (WORKSPACE_DIR / "skills").mkdir(exist_ok=True)

    def _build_env(self, service_key: str) -> dict:
        """构建服务环境变量"""
        env = os.environ.copy()
        # 排除 vendor 目录判定（原因见 build()）：服务进程内若有 go 调用，
        # 不指定 -mod 同样会被 vendor/ 参考源码目录拖垮。
        goflags = env.get("GOFLAGS", "")
        if "-mod=" not in goflags:
            env["GOFLAGS"] = (goflags + " -mod=mod").strip()
        # 只注入非空默认值：空字符串注入会覆盖 .env / 系统环境中已生效的配置
        # （例如 DEFAULT_ENV 的 JWT_SECRET="" 会覆盖 .env 的 JWT_SECRET，
        # 导致 python-engine 的 pydantic 校验拒绝启动）
        env.update({k: v for k, v in DEFAULT_ENV.items() if v})
        if service_key in SERVICES:
            env.update(SERVICES[service_key].get("env", {}))
        return env

    def build(self):
        """编译 Go 服务"""
        print(bold("编译 Go Gateway..."))

        try:
            # -mod=mod：排除 vendor 目录判定。本仓库的 vendor/ 下放的是参考源码
            # （vendor/DeepSeek-Reasonix），没有 Go 依赖清单 vendor/modules.txt，
            # 而 Go 只要看到 vendor 目录就会按 vendor 模式做一致性检查并直接报错
            # （inconsistent vendoring）。显式指定 -mod 后改走 module cache。
            result = subprocess.run(
                ["go", "build", "-mod=mod", "-o",
                 "chiron.exe" if is_windows() else "chiron",
                 "./cmd/chiron/"],
                cwd=str(BASE_DIR),
                capture_output=True, text=True
            )
        except FileNotFoundError:
            print(red("未找到 go 命令，请先安装 Go (https://go.dev/dl/)"))
            return False

        if result.returncode != 0:
            print(red(f"编译失败:\n{result.stderr}"))
            return False

        print(green("编译成功"))
        return True

    def _find_service_pid(self, key: str, port: int) -> int | None:
        """找到监听指定端口的进程 PID"""
        try:
            if is_windows():
                result = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True
                )
                for line in result.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        if parts:
                            try:
                                return int(parts[-1])
                            except ValueError:
                                pass
            else:
                result = subprocess.run(
                    ["lsof", "-i", f":{port}", "-t"],
                    capture_output=True, text=True
                )
                if result.stdout.strip():
                    return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        return None

    def setup(self):
        """首次安装依赖"""
        print(bold("安装依赖..."))

        # 检查 Go
        try:
            result = subprocess.run(["go", "version"], capture_output=True, text=True)
            print(f"  Go: {result.stdout.strip()}")
        except FileNotFoundError:
            print(red("  Go 未安装，请先安装 Go"))
            return False

        # 检查 Python
        print(f"  Python: {platform.python_version()}")

        # 编译 Go
        if not self.build():
            return False

        # 检查 Python 依赖
        print("  检查 Python 依赖...")
        result = subprocess.run(
            [sys.executable, "-c", "import uvicorn; print('OK')"],
            capture_output=True, text=True
        )
        if "OK" not in result.stdout:
            print(yellow("  安装 Python 依赖..."))
            req_file = BASE_DIR / "python-engine" / "requirements.txt"
            if req_file.exists():
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"],
                    capture_output=True
                )

        print(green("依赖安装完成"))
        return True

    def _start_service(self, key: str, background: bool = True):
        """启动单个服务"""
        svc = SERVICES[key]
        name = svc["name"]
        port = svc["port"]
        cmd = svc["cmd"]
        cwd = svc.get("cwd", str(BASE_DIR))
        env = self._build_env(key)

        # 可选服务：二进制不存在时**跳过并视为成功**。
        # 可选服务是增强项（如 XERJ 检索），不是运行前提 —— 让它把 `start` 的整体
        # 退出码拖成失败，会让"没装就该跳过"变成"没装就起不来"。
        if svc.get("optional") and shutil.which(cmd[0]) is None:
            print(f"  {name}: {gray('已跳过（未安装，可选）')}")
            return True

        # 检查是否已在运行
        pid = read_pid(key)
        if pid and is_process_running(pid):
            print(f"  {name}: {yellow('已在运行')} (PID {pid})")
            return True

        # 检查端口占用
        if is_port_open(port):
            print(f"  {name}: {yellow(f'端口 {port} 已被占用')}")
            return False

        if not background:
            # 前台模式
            print(f"  {name}: {blue('启动中...')} (前台模式, Ctrl+C 停止)")
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env)
                write_pid(key, proc.pid)
                proc.wait()
            except KeyboardInterrupt:
                print(f"\n  {name}: 停止中...")
            finally:
                remove_pid(key)
            return True

        # 后台模式
        stdout_log = get_log_file(key, "stdout")
        stderr_log = get_log_file(key, "stderr")

        if is_windows():
            # Windows: 直接使用 Popen 后台启动，重定向输出到日志文件
            out = open(stdout_log, "w")
            err = open(stderr_log, "w")
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=out, stderr=err,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
                close_fds=True
            )
        else:
            # Linux/macOS: 使用 nohup
            out = open(stdout_log, "w")
            err = open(stderr_log, "w")
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=out, stderr=err,
                start_new_session=True
            )

        write_pid(key, proc.pid)

        # 等待端口就绪；子进程已退出（如依赖缺失/配置错误导致崩溃）时立即报错，避免空等
        for _ in range(20):
            if is_port_open(port):
                # 找到实际的服务进程 PID
                actual_pid = self._find_service_pid(key, port)
                if actual_pid:
                    write_pid(key, actual_pid)
                print(f"  {name}: {green('已启动')} (PID {actual_pid or proc.pid}, 端口 {port})")
                return True
            if proc.poll() is not None:
                print(f"  {name}: {red('启动失败（进程已退出）')} (退出码 {proc.returncode})")
                # 失败时**直接把日志尾部贴出来** —— 否则用户得自己去 logs/ 里翻，
                # 而启动失败的原因几乎总在最后几行（依赖不可用 / 配置错误）。
                self._print_log_tail(key)
                remove_pid(key)
                return False
            time.sleep(1)

        # 端口 20 秒仍未就绪 **不等于成功**：进程可能卡在初始化，或已经进入"拒绝服务"状态
        # 却还没退出。原先这里 `return True` 并打印"已启动但端口未就绪"，是一个**真实的误报点**
        # —— 调用方（含 `restart` 的汇总）会把它当成启动成功。
        print(f"  {name}: {red('启动失败（端口未就绪）')} (PID {proc.pid}, 端口 {port} 在 20s 内未监听)")
        self._print_log_tail(key)
        print(gray(f"  {name}: 进程仍在（PID {proc.pid}），可用 python run.py stop 停止"))
        return False

    def _print_log_tail(self, key: str, lines: int = 12):
        """打印该服务 stdout/stderr 日志的尾部（启动失败时定位原因用）。

        两个文件都看：Go 网关把 FATAL 写 stdout，Python 引擎把 traceback 写 stderr ——
        只看一个会漏。取不到就**安静跳过**（日志缺失不该盖住"启动失败"这个主结论）。
        """
        for kind in ("stdout", "stderr"):
            path = get_log_file(key, kind)
            try:
                if not path.exists() or path.stat().st_size == 0:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            tail = text.splitlines()[-lines:]
            print(gray(f"  ── {path.name} 末尾 {len(tail)} 行 ──"))
            for line in tail:
                print(gray(f"  │ {line}"))

    def stop_service(self, key: str):
        """停止单个服务"""
        svc = SERVICES[key]
        name = svc["name"]

        pid = read_pid(key)
        if not pid:
            print(f"  {name}: {yellow('未运行')}")
            return

        if not is_process_running(pid):
            print(f"  {name}: {yellow('已停止')}")
            remove_pid(key)
            return

        print(f"  {name}: 停止中 (PID {pid})...")
        kill_process(pid)
        time.sleep(1)

        if is_process_running(pid):
            print(f"  {name}: {red('停止失败')}")
        else:
            print(f"  {name}: {green('已停止')}")
        remove_pid(key)

    def start(self, background: bool = True, services: list = None):
        """启动服务"""
        print(bold("\n═══ chiron 服务启动 ═══\n"))

        # 隔离档位可用性：必须在起任何服务之前说，否则用户只会看到"已启动"
        print_isolation_notice()

        targets = services or list(SERVICES.keys())

        # gateway 依赖编译产物 chiron.exe；缺失或源码比产物新时自动构建
        # （README: start 自动构建 Go；避免启动过期版本导致修复未生效）
        if "gateway" in targets:
            exe_path = Path(SERVICES["gateway"]["cmd"][0])
            if gateway_needs_rebuild(exe_path):
                print(yellow("Go Gateway 需要编译（产物缺失或源码已更新）..."))
                if not self.build():
                    return False

        results = {}
        for key in targets:
            if key in SERVICES:
                results[key] = self._start_service(key, background)

        print()

        if background:
            self._print_summary()

        return all(results.values())

    def stop(self, services: list = None):
        """停止服务"""
        print(bold("\n═══ chiron 服务停止 ═══\n"))

        targets = services or list(SERVICES.keys())
        for key in targets:
            if key in SERVICES:
                self.stop_service(key)
        print()

    def restart(self, services: list = None):
        """重启服务"""
        self.stop(services)
        time.sleep(1)
        self.start(services=services)

    def status(self):
        """查看服务状态"""
        print(bold("\n═══ chiron 服务状态 ═══\n"))
        print(f"{'服务':<20} {'状态':<12} {'PID':<10} {'端口':<8} {'端口状态'}")
        print("─" * 70)

        for key, svc in SERVICES.items():
            name = svc["name"]
            port = svc["port"]
            pid = read_pid(key)

            # 可选服务未安装：既不是"运行中"也不是"已停止"，报"未安装"才准确
            # —— 否则用户会以为它该跑却没跑起来，去翻一个根本不存在的日志。
            if svc.get("optional") and shutil.which(svc["cmd"][0]) is None:
                print(f"{name:<20} {gray('未安装'):<12} {'-':<10} {port:<8} {gray('可选')}")
                continue

            if pid and is_process_running(pid):
                port_ok = is_port_open(port)
                status_text = green("运行中")
                pid_text = str(pid)
                port_text = green("监听中") if port_ok else yellow("未就绪")
            else:
                status_text = red("已停止")
                pid_text = "-"
                port_text = gray("未监听") if not is_port_open(port) else yellow("被占用")

            print(f"{name:<20} {status_text:<12} {pid_text:<10} {port:<8} {port_text}")

        print()

    def logs(self, service: str = None, follow: bool = False, tail: int = 50):
        """查看日志"""
        if service:
            self._show_log(service, follow, tail)
        else:
            for key in SERVICES:
                self._show_log(key, follow=False, tail=10)

    def _show_log(self, key: str, follow: bool = False, tail: int = 50):
        """显示单个服务的日志"""
        svc = SERVICES[key]
        name = svc["name"]

        print(bold(f"\n═══ {name} 日志 ═══\n"))

        log_file = get_log_file(key, "stdout")
        if not log_file.exists():
            print(yellow("  暂无日志"))
            return

        if follow:
            # 实时跟踪
            print(f"  跟踪 {log_file} (Ctrl+C 退出)")
            try:
                proc = subprocess.Popen(
                    ["tail", "-f", str(log_file)] if not is_windows() else
                    ["powershell", "-Command", f"Get-Content '{log_file}' -Wait"],
                    stdout=subprocess.PIPE, text=True
                )
                for line in proc.stdout:
                    print(line, end="")
            except KeyboardInterrupt:
                proc.terminate()
        else:
            # 显示最后 N 行
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-tail:]:
                print(f"  {line}")

    def _print_summary(self):
        """打印启动摘要"""
        print(bold("\n═══ 服务访问地址 ═══\n"))
        print(f"  Gateway:      http://localhost:{SERVICES['gateway']['port']}")
        print("  HTTP 引擎:    http://localhost:8000")
        # 可选服务只在真正装了的时候才展示，避免给出一个点不开的地址
        if "xerj" in SERVICES and shutil.which(SERVICES["xerj"]["cmd"][0]):
            print(f"  XERJ 节点:    http://localhost:{SERVICES['xerj']['port']}  (只读检索)")
        print()
        print(f"  日志目录:     {LOG_DIR}")
        print(f"  PID 目录:     {PID_DIR}")
        print()
        print("  停止服务:     python run.py stop")
        print("  查看状态:     python run.py status")
        print("  查看日志:     python run.py logs")
        print()


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="chiron 一键运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run.py setup           首次安装依赖
  python run.py build           编译 Go 服务
  python run.py start           后台启动所有服务
  python run.py start --fg      前台启动（调试用）
  python run.py stop            停止所有服务
  python run.py restart         重启所有服务
  python run.py status          查看服务状态
  python run.py logs            查看所有日志
  python run.py logs gateway    查看 Gateway 日志
        """
    )

    parser.add_argument("command", choices=[
        "setup", "build", "start", "stop", "restart", "status", "logs"
    ], help="命令")

    parser.add_argument("service", nargs="?", help="指定服务 (gateway/python-engine)")
    parser.add_argument("--fg", action="store_true", help="前台模式运行")
    parser.add_argument("--tail", type=int, default=50, help="日志行数")
    parser.add_argument(
        "--env",
        default=os.environ.get("CHIRON_ENV") or None,
        metavar="NAME",
        help="配置档位：读取 .env.NAME（缺省时自动发现 .env.local，没有才退回环境自适应）",
    )

    args = parser.parse_args()

    # 档位与自适应只在**会用到 DEFAULT_ENV 的命令**上应用：start / restart / build。
    # status / stop / logs 只读 PID 文件与日志、不读 env，对它们做档位既无意义，
    # 又会往本应干净的输出里插一行 `[env] …`（没有 .env.local 时还会白跑一轮 TCP 探测）。
    if args.command in ("start", "restart", "build"):
        _apply_env_profile(args.env)

    mgr = ServiceManager()

    services = [args.service] if args.service else None

    if args.command == "setup":
        success = mgr.setup()
        sys.exit(0 if success else 1)

    elif args.command == "build":
        success = mgr.build()
        sys.exit(0 if success else 1)

    elif args.command == "start":
        success = mgr.start(background=not args.fg, services=services)
        sys.exit(0 if success else 1)

    elif args.command == "stop":
        mgr.stop(services)

    elif args.command == "restart":
        mgr.restart(services)

    elif args.command == "status":
        mgr.status()

    elif args.command == "logs":
        mgr.logs(service=args.service, tail=args.tail)


if __name__ == "__main__":
    main()
