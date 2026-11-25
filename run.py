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
import base64
import hashlib
import hmac
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
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
    "pageindex-service": {
        # 内置知识库检索服务（pageindex-service）：引擎经内网 HTTP 调它做「建索引 / 检索」。
        #
        # **必须有这一项**：此前 run.py 只起网关与引擎，`KNOWLEDGE_SERVICE_URL` 永远为空，
        # 引擎侧 `RemoteProvider` 每次检索都报"服务未就绪"，知识库页面能建库、能上传，
        # 一建索引就失败 —— 这就是开发态"连不上内置 pageindex"的直接原因。
        #
        # 独立解释器：`pageindex → litellm → openai<3` 与引擎的 `openai 3.x` 在同一个
        # 环境里 pip 直接 ResolutionImpossible，所以用 `<仓库>/.venv-pageindex`
        # （由 `run.py setup` 创建）。缺这个 venv 时该服务被跳过，不影响聊天/记忆。
        "name": "PageIndex 检索服务",
        "port": 8100,
        "cmd": [""],  # 占位：真实命令在启动时由 ServiceManager._pageindex_cmd() 解析
        "cwd": str(BASE_DIR / "pageindex-service"),
        "env": {},
        # 8100 被占时改挑空闲端口（引擎拿到的是**实际**端口）
        "dynamic_port": True,
        # 可选组件：起不来只提示，不让 `run.py start` 整体失败
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

#: 本次运行实际使用的端口（与 `SERVICES[*]["port"]` 的"首选值"区分）。
#: 检索服务的端口是**运行时决定**的（8100 被占就换号），而引擎要拿这个真实端口
#: 拼 `KNOWLEDGE_SERVICE_URL` —— 所以它必须记录下来供后续服务读取。
_RUNTIME_PORTS: dict = {}


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
    """检查端口是否在监听（**只说明"有人在"**，不说明是不是我们起的那个）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def listener_pid(port: int) -> int | None:
    """查询监听 `port` 的 PID（H43）。取不到返回 None：**未知 ≠ 通过**。"""
    from scripts.service_identity import listener_pid as _listener_pid

    return _listener_pid(port)


def port_owned_by(port: int, pid: int) -> bool | None:
    """`port` 是否由 `pid` 监听。三态（H43）：

    - ``True``  能证明是它；
    - ``False`` 能证明不是它（端口被别的进程占着 —— 技能上的"重启没停干净"）；
    - ``None``  查不到（没有 netstat/lsof、权限不足）⇒ 调用方按"无法证明"处理，
      既不能报成功，也不该判失败。
    """
    owner = listener_pid(port)
    if owner is None:
        return None
    return owner == pid


def find_free_port() -> int:
    """借一个当前空闲的端口号（listen 到 0 再 close，与 Electron 侧 pickFreePort 同口径）。

    借号与真正 bind 之间仍有竞态，所以调用方要么重试、要么接受"极少数情况下失败"。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── 内置知识库检索服务（pageindex-service）────────────────────────────

#: 独立解释器的位置：`pageindex → litellm → openai<3` 与引擎的 `openai 3.x`
#: 无法共存于同一份环境（pip ResolutionImpossible），只能各用一份 venv。
PAGEINDEX_VENV = BASE_DIR / ".venv-pageindex"


def pageindex_python() -> str:
    """检索服务该用哪个解释器；没有可用的就返回空串（调用方跳过该服务）。

    顺序：显式 `CHIRON_PAGEINDEX_PYTHON` → 仓库内 `.venv-pageindex` → 空。
    **不回退到当前解释器**：那份环境装了引擎的 `openai 3.x`，起 pageindex 必然
    在 import 阶段炸，而报错点离"用错了 venv"很远。
    """
    explicit = (os.environ.get("CHIRON_PAGEINDEX_PYTHON") or "").strip()
    if explicit:
        return explicit
    candidate = PAGEINDEX_VENV / ("Scripts/python.exe" if is_windows() else "bin/python")
    return str(candidate) if candidate.exists() else ""


def pageindex_cmd() -> list[str]:
    """检索服务的启动命令（uvicorn，监听地址与端口由调用方补 --port）。"""
    python = pageindex_python() or sys.executable
    return [python, "-u", "-m", "uvicorn", "app.search_api:app", "--host", "127.0.0.1"]


def _service_available(key: str, svc: dict) -> bool:
    """该服务在这台机器上能不能起（可选服务据此决定"跳过"还是"启动"）。

    `pageindex-service` 的 `SERVICES["cmd"]` 只是占位（真实解析依赖独立 venv），
    所以它不能走 `shutil.which(cmd[0])` —— 那会把"已装好 venv"判成"未安装"。
    """
    if key == "pageindex-service":
        return bool(pageindex_python())
    return shutil.which(svc["cmd"][0]) is not None


def derive_subsecret(app_secret: str, domain: str) -> str:
    """HMAC-SHA256(APP_SECRET, domain) → base64url 无 padding。

    **必须与 Go `config/config.go::deriveSubsecret`、引擎 `app/config.py`、
    `frontend-vue/electron/local-stack.cjs` 逐字一致**：检索服务用 `INTERNAL_TOKEN`
    校验引擎的内部调用，两边只要有一处算法不同，表现就是每一次检索都 401。
    """
    digest = hmac.new(app_secret.encode("utf-8"), domain.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _gateway_base_url() -> str:
    """本机网关的内部访问地址（给检索服务回查模型配置用）。"""
    port = read_port("gateway") or SERVICES["gateway"]["port"]
    return f"http://127.0.0.1:{port}"


def fetch_gateway_knowledge_model(base_url: str, token: str, *, attempts: int = 8, delay: float = 0.5) -> dict:
    """向网关取「这台机器上第一个可用 chat 模型」（`/v1/internal/knowledge-model`）。

    为什么必须问网关而不是自己读 PG：模型与 provider key 的**权威组合**在网关
    （`llm_models` + `llm_provider_keys` + 目录里的 provider 默认端点），本文件没有
    解密能力也没有那份目录。自己拼一份的结果就是两边漂移 —— 而漂移的表现是
    "管理员在 /models 配了模型，检索服务却说未配置"。

    重试是必要的：网关刚 spawn 出来时端口可能还没监听，此刻直接判失败会让
    pageindex 带着空模型起来，之后要等用户手动重启才恢复。
    """
    url = f"{base_url.rstrip('/')}/v1/internal/knowledge-model"
    headers = {"X-Internal-Token": token, "Accept": "application/json"}
    for i in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            payload = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
            if isinstance(payload, dict) and payload.get("available") and payload.get("chat_model"):
                return payload
            # 网关回"库里没有可用模型"是**终态**：再重试也不会变，直接把原因带回去
            if isinstance(payload, dict):
                return {"available": False, "reason": str(payload.get("reason") or "")}
        except Exception:  # noqa: BLE001 — 未就绪/连接被拒：等下一轮
            pass
        if i + 1 < attempts:
            time.sleep(delay)
    return {"available": False, "reason": "网关未就绪，无法读取 /models 的模型配置"}


def _resolve_pageindex_env() -> dict:
    """检索服务需要的环境：内部令牌、索引/问答模型、原文与索引目录。

    这些都是**服务侧必需**、而引擎侧各自有默认值的项。缺令牌 ⇒ 每次调用 401；
    缺模型 ⇒ `/healthz` 回 `status=disabled`（引擎看到的就是"服务不可用"）；
    目录与引擎不一致 ⇒ worker 读不到原文，索引永远停在 indexing。

    模型来源的优先级（见 docs 与 electron/local-stack.cjs 的同名实现）：
      1. 显式 `KNOWLEDGE_SELFHOSTED_*`（管理员在「知识库」页钉死，最高）；
      2. 网关 `llm_models`（= 管理员在 /models 配的，**权威源**）；
      3. `LLM_MODEL` / `OPENAI_MODEL` 等启动环境（开发态便利，最低）。
    """
    env: dict = {}

    app_secret = (os.environ.get("APP_SECRET") or DEFAULT_ENV.get("APP_SECRET") or "").strip()
    internal_token = (os.environ.get("INTERNAL_TOKEN") or DEFAULT_ENV.get("INTERNAL_TOKEN") or "").strip()
    if not internal_token and app_secret:
        internal_token = derive_subsecret(app_secret, "chiron-internal")
    if internal_token:
        env["INTERNAL_TOKEN"] = internal_token

    # 检索服务在 env 里找不到模型时会回查网关（惰性、带短负缓存）。地址必须给它：
    # 少了它，Electron 那种"检索服务先于网关启动"的顺序永远读不到 /models 的配置。
    gateway_base = _gateway_base_url()
    env["GATEWAY_INTERNAL_URL"] = gateway_base

    explicit_chat = (
        os.environ.get("KNOWLEDGE_SELFHOSTED_CHAT_MODEL")
        or DEFAULT_ENV.get("KNOWLEDGE_SELFHOSTED_CHAT_MODEL")
        or ""
    ).strip()
    explicit_index = (
        os.environ.get("KNOWLEDGE_SELFHOSTED_INDEX_MODEL")
        or DEFAULT_ENV.get("KNOWLEDGE_SELFHOSTED_INDEX_MODEL")
        or ""
    ).strip()

    chat_model = explicit_chat
    index_model = explicit_index
    base_url = ""
    api_key = ""

    if not (chat_model and index_model) and internal_token:
        from_gateway = fetch_gateway_knowledge_model(gateway_base, internal_token)
        if from_gateway.get("available"):
            chat_model = chat_model or str(from_gateway.get("chat_model") or "")
            index_model = index_model or str(from_gateway.get("index_model") or chat_model)
            base_url = str(from_gateway.get("base_url") or "")
            api_key = str(from_gateway.get("api_key") or "")
        elif from_gateway.get("reason"):
            print(yellow(f"  检索服务模型未解析：{from_gateway['reason']}"))
            print(gray("    （检索服务会在网关就绪后自动重试；也可在「知识库」页显式覆盖）"))

    # 最低优先：启动环境里的模型配置（开发态便利）。**不覆盖**上面两级已定的值。
    chat_model = chat_model or (
        os.environ.get("OPENAI_MODEL") or os.environ.get("LLM_MODEL") or DEFAULT_ENV.get("LLM_MODEL") or ""
    )
    if chat_model:
        env["KNOWLEDGE_SELFHOSTED_CHAT_MODEL"] = chat_model
        env["KNOWLEDGE_SELFHOSTED_INDEX_MODEL"] = index_model or chat_model
    base_url = base_url or os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    if base_url:
        env["KNOWLEDGE_SELFHOSTED_LLM_BASE_URL"] = base_url
    api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if api_key:
        env["KNOWLEDGE_SELFHOSTED_LLM_API_KEY"] = api_key

    # 原文（引擎侧写）与索引产物（worker 侧写）必须是**同一对目录**：
    # 引擎的 `KNOWLEDGE_LOCAL_PATH` 默认是相对 cwd 的 `data/knowledge`，
    # 而服务侧默认是 `/srv/chiron/...`（容器口径）—— 在裸机上两者对不上，
    # 于是 worker 读不到原文，文档永远停在 pending/indexing。
    env["KNOWLEDGE_LOCAL_PATH"] = str(BASE_DIR / "python-engine" / "data" / "knowledge")
    env["KNOWLEDGE_SELFHOSTED_ROOT"] = str(BASE_DIR / "data" / "knowledge_index")
    return env


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


def get_port_file(service: str) -> Path:
    return PID_DIR / f"{service}.port"


def read_port(service: str) -> int | None:
    """读上次启动时记录的**实际**端口（`status` / 单独重启下游服务时要用）。

    检索服务的端口是运行时挑的（8100 被占就换号），只靠 `SERVICES[*]["port"]`
    会把"跑在 49321 上"的服务显示成"8100 未监听"。
    """
    pf = get_port_file(service)
    if pf.exists():
        try:
            return int(pf.read_text().strip())
        except ValueError:
            return None
    return None


def write_port(service: str, port: int):
    PID_DIR.mkdir(parents=True, exist_ok=True)
    get_port_file(service).write_text(str(port))


def remove_port(service: str):
    pf = get_port_file(service)
    if pf.exists():
        pf.unlink()


def is_process_running(pid: int) -> bool:
    """检查进程是否在运行 —— **按 PID 精确匹配**（H43）。

    旧实现是 `str(pid) in tasklist_output`：PID `123` 会命中 `1234`，于是"我们的
    进程早死了、PID 被系统复用给别人"也会报"运行中"。表格式输出又是按列对齐的，
    窄终端里各列会挤成一行，子串匹配必然误判。这里改用 `-FO CSV` + 精确比较。
    """
    if pid <= 0:
        return False
    try:
        if is_windows():
            from scripts.service_identity import parse_tasklist_pid

            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
            )
            return parse_tasklist_pid(result.stdout or "", pid) is not None
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

        if service_key == "pageindex-service":
            # 检索服务本身用不到引擎那套变量，但需要内部令牌/模型/目录（见 _resolve_pageindex_env）
            for key, value in _resolve_pageindex_env().items():
                env.setdefault(key, value)
            port = _RUNTIME_PORTS.get("pageindex-service")
            if port:
                env["PAGEINDEX_PORT"] = str(port)

        if service_key == "python-engine":
            # 引擎必须知道检索服务的**实际**地址：它在 spawn 时读一次就定了，
            # 之后再改（换个引擎参数）不会生效。只在本机确实起了该服务时才下发，
            # 否则引擎侧会显示"检索服务不可达"而不是"服务未部署"。
            port = _RUNTIME_PORTS.get("pageindex-service") or read_port("pageindex-service")
            explicit = os.environ.get("KNOWLEDGE_SERVICE_URL") or DEFAULT_ENV.get("KNOWLEDGE_SERVICE_URL")
            if port and not explicit and is_port_open(port, "127.0.0.1"):
                env["KNOWLEDGE_SERVICE_URL"] = f"http://127.0.0.1:{port}"

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
        """找到监听指定端口的进程 PID（H43：**按端口精确匹配**）。

        旧实现是 `f":{port}" in line`：端口 `8080` 会命中 `18080`、`80800`，
        于是"端口已有人监听"的判定可能来自一个完全无关的服务。
        """
        owner = listener_pid(port)
        if owner is not None:
            return owner
        # 拿不到归属时的兜底：先看我们记录的 PID 是否还活着（**不**等价于"它在监听"，
        # 但比"随机挑一个 PID"强）。调用方要区分"未知"与"确认"，见 verify_service。
        pid = read_pid(key)
        if pid and is_process_running(pid):
            return pid
        return None

    def verify_service(self, key: str, *, port: int | None = None) -> tuple[bool, str]:
        """校验服务**真的**起来了（H43）：PID 存活 **且** 端口归属一致。

        返回 (ok, 说明)。三态语义：
        - ok=True  ：PID 存活且能证明端口属于它；
        - ok=False ：PID 不存活 / 端口被别的进程占着（重启没停干净的典型形态）；
        - ok=True  + 说明里写 "unverified"：PID 存活但查不到端口归属
          （没有 netstat/lsof）—— **无法证明**，既不报成功也不报失败，
          由调用方决定是否升级为失败（`--strict` 时会）。
        """
        svc = SERVICES[key]
        name = svc["name"]
        if port is None:
            port = read_port(key) or svc["port"]
        pid = read_pid(key)
        if not pid or not is_process_running(pid):
            return False, f"{name}: 进程未运行（PID {pid or '-'}）"
        owned = port_owned_by(port, pid)
        if owned is True:
            return True, f"{name}: PID {pid} 监听 {port}"
        if owned is False:
            owner = listener_pid(port)
            return False, (
                f"{name}: 端口 {port} 不属于本进程（监听者 PID {owner}，记录 PID {pid}）"
                " —— 旧进程可能没停干净"
            )
        return True, f"{name}: PID {pid} 存活，端口 {port} 归属未验证（缺 netstat/lsof）"

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

        self._setup_pageindex()

        print(green("依赖安装完成"))
        return True

    def _setup_pageindex(self):
        """为内置检索服务准备**独立** venv（`pageindex → litellm → openai<3`）。

        为什么不能装进当前解释器：引擎固定 `openai 3.x`，两者的依赖树在同一个环境里
        pip 直接 ResolutionImpossible —— 装一边必然把另一边弄坏。失败**不算致命**：
        检索是可选能力，缺它时建库/上传照常，只是建索引与检索不可用。
        """
        print("  检查知识库检索服务（pageindex-service）...")
        if pageindex_python():
            print(green("    已就绪"))
            return
        req_file = BASE_DIR / "pageindex-service" / "requirements.txt"
        if not req_file.exists():
            print(gray("    未找到 pageindex-service，跳过"))
            return
        try:
            print(yellow("    创建独立环境 .venv-pageindex ..."))
            created = subprocess.run(
                [sys.executable, "-m", "venv", str(PAGEINDEX_VENV)],
                capture_output=True, text=True,
            )
            if created.returncode != 0:
                raise RuntimeError(created.stderr.strip()[:400] or "venv 创建失败")
            python = pageindex_python()
            installed = subprocess.run(
                [python, "-m", "pip", "install", "-r", str(req_file), "-q"],
                capture_output=True, text=True,
            )
            if installed.returncode != 0:
                raise RuntimeError(installed.stderr.strip()[-400:] or "pip install 失败")
            print(green("    已安装（python run.py start 会一并拉起它）"))
        except Exception as exc:  # noqa: BLE001 — 可选组件，装不上只提示
            print(yellow(f"    安装失败：{exc}"))
            print(gray("    知识库的建索引/检索将不可用（建库与上传不受影响）。"))
            print(gray("    可设置 CHIRON_PAGEINDEX_PYTHON 指向一个已装好依赖的解释器。"))

    def _start_service(self, key: str, background: bool = True):
        """启动单个服务"""
        svc = SERVICES[key]
        name = svc["name"]
        cwd = svc.get("cwd", str(BASE_DIR))

        # 检索服务的命令是**运行时解析**的（要用独立的 venv 解释器），不能写死在 SERVICES 里。
        if key == "pageindex-service":
            if not pageindex_python():
                print(f"  {name}: {gray('已跳过（缺少独立 venv：先跑 python run.py setup）')}")
                return True
            cmd = pageindex_cmd()
        else:
            cmd = svc["cmd"]

        # 首选端口被占时换一个空闲端口。检索服务必须换（本机上 8100 常被别人占着），
        # 而引擎要拿这个**实际**端口拼 URL —— 所以端口要记进 _RUNTIME_PORTS。
        port = svc["port"]
        if svc.get("dynamic_port") and is_port_open(port):
            preferred = port
            port = find_free_port()
            print(f"  {name}: {yellow(f'端口 {preferred} 已被占用，改用 {port}')}")
        _RUNTIME_PORTS[key] = port
        write_port(key, port)

        # uvicorn 的监听端口只能从命令行给（`PAGEINDEX_PORT` 只影响服务内部配置），
        # 少了这一句它会退回 uvicorn 默认的 8000 —— 正好与引擎撞车。
        if key == "pageindex-service":
            cmd = [*cmd, "--port", str(port)]

        env = self._build_env(key)
        if key == "pageindex-service":
            env["PAGEINDEX_PORT"] = str(port)

        # 可选服务：二进制不存在时**跳过并视为成功**。
        # 可选服务是增强项（如 XERJ 检索），不是运行前提 —— 让它把 `start` 的整体
        # 退出码拖成失败，会让"没装就该跳过"变成"没装就起不来"。
        if svc.get("optional") and not _service_available(key, svc):
            print(f"  {name}: {gray('已跳过（未安装，可选）')}")
            return True

        # 检查是否已在运行
        pid = read_pid(key)
        if pid and is_process_running(pid):
            print(f"  {name}: {yellow('已在运行')} (PID {pid})")
            return True

        # 检查端口占用（换过号的端口是刚借到的，仍然有极小的竞态，所以照查）
        if is_port_open(port):
            print(f"  {name}: {yellow(f'端口 {port} 已被占用')}")
            return bool(svc.get("optional"))

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
        remove_port(key)

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
            if key not in SERVICES:
                continue
            ok = self._start_service(key, background)
            # 可选服务（检索 / 索引增强）失败不该让 `start` 整体退出码变红：
            # 聊天、记忆、技能与建库上传都不依赖它，只有"建索引/检索"依赖。
            if not ok and SERVICES[key].get("optional"):
                print(
                    f"  {SERVICES[key]['name']}: {yellow('未启动（可选组件，不影响其它功能）')}"
                )
                ok = True
            results[key] = ok

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

    def restart(self, services: list = None, *, strict: bool = False) -> bool:
        """重启服务，并**校验真的起来了**（H43）。

        旧实现只做 `stop(); sleep(1); start()` 且**没有返回值** —— 于是 `run.py restart`
        永远退出码 0。"重启失败"的两种真实形态（旧进程没停干净、新进程起来又立刻挂掉）
        在旧实现里完全不可见：用户看到 "已重启"，实际连的还是旧进程或根本没人听。

        返回：全部服务校验通过为 True。
        """
        self.stop(services)
        time.sleep(1)
        started = self.start(services=services)
        targets = services or list(SERVICES.keys())
        ok = bool(started)
        print(bold("\n重启后校验"))
        for key in targets:
            if key not in SERVICES:
                continue
            svc = SERVICES[key]
            if svc.get("optional") and not _service_available(key, svc):
                continue
            healthy, detail = self.verify_service(key)
            if healthy:
                print(f"  {green('OK')} {detail}")
                if "未验证" in detail:
                    # 查不到归属：不能报成功，也不该静默 —— 显式提示取证方式。
                    print(gray("       （无法证明端口归属：装 netstat/lsof 或用 --strict 失败）"))
                    if strict:
                        ok = False
                continue
            print(f"  {red('FAIL')} {detail}")
            ok = False
        return ok

    def status(self):
        """查看服务状态"""
        print(bold("\n═══ chiron 服务状态 ═══\n"))
        print(f"{'服务':<20} {'状态':<12} {'PID':<10} {'端口':<8} {'端口状态'}")
        print("─" * 70)

        for key, svc in SERVICES.items():
            name = svc["name"]
            port = read_port(key) or svc["port"]
            pid = read_pid(key)

            # 可选服务未安装：既不是"运行中"也不是"已停止"，报"未安装"才准确
            # —— 否则用户会以为它该跑却没跑起来，去翻一个根本不存在的日志。
            if svc.get("optional") and not _service_available(key, svc):
                hint = "可选（先跑 setup）" if key == "pageindex-service" else "可选"
                print(f"{name:<20} {gray('未安装'):<12} {'-':<10} {port:<8} {gray(hint)}")
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
        kb_port = _RUNTIME_PORTS.get("pageindex-service")
        if kb_port and is_port_open(kb_port):
            print(f"  PageIndex:    http://localhost:{kb_port}  (知识库检索，内置)")
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
        "--strict",
        action="store_true",
        help="重启后校验更严：端口归属查不到也算失败（默认只告警）",
    )
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
        # H43：重启必须有可验证的结论 —— 旧实现没有返回值也没有 `sys.exit`，
        # 于是"重启失败"退出码 0（CI/脚本据此判定成功），这正是"假重启"。
        sys.exit(0 if mgr.restart(services, strict=args.strict) else 1)

    elif args.command == "status":
        mgr.status()

    elif args.command == "logs":
        mgr.logs(service=args.service, tail=args.tail)


if __name__ == "__main__":
    main()
