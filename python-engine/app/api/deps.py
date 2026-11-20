"""Python 依赖管理 API —— 列出 / 搜索 / 安装 / 卸载 pip 包（内部端点）。

后台「Python 依赖管理」页面的唯一后端实现。四个端点都是**变更运行环境**的操作，
因此每一条边界都必须显式写下来（顺序即优先级）：

1. **鉴权**：所有端点都要求 ``X-Internal-Token``（与网关的 ``internalTokenMW``
   同一凭据）。引擎的 ``AuthMiddleware`` 已经会对非探针路径做这一层校验，这里再以
   FastAPI 依赖做一遍是**刻意的纵深防御** —— 中间件白名单/装配一旦被改动，端点自己
   仍然 fail-closed。
2. **解释器固定**：只用 ``sys.executable -m pip``。绝不用 ``pip``（PATH 上的可能是
   另一个 venv / 系统 Python），绝不读仓库的 ``.env``（那是密钥文件，与"装包"无关）。
3. **规格白名单**：只接受 ``name`` 或 ``name<op>version`` 形式的 PEP 508 子集
   （见 ``PIP_SPEC_RE``）。空格 / 分号 / ``-r`` / URL / 路径 / extras 一律拒绝 ——
   ``pip install -r .env`` 能把密钥文件当需求清单读出来，``pkg; rm -rf /`` 在
   ``shell=True`` 下就是任意命令执行。这里**不用 shell**（``create_subprocess_exec``
   传参数列表），上面的正则只是把"能做但绝不该做"的形态也挡在门外。
4. **超时 + 输出上限**：pip 卡在网络上时不设超时就会占住一个连接直到进程退出；
   输出不设上限则一条 ``pip install`` 的编译日志可以吃掉几百 MB。
5. **单任务互斥**：安装/卸载是"改同一个 site-packages"的操作，并发执行会互相破坏
   半装的包。安装走**后台任务 + 轮询**（``GET /internal/deps/jobs/{job_id}``），
   因为一次编译安装必然超过网关的同步超时。

端点清单：

- ``GET  /internal/deps/list?``               → 当前解释器已安装的包
- ``GET  /internal/deps/search?q=<name>``     → PyPI 精确名称查询（60s 缓存）
- ``POST /internal/deps/install`` ``{"spec"}`` → 创建安装任务，立即返回 job
- ``POST /internal/deps/uninstall`` ``{"name"}`` → 同步卸载（秒级）
- ``GET  /internal/deps/jobs/{job_id}``        → 轮询任务进度与输出
- ``GET  /internal/deps/snapshots``           → 快照列表（最新在前）
- ``POST /internal/deps/snapshots`` ``{"label"}`` → 立即创建一份手动快照
- ``POST /internal/deps/snapshots/{id}/restore`` ``{"dry_run"}`` → 差异预览 / 恢复任务

**快照与回滚**：install/uninstall/restore 在**改动 site-packages 之前**都会先自动落一份
`pip list --format=json` 的结果到 ``<data_dir>/deps-snapshots/<id>.json``（只保留最近 20
份）。恢复先算差异（多出的卸、缺失/改版本的按快照版本钉住装），再以后台任务执行 ——
一次最多装 50 个包，超出直接拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.http_client import shared_client

logger = logging.getLogger(__name__)

# ── 校验规则（前后端**同一份**语义，改动必须与 frontend-vue/src/api/deps.ts 同步）──

#: PEP 508 名称的左半部分：**必须以字母/数字开头并结尾**（中间可含 ``. _ -``）。
#:
#: 这个"首尾必须是字母数字"的约束不是洁癖，是安全边界：pip 会把形如路径的参数当
#: 本地路径处理，而 ``.env`` / ``-r`` 都满足"由名称字符组成" —— 前者会让
#: ``pip install .env`` 变成"从本地目录安装"，后者会被 pip 当成 ``--requirement``
#: 的缩写、把任意文件读成需求清单（``.env`` 里恰好全是密钥）。
_NAME_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"

#: 允许的 pip 规格：``name`` 或 ``name<op>version``。
#:
#: 刻意**不允许**的部分（每一条都是真实的攻击面）：
#: * 空格 / 分号 / ``&&`` / 反引号 → 命令拼接（即便不用 shell 也不给未来的改动留后门）；
#: * 以 ``-`` / ``.`` 开头 → ``-r`` / ``-e`` / ``.env``（pip 会把它们当选项或本地路径）；
#: * ``://`` / ``/`` / ``\\`` → URL 与本地路径（``pip install https://evil/x.whl``）；
#: * ``[...]`` → extras（本身无害，但会引出 ``name[extra] @ url`` 这种形态，先不收）。
PIP_SPEC_RE = re.compile(rf"^{_NAME_PATTERN}(?:[<>=!~]=?[A-Za-z0-9.*+_-]+)?$")

#: 卸载只接受**纯包名**：带版本约束的卸载没有意义（pip 会警告并忽略）。
PACKAGE_NAME_RE = re.compile(rf"^{_NAME_PATTERN}$")

MAX_SPEC_LEN = 128
MAX_PACKAGE_NAME_LEN = 128

#: 单次 pip 调用的超时（秒）。安装要编译轮子，给足时间但必须有限。
LIST_TIMEOUT_SECONDS = 30.0
INSTALL_TIMEOUT_SECONDS = 300.0
UNINSTALL_TIMEOUT_SECONDS = 120.0

#: 子进程输出上限：超出部分丢弃（仍继续读取，否则管道写满会让 pip 阻塞）。
MAX_OUTPUT_BYTES = 64 * 1024
#: 任务日志保留上限（字符）：足够看清 pip 的 Conclusion / error 行。
MAX_JOB_LOG_CHARS = 8000
#: 已安装包列表上限：防止异常环境把响应撑爆。
MAX_PACKAGES = 2000

#: PyPI JSON API（精确名称查询；该 API 没有关键词搜索端点，见 ``search_packages``）。
PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
PYPI_TIMEOUT_SECONDS = 8.0
PYPI_CACHE_TTL_SECONDS = 60.0
PYPI_CACHE_MAX_ENTRIES = 200

#: 任务表上限：只保留最近 N 条，避免长期运行积累。
MAX_JOBS = 50
#: 已完成任务的最长保留时间（秒），超时后清理。
JOB_RETENTION_SECONDS = 3600.0

# ── 快照相关上限 ─────────────────────────────────────────────────────────
#: 快照目录名（挂在 ``data_dir`` 下）与保留份数。20 份足够覆盖"装坏了再往回找"，
#: 又不会把磁盘吃满（几百个包的一份快照约几十 KB）。
SNAPSHOT_DIRNAME = "deps-snapshots"
MAX_SNAPSHOTS = 20
#: 一次恢复最多装多少个包（缺失 + 版本变化）。再多就不该用"回滚"解决 ——
#: 那已经不是"回到某个已知良好状态"，而是一次环境重建，应当显式去做。
MAX_RESTORE_INSTALLS = 50
#: 批量重装的超时（秒）：一次装 50 个包可能是编译墙，比单个安装更宽松但仍有限。
RESTORE_INSTALL_TIMEOUT_SECONDS = 900.0
#: 快照标签长度上限（只用于展示）。
MAX_SNAPSHOT_LABEL_LEN = 120
#: 快照文件格式版本：将来字段变化时据此判断兼容（当前只写不用）。
SNAPSHOT_SCHEMA_VERSION = 1

#: 快照 id 白名单：``<UTC 时间戳到毫秒>Z-<6 位随机>``。
#:
#: id 会被拼进文件路径，所以这里**只认引擎自己生成的形态**（不接受任何 ``.`` / ``/``
#: / ``\\`` / ``..``，随机后缀也是必需的）。``snapshot_path()`` 还会再用 realpath
#: 确认落在快照目录内 —— 白名单是"只接受已知形态"，realpath 是"就算白名单被放宽也
#: 出不去"，两道都要。
SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{9}Z-[a-z0-9]{6}$")

router = APIRouter(prefix="/internal/deps", tags=["deps"])


# ── 鉴权 ────────────────────────────────────────────────────────────────


def require_internal_token(request: Request) -> None:
    """校验 ``X-Internal-Token``（与 Go 网关 ``internalTokenMW`` 同一凭据）。

    与 ``app/api/isolation.py::_verify_gateway_key`` 同款策略：未配置 ⇒ 503
    （fail-closed，不存在"没配就放行"），不匹配 ⇒ 401。
    """
    expected = (settings.internal_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_TOKEN is not configured on the engine; refusing dependency management",
        )
    provided = request.headers.get("X-Internal-Token", "")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


# ── 输入校验（纯函数，便于单测）──────────────────────────────────────────


def pip_spec_ok(spec: str) -> bool:
    """规格形态是否合法（不含长度与空值判定，见 ``validate_pip_spec``）。"""
    return PIP_SPEC_RE.fullmatch(spec) is not None


def package_name_ok(name: str) -> bool:
    """包名形态是否合法（不含长度与空值判定，见 ``validate_package_name``）。"""
    return PACKAGE_NAME_RE.fullmatch(name) is not None


def validate_pip_spec(raw: str) -> str:
    """校验并归一化安装规格；非法时抛 ``ValueError``（调用方转成 400）。

    **任何**不匹配白名单的形态都拒绝，并且错误信息只说"允许什么"，不回显完整输入
    （回显会把恶意串原样写进日志/响应，虽然无害但没必要）。
    """
    spec = (raw or "").strip()
    if not spec:
        raise ValueError("spec is required")
    if len(spec) > MAX_SPEC_LEN:
        raise ValueError(f"spec is too long (max {MAX_SPEC_LEN} characters)")
    if not pip_spec_ok(spec):
        raise ValueError(
            "spec must be a package name optionally followed by one version operator "
            "(examples: requests, requests==2.31.0, requests>=2.31); "
            "spaces, semicolons, URLs, local paths, -r/-e options and extras are not allowed"
        )
    return spec


def validate_package_name(raw: str) -> str:
    """校验卸载用的包名（纯名称，不带版本约束）。"""
    name = (raw or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > MAX_PACKAGE_NAME_LEN:
        raise ValueError(f"name is too long (max {MAX_PACKAGE_NAME_LEN} characters)")
    if not package_name_ok(name):
        raise ValueError(
            "name must start and end with a letter or digit and contain only letters, "
            "digits, dot, underscore or hyphen (no version specifiers, spaces, paths or URLs)"
        )
    return name


# ── 子进程执行 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PipResult:
    """一次 pip 调用的结果（已脱敏、已截断）。"""

    ok: bool
    returncode: int
    output: str
    timed_out: bool = False
    truncated: bool = False


def build_pip_command(args: list[str]) -> list[str]:
    """构造 ``<当前解释器> -m pip <args>``。

    **永远是当前解释器**：后台管理的是"引擎正在用的那个 venv"。PATH 上的 ``pip``
    可能是另一个环境（桌面壳把 venv 装在自己目录下），装到那儿等于没装。
    """
    return [sys.executable, "-m", "pip", *args]


#: 传给 pip 子进程的环境变量白名单。
#:
#: 不继承完整 ``os.environ``：引擎环境里有 JWT_SECRET / INTERNAL_TOKEN / 各 provider
#: 的 API key，pip 的报错信息（尤其 ``-v`` 与索引 401）可能把环境值带出来。白名单只留
#: 运行 pip 必需的系统变量 + 代理/证书 + ``PIP_*``（用户自己配的镜像源要能生效）。
_PIP_ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PATHEXT",
        "COMSPEC",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

#: URL 里的 userinfo（``https://user:token@host/…``）→ 脱敏。pip 的报错会带出索引 URL，
#: 而这些 URL 常常内嵌凭据（私有源）。
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+(?::[^/\s@]*)?@")


def redact_output(text: str) -> str:
    """脱敏 pip 输出中的内嵌凭据（仅处理 URL userinfo，其它内容原样返回）。"""
    return _URL_USERINFO_RE.sub(r"\g<scheme>***@", text)


def _pip_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _PIP_ENV_ALLOW or k.startswith("PIP_")}
    # 关掉交互与版本检查：后台任务没有 TTY，任何 prompt 都会把任务挂到超时；
    # 版本检查是一次额外的网络往返，对这个页面没有价值。
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_PROGRESS_BAR"] = "off"
    env["PYTHONUNBUFFERED"] = "1"
    return env


async def _run_pip(
    args: list[str],
    *,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
) -> PipResult:
    """执行一次 pip 调用：不经过 shell、有超时、输出有上限。

    ``on_line`` 用于把输出**边跑边**喂给任务日志（后台安装任务的进度来源）。
    超时后 kill 子进程并返回 ``timed_out=True`` —— 不抛异常，因为调用方需要把
    "超时"与"exit 1"区分开展示。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_pip_command(args),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_pip_env(),
        )
    except OSError as exc:
        return PipResult(ok=False, returncode=-1, output=f"failed to start pip: {exc}")

    stdout = proc.stdout
    if stdout is None:  # pragma: no cover - 由 PIPE 保证非 None
        return PipResult(ok=False, returncode=-1, output="pip stdout is unavailable")

    collected: list[str] = []
    collected_bytes = 0
    truncated = False
    timed_out = False

    async def _drain() -> None:
        nonlocal collected_bytes, truncated
        while True:
            raw = await stdout.readline()
            if not raw:
                return
            text = redact_output(raw.decode("utf-8", errors="replace"))
            if on_line is not None:
                on_line(text)
            if collected_bytes < MAX_OUTPUT_BYTES:
                collected.append(text)
                collected_bytes += len(raw)
            elif not truncated:
                truncated = True

    try:
        async with asyncio.timeout(timeout):
            await _drain()
            returncode = await proc.wait()
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        returncode = -9

    output = "".join(collected)
    if truncated:
        output += "\n… (output truncated)\n"
    return PipResult(
        ok=returncode == 0 and not timed_out,
        returncode=returncode,
        output=output,
        timed_out=timed_out,
        truncated=truncated,
    )


# ── pip list 解析 ────────────────────────────────────────────────────────


def _extract_json_array(text: str) -> str | None:
    """从 pip 输出里截出 JSON 数组。

    pip 会在 JSON 前后打印 warning（弃用提示、索引告警），直接 ``json.loads`` 会因
    前缀而失败 —— 那会让"能装包"的环境被误判成"pip 坏了"。截取首个 ``[`` 到末个
    ``]`` 是最小且够用的修正。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]


def parse_pip_list(text: str) -> list[dict[str, str]]:
    """把 ``pip list --format=json`` 的输出解析成 ``[{name, version}]``（按名称排序、去重）。

    无法解析时抛 ``ValueError``：静默返回空列表会让页面显示"没有安装任何包"，
    而真相是 pip 输出变了 —— 那是两种完全不同的处置。
    """
    payload = _extract_json_array(text)
    if payload is None:
        raise ValueError("pip list did not return a JSON array")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"pip list returned invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("pip list returned an unexpected JSON shape")

    by_name: dict[str, dict[str, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        version = str(item.get("version") or "").strip()
        if not name or not version:
            continue
        by_name.setdefault(name.lower(), {"name": name, "version": version})
    packages = [by_name[key] for key in sorted(by_name)]
    return packages[:MAX_PACKAGES]


# ── PyPI 查询（缓存 60s）─────────────────────────────────────────────────

_pypi_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _pypi_cache.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > PYPI_CACHE_TTL_SECONDS:
        _pypi_cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: dict[str, Any]) -> None:
    if len(_pypi_cache) >= PYPI_CACHE_MAX_ENTRIES:
        # 简单清理：删掉最旧的一半（不需要精确 LRU，这里只是防止无界增长）。
        for old_key in list(_pypi_cache)[: PYPI_CACHE_MAX_ENTRIES // 2]:
            _pypi_cache.pop(old_key, None)
    _pypi_cache[key] = (time.monotonic(), value)


async def _fetch_pypi(url: str) -> httpx.Response:
    """GET 一个 PyPI URL，返回响应（调用方判状态码）。

    优先走进程级共享 client（keep-alive、尊重环境里的代理配置 —— 国内镜像/企业代理
    场景必须有）。但 httpx 在**建 client 阶段**就会解析 ``urllib.request.getproxies()``
    的全部代理项，其中包含 Windows 注册表的 ProxyServer；那一项畸形时（实测见过
    ``Invalid port: ':1]'``）连"发请求"都到不了，直接抛 ``InvalidURL``。

    此时回退到**直连**（``trust_env=False``）：目标主机是写死的 ``pypi.org``，
    不涉及用户可控 URL，因此不存在 SSRF 风险；而"本机代理配置坏了就完全不能查包"
    对用户是个无法自诊断的死路（错误文案里只会出现 InvalidURL）。
    """
    headers = {"Accept": "application/json"}
    try:
        async with shared_client(timeout=PYPI_TIMEOUT_SECONDS) as client:
            return await client.get(url, headers=headers)
    except httpx.InvalidURL as exc:
        logger.warning("PyPI lookup: invalid proxy configuration (%s); retrying without env proxies", exc)
        async with httpx.AsyncClient(timeout=PYPI_TIMEOUT_SECONDS, trust_env=False) as direct:
            return await direct.get(url, headers=headers)


async def _lookup_pypi(name: str) -> dict[str, Any]:
    """查 PyPI JSON API；返回 ``{"query","results","count","found"}``。

    该 API 是**精确项目名**查询（没有关键词搜索端点），所以 ``q`` 必须是一个包名。
    404 返回 ``found=False``（这是正常结论，不是错误）；网络/5xx 由调用方转 502。
    """
    url = PYPI_JSON_URL.format(name=quote(name, safe=""))
    resp = await _fetch_pypi(url)
    if resp.status_code == 404:
        return {"query": name, "results": [], "count": 0, "found": False}
    if resp.status_code >= 500:
        raise httpx.HTTPStatusError(f"PyPI returned {resp.status_code}", request=resp.request, response=resp)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ValueError(f"PyPI returned non-JSON payload: {exc}") from exc
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return {"query": name, "results": [], "count": 0, "found": False}

    project_url = str(info.get("project_url") or "")
    home_page = str(info.get("home_page") or "")
    entry = {
        "name": str(info.get("name") or name),
        "version": str(info.get("version") or ""),
        "summary": str(info.get("summary") or ""),
        "requires_python": str(info.get("requires_python") or ""),
        "homepage": home_page or project_url,
    }
    return {"query": name, "results": [entry], "count": 1, "found": True}


# ── 后台任务 ──────────────────────────────────────────────────────────────


@dataclass
class DepJob:
    """一次安装任务的可见状态。

    刻意只保留"用户需要看到的东西"：状态、进度、pip 的最后一段输出。
    不保留完整命令行以外的环境信息，也不回传 pip 的临时目录。
    """

    id: str
    kind: str  # install | uninstall
    target: str
    status: str = "queued"  # queued | running | succeeded | failed
    message: str = ""
    progress: int = 0
    output: str = ""
    returncode: int | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "message": self.message,
            "progress": self.progress,
            "output": self.output,
            "returncode": self.returncode,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


_jobs: dict[str, DepJob] = {}
#: 当前**唯一**的变更任务 id（install/uninstall 共用一把闸）。None 表示空闲。
_running_job_id: str | None = None
#: 后台任务的强引用（asyncio 只持弱引用，不存会被 GC 掉 —— 任务跑到一半消失）。
_job_tasks: set[asyncio.Task[None]] = set()


def _prune_jobs(now: float | None = None) -> None:
    """清理过期/超量的任务记录（只清已完成且超过保留期的）。"""
    current = now if now is not None else time.time()
    stale = [
        job_id
        for job_id, job in _jobs.items()
        if job.finished_at is not None and current - job.finished_at > JOB_RETENTION_SECONDS
    ]
    for job_id in stale:
        _jobs.pop(job_id, None)
    if len(_jobs) > MAX_JOBS:
        finished = sorted(
            (job for job in _jobs.values() if job.finished_at is not None),
            key=lambda job: job.finished_at or 0.0,
        )
        for job in finished[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.id, None)


def _append_job_output(job: DepJob, text: str) -> None:
    """追加任务日志（有上限；超出时保留**尾部** —— 结论行总在最后）。"""
    combined = job.output + text
    if len(combined) > MAX_JOB_LOG_CHARS:
        combined = combined[-MAX_JOB_LOG_CHARS:]
    job.output = combined


async def _run_install_job(job: DepJob, spec: str) -> None:
    """后台安装任务的执行体：先自动快照、再调 pip、推进进度、落结果。"""
    global _running_job_id

    job.status = "running"
    job.progress = 5
    job.message = "creating pre-install snapshot"
    try:
        # 快照必须在 pip install 之前：装完再拍就拍到了坏状态。
        await _snapshot_before_mutation(job, f"install:{spec}")
        job.progress = 10
        job.message = "running pip install"
        result = await _run_pip(
            ["install", "--no-input", "--disable-pip-version-check", spec],
            timeout=INSTALL_TIMEOUT_SECONDS,
            on_line=lambda line: _append_job_output(job, line),
        )
        job.returncode = result.returncode
        if result.ok:
            job.status = "succeeded"
            job.progress = 100
            job.message = "installed"
        else:
            job.status = "failed"
            job.progress = 100
            job.message = (
                f"pip install timed out after {int(INSTALL_TIMEOUT_SECONDS)}s"
                if result.timed_out
                else f"pip install exited with code {result.returncode}"
            )
        if result.output and result.output not in job.output:
            # 采集可能因上限而只有尾部：确保结论行一定在。
            _append_job_output(job, result.output[-2000:])
        logger.info(
            "deps install job finished",
            extra={"job_id": job.id, "spec": spec, "status": job.status},
        )
    except asyncio.CancelledError:
        job.status = "failed"
        job.message = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落到任务结果里，而不是消失
        job.status = "failed"
        job.message = f"unexpected error: {exc}"
        logger.warning("deps install job crashed: %s", exc)
    finally:
        job.finished_at = time.time()
        _running_job_id = None


# ── 快照与回滚 ────────────────────────────────────────────────────────────
#
# 装/卸包直接改动引擎自己的运行环境，而"装坏了"最需要的是一键回到装之前。因此
# install/uninstall/restore 在**动手之前**都会先落一份 `pip list --format=json` 的结果，
# 用户也可以手动创建。快照是**纯文件**（`<data_dir>/deps-snapshots/<id>.json`），
# 不依赖 Redis / 数据库 —— 引擎换进程、桌面壳重启后仍然可读。

class PipListError(RuntimeError):
    """``pip list`` 失败（超时 / 非零退出 / 输出无法解析）。

    单独一种异常是为了让调用方区分"读不到当前环境"（快照做不了，但可以继续装包并
    告警）与"落盘失败"（同样是继续，但原因不同）。
    """


def data_root() -> Path:
    """引擎的持久数据根目录。

    ``settings.data_dir`` 优先（桌面壳/部署方下发**绝对路径**，打包态 cwd 只读）；
    未配置时退化为 cwd 下的 ``data/`` —— 与 ``media`` / ``skills`` 的默认根同源。
    """
    configured = (getattr(settings, "data_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.getcwd()) / "data"


def snapshot_root() -> Path:
    """快照目录：``<data_dir>/deps-snapshots``。"""
    return data_root() / SNAPSHOT_DIRNAME


def snapshot_path(snapshot_id: str) -> Path:
    """把快照 id 解析成**绝对路径**，并同时做两道防穿越检查。

    1. 形态白名单（``SNAPSHOT_ID_RE``）：只接受引擎自己生成的 id，``../x`` / ``a/b`` /
       ``.env`` 这类"看着像文件名"的输入在第一步就被拒；
    2. realpath 前缀校验：即使白名单哪天被放宽，解析后的路径也必须仍在快照目录内。

    非法 id 抛 ``ValueError``（调用方转 400）；**不**抛 HTTPException —— 这是纯函数，
    要能被单测直接覆盖。
    """
    sid = (snapshot_id or "").strip()
    if not SNAPSHOT_ID_RE.fullmatch(sid):
        raise ValueError("invalid snapshot id")
    root = snapshot_root().resolve()
    target = (root / f"{sid}.json").resolve()
    if not target.is_relative_to(root):
        raise ValueError("snapshot id escapes the snapshot directory")
    return target


def new_snapshot_id(now: float | None = None) -> str:
    """生成快照 id：UTC 时间戳（到毫秒）+ 6 位随机后缀。

    时间戳在前 ⇒ 文件名按字典序即时间序（列表排序、保留最近 N 份都靠这一点）；
    随机后缀 ⇒ 同一毫秒内连续创建两次不会互相覆盖。
    """
    current = time.time() if now is None else now
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(current))
    millis = int(current * 1000) % 1000
    return f"{stamp}{millis:03d}Z-{uuid.uuid4().hex[:6]}"


def normalize_label(raw: str) -> str:
    """把用户输入的标签压成单行可打印文本（只用于展示，不参与任何路径/命令）。"""
    text = "".join(ch for ch in (raw or "") if ch.isprintable())
    return " ".join(text.split())[:MAX_SNAPSHOT_LABEL_LEN]


def write_snapshot(
    packages: list[dict[str, str]],
    *,
    trigger: str,
    label: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """把一份包清单落成快照文件（原子写 + 保留最近 N 份），返回完整文档。

    "原子"是指先写 ``.json.tmp`` 再 ``replace``：直接写目标文件的话，写一半被杀会留下
    半截 JSON，而列表端点会把每一个坏文件都当成一次告警 —— 用户看到的是"快照都不见了"。
    """
    current = time.time() if now is None else now
    sid = new_snapshot_id(current)
    doc: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "id": sid,
        "created_at": current,
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(current)),
        "trigger": trigger,
        "label": label,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": packages,
        "package_count": len(packages),
    }
    root = snapshot_root()
    root.mkdir(parents=True, exist_ok=True)
    target = snapshot_path(sid)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    prune_snapshots()
    return doc


def prune_snapshots(keep: int = MAX_SNAPSHOTS) -> list[str]:
    """只保留最近 ``keep`` 份快照，返回被删掉的 id。

    只处理**命名符合白名单**的文件：快照目录里如果有别的东西（用户放的笔记、调试
    留下的文件），清理时绝不能顺手删掉。
    """
    root = snapshot_root()
    if not root.is_dir():
        return []
    files = sorted(
        (path for path in root.glob("*.json") if SNAPSHOT_ID_RE.fullmatch(path.stem)),
        key=lambda path: path.stem,
        reverse=True,
    )
    removed: list[str] = []
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError as exc:  # pragma: no cover - 权限问题只影响清理
            logger.warning("deps snapshot prune failed for %s: %s", stale.name, exc)
            continue
        removed.append(stale.stem)
    return removed


def read_snapshot(snapshot_id: str) -> dict[str, Any]:
    """读一份快照并归一化其中的包条目。

    id 非法 → ``ValueError``；文件不存在 → ``FileNotFoundError``；坏 JSON / 结构异常 →
    ``ValueError``。包条目里缺 name/version 的项被丢弃（计数随之修正），因为恢复时
    它们无法变成任何 pip 参数。
    """
    path = snapshot_path(snapshot_id)
    raw = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot {snapshot_id} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"snapshot {snapshot_id} has an unexpected shape")

    packages: list[dict[str, str]] = []
    raw_packages = doc.get("packages")
    if isinstance(raw_packages, list):
        for item in raw_packages:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            version = str(item.get("version") or "").strip()
            if name and version:
                packages.append({"name": name, "version": version})
    doc["packages"] = packages
    doc["package_count"] = len(packages)
    doc.setdefault("id", snapshot_id)
    return doc


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def snapshot_meta(doc: dict[str, Any]) -> dict[str, Any]:
    """快照的**列表用**元数据（不含 packages：列表页不需要几百条包名）。"""
    return {
        "id": str(doc.get("id") or ""),
        "created_at": _as_float(doc.get("created_at")),
        "created_at_iso": str(doc.get("created_at_iso") or ""),
        "trigger": str(doc.get("trigger") or ""),
        "label": str(doc.get("label") or ""),
        "package_count": int(_as_float(doc.get("package_count"))),
        "python": str(doc.get("python") or ""),
    }


def list_snapshot_docs(limit: int = MAX_SNAPSHOTS) -> list[dict[str, Any]]:
    """列出快照元数据（最新在前）。坏文件跳过并告警，不因一个坏文件让整页 502。"""
    root = snapshot_root()
    if not root.is_dir():
        return []
    paths = sorted(
        (path for path in root.glob("*.json") if SNAPSHOT_ID_RE.fullmatch(path.stem)),
        key=lambda path: path.stem,
        reverse=True,
    )[:limit]
    docs: list[dict[str, Any]] = []
    for path in paths:
        try:
            docs.append(snapshot_meta(read_snapshot(path.stem)))
        except (OSError, ValueError) as exc:
            logger.warning("deps snapshot unreadable: %s (%s)", path.name, exc)
    return docs


def compute_restore_diff(
    snapshot_packages: list[dict[str, str]],
    current_packages: list[dict[str, str]],
) -> dict[str, Any]:
    """算出"把当前环境拉回快照"要动哪些包。

    三类差异（按包名大小写不敏感匹配，展示时保留原始大小写）：

    * ``extra``：现在有、快照里没有 → 卸载；
    * ``missing``：快照里有、现在没有 → 按快照版本装回；
    * ``changed``：两边都有但版本不同 → 装成快照里的版本（pip 天然支持升降级）。

    ``install_count`` = missing + changed，这就是"一次最多装 50 个"的计量口径：
    卸载不计入上限（卸载不下载、不编译，代价与风险都不在一个量级）。
    """
    snap: dict[str, dict[str, str]] = {}
    for pkg in snapshot_packages:
        snap.setdefault(pkg["name"].lower(), pkg)
    cur: dict[str, dict[str, str]] = {}
    for pkg in current_packages:
        cur.setdefault(pkg["name"].lower(), pkg)

    missing = sorted((snap[key] for key in snap if key not in cur), key=lambda p: p["name"].lower())
    extra = sorted((cur[key] for key in cur if key not in snap), key=lambda p: p["name"].lower())
    changed = sorted(
        (
            {"name": cur[key]["name"], "from": cur[key]["version"], "to": snap[key]["version"]}
            for key in snap
            if key in cur and cur[key]["version"] != snap[key]["version"]
        ),
        key=lambda item: str(item["name"]).lower(),
    )
    install_count = len(missing) + len(changed)
    return {
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "unchanged": len(snap) - len(missing) - len(changed),
        "install_count": install_count,
        "uninstall_count": len(extra),
        "total_changes": install_count + len(extra),
        "limit": MAX_RESTORE_INSTALLS,
        "exceeds_limit": install_count > MAX_RESTORE_INSTALLS,
    }


async def _current_packages() -> list[dict[str, str]]:
    """取当前解释器的包清单（与 ``/list`` 同一命令、同一解析），失败抛 ``PipListError``。"""
    result = await _run_pip(
        ["list", "--format=json", "--disable-pip-version-check"],
        timeout=LIST_TIMEOUT_SECONDS,
    )
    if not result.ok:
        reason = (
            f"pip list timed out after {int(LIST_TIMEOUT_SECONDS)}s"
            if result.timed_out
            else f"pip list exited with code {result.returncode}"
        )
        raise PipListError(f"{reason}: {result.output[-500:]}")
    try:
        return parse_pip_list(result.output)
    except ValueError as exc:
        raise PipListError(str(exc)) from exc


async def capture_snapshot(trigger: str, label: str = "") -> dict[str, Any]:
    """采集 + 落盘一份快照（手动创建与自动快照共用）。

    失败形态原样抛出（``PipListError`` / ``OSError``），由调用方决定是"转成 502"
    还是"记一行告警后继续装包"。
    """
    packages = await _current_packages()
    return write_snapshot(packages, trigger=trigger, label=normalize_label(label))


async def _snapshot_before_mutation(job: DepJob, trigger: str, label: str = "") -> None:
    """变更前的自动快照：失败**不**阻断变更，只在任务日志里留一行。

    为什么不是 fail-closed：快照是对"装坏了"的补偿，而不是安全前置条件。让磁盘只读
    这类环境问题把整个依赖管理功能锁死，用户会失去唯一的恢复手段；而继续执行至少
    还有 pip 自己的 wheel 缓存与"重装回去"这条手工路径。
    """
    try:
        await capture_snapshot(trigger, label)
        _append_job_output(job, f"snapshot created before {trigger}\n")
    except (PipListError, OSError) as exc:
        _append_job_output(job, f"pre-change snapshot failed (continuing): {exc}\n")
        logger.warning("deps pre-change snapshot failed: %s", exc)


async def _run_restore_job(job: DepJob, snapshot: dict[str, Any], diff: dict[str, Any]) -> None:
    """恢复任务的执行体：先卸多出的，再把缺失/改版本的按快照版本钉住装。"""
    global _running_job_id

    job.status = "running"
    job.progress = 5
    job.message = "creating pre-restore snapshot"
    try:
        await _snapshot_before_mutation(job, f"restore:{snapshot['id']}", f"pre-restore {snapshot['id']}")

        extras = [pkg["name"] for pkg in diff["extra"]]
        if extras:
            job.progress = 20
            job.message = f"uninstalling {len(extras)} package(s)"
            result = await _run_pip(
                ["uninstall", "--yes", "--no-input", "--disable-pip-version-check", *extras],
                timeout=UNINSTALL_TIMEOUT_SECONDS,
                on_line=lambda line: _append_job_output(job, line),
            )
            if not result.ok:
                job.status = "failed"
                job.progress = 100
                job.message = (
                    f"pip uninstall timed out after {int(UNINSTALL_TIMEOUT_SECONDS)}s"
                    if result.timed_out
                    else f"pip uninstall exited with code {result.returncode}"
                )
                return

        pinned = [f"{pkg['name']}=={pkg['version']}" for pkg in diff["missing"]]
        pinned += [f"{item['name']}=={item['to']}" for item in diff["changed"]]
        if pinned:
            job.progress = 50
            job.message = f"installing {len(pinned)} package(s)"
            result = await _run_pip(
                ["install", "--no-input", "--disable-pip-version-check", *pinned],
                timeout=RESTORE_INSTALL_TIMEOUT_SECONDS,
                on_line=lambda line: _append_job_output(job, line),
            )
            if not result.ok:
                job.status = "failed"
                job.progress = 100
                job.message = (
                    f"pip install timed out after {int(RESTORE_INSTALL_TIMEOUT_SECONDS)}s"
                    if result.timed_out
                    else f"pip install exited with code {result.returncode}"
                )
                return

        job.status = "succeeded"
        job.progress = 100
        job.message = f"restored to snapshot {snapshot['id']}"
        logger.info("deps restore job finished", extra={"job_id": job.id, "snapshot": snapshot["id"]})
    except asyncio.CancelledError:
        job.status = "failed"
        job.message = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落到任务结果里
        job.status = "failed"
        job.message = f"unexpected error: {exc}"
        logger.warning("deps restore job crashed: %s", exc)
    finally:
        job.finished_at = time.time()
        _running_job_id = None


# ── 请求体 ───────────────────────────────────────────────────────────────


class InstallRequest(BaseModel):
    """``{"spec": "requests==2.31.0"}``。"""

    spec: str = Field(default="", description="pip requirement spec (name or name<op>version)")


class UninstallRequest(BaseModel):
    """``{"name": "requests"}``。"""

    name: str = Field(default="", description="exact installed package name")


class SnapshotRequest(BaseModel):
    """``{"label": "before upgrade"}``（标签可选，只用于展示）。"""

    label: str = Field(default="", description="free-form label shown in the snapshot list")


class RestoreRequest(BaseModel):
    """``{"dry_run": true}`` 只算差异；``false`` 才真的建恢复任务。"""

    dry_run: bool = Field(default=False, description="compute the diff only, do not touch the environment")


# ── 端点 ─────────────────────────────────────────────────────────────────


@router.get("/list", dependencies=[Depends(require_internal_token)])
async def list_packages() -> dict[str, Any]:
    """列出**当前引擎解释器**已安装的包。

    不用 ``importlib.metadata`` 直接读：那个视角与 pip 的视角在可编辑安装 / 多
    ``site-packages`` 时会分叉，而用户在这里做的是 pip 操作，应当看到 pip 认为的结果。
    """
    result = await _run_pip(
        ["list", "--format=json", "--disable-pip-version-check"],
        timeout=LIST_TIMEOUT_SECONDS,
    )
    if not result.ok:
        reason = (
            f"pip list timed out after {int(LIST_TIMEOUT_SECONDS)}s"
            if result.timed_out
            else f"pip list exited with code {result.returncode}"
        )
        raise HTTPException(
            status_code=502,
            detail={"error": reason, "output": result.output[-2000:]},
        )
    try:
        packages = parse_pip_list(result.output)
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": str(exc), "output": result.output[-2000:]},
        ) from exc

    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": packages,
        "count": len(packages),
    }


@router.get("/search", dependencies=[Depends(require_internal_token)])
async def search_packages(
    q: str = Query(..., min_length=1, max_length=MAX_PACKAGE_NAME_LEN, description="exact PyPI project name"),
) -> dict[str, Any]:
    """按**精确项目名**查 PyPI（JSON API，60s 缓存）。

    PyPI 的 JSON API 没有关键词搜索端点（网页搜索是 HTML），所以这里把 ``q`` 当作
    项目名做精确查询，并在响应里用 ``found`` 明确区分"查无此包"与"网络不可用"
    （后者是 502，带明确原因）。
    """
    name = (q or "").strip()
    try:
        validate_package_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    key = name.lower()
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        result = await _lookup_pypi(name)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 失败形态无法穷举（DNS/代理/证书/URL 解析）
        # 刻意兜到最宽：这里的"不可用"不只是网络断开。实测机器上出现过
        # `httpx.InvalidURL: Invalid port: ':1]'`（来自畸形的 *_PROXY 环境变量），
        # 而它**不是** HTTPError —— 只捕 HTTPError 会让用户看到 500"内部错误"，
        # 与真实原因（本机代理配置坏了）完全对不上。所有形态一律转成 502 + 明确原因。
        logger.warning("PyPI lookup failed for %s: %s: %s", name, type(exc).__name__, exc)
        raise HTTPException(
            status_code=502,
            detail=f"PyPI is unreachable: {type(exc).__name__}: {exc}",
        ) from exc

    _cache_put(key, result)
    return {**result, "cached": False}


@router.post("/install", dependencies=[Depends(require_internal_token)])
async def install_package(payload: InstallRequest) -> dict[str, Any]:
    """创建安装任务并立即返回 job（进度用 ``GET /jobs/{id}`` 轮询）。

    为什么异步：一次带编译的安装轻易超过 60s，网关的同步超时会先断开，用户只会看到
    "请求失败"而包其实装了一半。返回 job 之后，连接超时不再影响结果能否被读到。
    """
    global _running_job_id

    try:
        spec = validate_pip_spec(payload.spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _running_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "another dependency task is already running",
                "job_id": _running_job_id,
            },
        )

    _prune_jobs()
    job = DepJob(id=uuid.uuid4().hex[:12], kind="install", target=spec, message="queued")
    _jobs[job.id] = job
    _running_job_id = job.id
    task = asyncio.create_task(_run_install_job(job, spec))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)

    logger.info("deps install job queued: %s", spec, extra={"job_id": job.id})
    return {"job": job.to_dict()}


@router.post("/uninstall", dependencies=[Depends(require_internal_token)])
async def uninstall_package(payload: UninstallRequest) -> dict[str, Any]:
    """同步卸载（秒级，不需要任务表）；与安装共用"同一时刻只有一个变更任务"的闸。

    ``-y`` 与 ``--no-input`` 同时给：``--no-input`` 只是"不读 stdin"，卸载的确认
    prompt 默认答案并非"是"，缺 ``-y`` 时在没有 TTY 的环境里会直接放弃卸载。
    """
    global _running_job_id

    try:
        name = validate_package_name(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _running_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "another dependency task is already running",
                "job_id": _running_job_id,
            },
        )

    marker = f"uninstall-{uuid.uuid4().hex[:8]}"
    _running_job_id = marker
    try:
        # 卸载前同样先快照（卸载没有任务表，所以快照失败只能记日志）。
        try:
            await capture_snapshot(f"uninstall:{name}")
        except (PipListError, OSError) as exc:
            logger.warning("deps pre-uninstall snapshot failed: %s", exc)
        result = await _run_pip(
            ["uninstall", "--yes", "--no-input", "--disable-pip-version-check", name],
            timeout=UNINSTALL_TIMEOUT_SECONDS,
        )
    finally:
        if _running_job_id == marker:
            _running_job_id = None

    if not result.ok:
        reason = (
            f"pip uninstall timed out after {int(UNINSTALL_TIMEOUT_SECONDS)}s"
            if result.timed_out
            else f"pip uninstall exited with code {result.returncode}"
        )
        raise HTTPException(
            status_code=502,
            detail={"error": reason, "output": result.output[-2000:]},
        )
    return {"ok": True, "name": name, "output": result.output[-2000:]}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_internal_token)])
async def get_job(job_id: str) -> dict[str, Any]:
    """轮询任务状态。未知 id（含已被清理）返回 404，前端据此停止轮询。"""
    _prune_jobs()
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job.to_dict()


# ── 快照端点 ─────────────────────────────────────────────────────────────


@router.get("/snapshots", dependencies=[Depends(require_internal_token)])
async def list_snapshots() -> dict[str, Any]:
    """快照列表（最新在前）。

    只回元数据（id / created_at / package_count / trigger / label）：几百个包名对列表页
    没有价值，却会让每次刷新都多传几十 KB。恢复时由后端自己读全文。
    """
    snapshots = list_snapshot_docs()
    return {
        "snapshots": snapshots,
        "count": len(snapshots),
        "retention": MAX_SNAPSHOTS,
        "directory": str(snapshot_root()),
    }


@router.post("/snapshots", dependencies=[Depends(require_internal_token)])
async def create_snapshot(payload: SnapshotRequest | None = None) -> dict[str, Any]:
    """手动创建一份快照（trigger=manual）。

    同步执行：``pip list`` 是秒级操作，不像安装那样会撞网关超时；同步返回才能让前端
    立刻把新快照放进列表。
    """
    label = normalize_label(payload.label if payload is not None else "")
    try:
        doc = await capture_snapshot("manual", label)
    except PipListError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": f"cannot snapshot: {exc}"},
        ) from exc
    except OSError as exc:
        # 落盘失败（目录只读 / 磁盘满）：这是"服务器侧问题"，但原因必须让用户看见，
        # 否则"创建快照没反应"无从排查。
        raise HTTPException(status_code=500, detail=f"cannot write snapshot: {exc}") from exc
    return {"snapshot": snapshot_meta(doc)}


@router.post("/snapshots/{snapshot_id}/restore", dependencies=[Depends(require_internal_token)])
async def restore_snapshot(
    snapshot_id: str,
    payload: RestoreRequest | None = None,
) -> dict[str, Any]:
    """把环境拉回某份快照。

    ``dry_run=true``：只算差异并返回（前端的二次确认弹窗就靠这个）；
    ``dry_run=false``：建后台恢复任务并立即返回 job（批量重装必然超过网关同步超时）。

    顺序即安全边界：id 白名单 → 文件存在 → 快照里的每个条目重新过规格白名单
    （快照是磁盘文件，被改过一次就意味着任意 pip 参数）→ 读当前环境 → 算差异 →
    上限校验 → 单任务闸 → 建任务。
    """
    global _running_job_id

    dry_run = bool(payload.dry_run) if payload is not None else False

    try:
        path = snapshot_path(snapshot_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"snapshot {snapshot_id} not found")

    try:
        snapshot = read_snapshot(snapshot_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"snapshot is unreadable: {exc}") from exc

    for pkg in snapshot["packages"]:
        try:
            validate_package_name(pkg["name"])
            validate_pip_spec(f"{pkg['name']}=={pkg['version']}")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"snapshot contains an invalid package entry: {exc}",
            ) from exc

    try:
        current = await _current_packages()
    except PipListError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": f"cannot read installed packages: {exc}"},
        ) from exc

    diff = compute_restore_diff(snapshot["packages"], current)
    meta = snapshot_meta(snapshot)

    if dry_run:
        return {"dry_run": True, "snapshot": meta, "diff": diff, "job": None}

    if diff["install_count"] > MAX_RESTORE_INSTALLS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"restore would install {diff['install_count']} packages "
                    f"(max {MAX_RESTORE_INSTALLS} per run); split the snapshot or rebuild manually"
                ),
                "diff": diff,
            },
        )

    if diff["total_changes"] == 0:
        # 环境已经与快照一致：不建任务（否则前端要为一个空转任务轮询到底）。
        return {"dry_run": False, "snapshot": meta, "diff": diff, "job": None, "noop": True}

    if _running_job_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "another dependency task is already running",
                "job_id": _running_job_id,
            },
        )

    _prune_jobs()
    job = DepJob(id=uuid.uuid4().hex[:12], kind="restore", target=snapshot_id, message="queued")
    _jobs[job.id] = job
    _running_job_id = job.id
    task = asyncio.create_task(_run_restore_job(job, snapshot, diff))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)

    logger.info("deps restore job queued: %s", snapshot_id, extra={"job_id": job.id})
    return {"dry_run": False, "snapshot": meta, "diff": diff, "job": job.to_dict()}
