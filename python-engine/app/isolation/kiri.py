"""Kiri 容器运行时的**按需获取**与检测（不内置任何二进制）。

## 为什么是 Kiri

团队模式的执行隔离此前只有「命令白名单 + RLIMIT + 逃逸拦截」，**没有命名空间 /
seccomp**（见 `sandbox-service/service.py` 的模块说明），H2 的缺口就在这里。
`container` 挡位把每条命令放进一个一次性容器：独立 namespaces/cgroup、`--network none`、
只读根文件系统、`CAP_DROP=ALL`、`no-new-privileges`、pids/memory 上限。

Kiri 是一个 Docker Desktop 替代品（Rust daemon + 自家 agent，无 dockerd/containerd/
shim/runc），**说 Docker Engine API**，所以本模块只实现 Engine API 客户端 —— Docker、
containerd、Podman 等同一协议的引擎也能接（`SANDBOX_CONTAINER_SOCKET`）。

## 不内置、按需下载

仓库里**没有** Kiri 包，也没有 docker CLI。缺运行时的时候：

1. `status()` 告诉界面"装没装、为什么不可用、能下载什么"；
2. `prepare()` 按平台下载**固定版本**的官方产物到缓存目录，并用**内嵌的 SHA-256** 校验；
3. 安装/启用需要管理员动作（Linux 是 `.deb`/`.rpm` + `kiri host-access`），这里**不**
   静默提权，只返回可执行的安装命令。桌面端同理（dmg/msi 交给人点）。

版本固定 `0.2.2`：这是 beta 产品，升级必须显式改这里并重新核对哈希，不能"装到最新"。

## 边界

* 只支持 Engine API over **Unix socket**（Linux/macOS）。Windows 的 Kiri 走命名管道，
  本版没有驱动 ⇒ `container` 挡位在 Windows 上不可用（有明确原因，不静默降级）。
* 下载只落缓存；安装是运维动作。`CHIRON_KIRI_AUTO_INSTALL=1` 时才尝试无提权安装
  （仅 Windows MSI 形态；Linux 仍需 root）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: 固定版本（beta）：升级 = 改版本 + 重新核对每个资产的 SHA-256。
KIRI_VERSION = "0.2.2"

#: 官方发布地址（GitHub Releases）。**不跟随"最新版"**。
_RELEASE_BASE = f"https://github.com/libracoder/kiri/releases/download/v{KIRI_VERSION}"

#: 内嵌的资产表：(系统, 架构, 包类型) → (文件名, SHA-256)。
#: 哈希来自该版本随发布提供的 `SHA256SUMS`，内嵌是为了**不信任下载时的远端校验和**。
_ASSETS: dict[tuple[str, str, str], tuple[str, str]] = {
    ("linux", "amd64", "deb"): (
        "kiri-lite_0.2.2_amd64.deb",
        "24798125ee50bb24db86594a9ee1469b1032d9b2ffd7da616b06e40763c723b9",
    ),
    ("linux", "arm64", "deb"): (
        "kiri-lite_0.2.2_arm64.deb",
        "5388bd5651db97a23d7eb9f057881522cbad8a27b3463d293f337b8c8797d7f2",
    ),
    ("linux", "amd64", "rpm"): (
        "kiri-lite-0.2.2-1.x86_64.rpm",
        "9d0743e224d2729648703234d8b634f122f5e3ee545538bf8cccbb74cb4237f0",
    ),
    ("linux", "arm64", "rpm"): (
        "kiri-lite-0.2.2-1.aarch64.rpm",
        "91bbab3fc5f27cc41d546ba80d45531234ace3316bd9b7aaf4bf1e7b0d4b9a19",
    ),
    # macOS / Windows 只下载、不代装（dmg 需要拖拽；Windows 的引擎是 WSL2 命名管道，
    # 本版没有驱动 ⇒ 下载了也用不上，故不列出）。
    ("darwin", "arm64", "dmg"): (
        "Kiri-0.2.2-arm64.dmg",
        "0f0ed39fc08f971c33ead60371c9d58768d993a853407e136f63f62a7edc9864",
    ),
    ("darwin", "amd64", "dmg"): (
        "Kiri-0.2.2-x86_64.dmg",
        "f78299a131d28801bcdfafd4bc0af8737441181d217110ee955ce251691d83d9",
    ),
}

#: socket 候选（按优先级）。**只认 Kiri**：这是"用 Kiri 做容器挡位"的语义，
#: 不能因为机器上恰好跑着 Docker 就悄悄换成 Docker（那不是用户选的东西）。
#: 要显式使用 Docker/Podman 等兼容引擎，设 `SANDBOX_CONTAINER_SOCKET`。
_SOCKET_CANDIDATES = (
    "/run/kiri/docker.sock",
    "~/.kiri/run/docker.sock",
)

_SOCKET_ENV = "SANDBOX_CONTAINER_SOCKET"
_KIRI_SOCKET_ENV = "KIRI_SOCKET"
_CACHE_ENV = "CHIRON_KIRI_CACHE_DIR"

#: 探测缓存（秒）。`status()` 会被设置页反复调用，不值得每次都打 socket。
_PROBE_TTL = 10.0
_probe_at: float = 0.0
_probe: dict[str, Any] | None = None

#: 引擎名/版本（探测成功后填充，给界面显示"到底连的是谁"）。
ENGINE_PING_TIMEOUT = 2.0


def _platform_key() -> tuple[str, str]:
    sysname = sys.platform
    if sysname.startswith("linux"):
        system = "linux"
    elif sysname == "darwin":
        system = "darwin"
    elif sysname.startswith("win"):
        system = "windows"
    else:
        system = ""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )
    return system, arch


def _distro_kind() -> str:
    """Linux 包类型：Debian 系 → deb，RPM 系 → rpm，其余空串（不猜）。"""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    family = " ".join((fields.get("ID", ""), fields.get("ID_LIKE", ""))).lower()
    if any(name in family for name in ("debian", "ubuntu", "linuxmint", "pop")):
        return "deb"
    if any(name in family for name in ("rhel", "fedora", "centos", "rocky", "almalinux", "suse")):
        return "rpm"
    return ""


def asset_for_platform() -> tuple[str, str, str, str] | None:
    """本机对应的 `(kind, 文件名, sha256, 下载 URL)`；不支持返回 None。"""
    system, arch = _platform_key()
    if system == "linux":
        kind = _distro_kind()
        if not kind:
            return None
    elif system == "darwin":
        kind = "dmg"
    else:
        return None
    entry = _ASSETS.get((system, arch, kind))
    if entry is None:
        return None
    name, digest = entry
    return kind, name, digest, f"{_RELEASE_BASE}/{name}"


def socket_path() -> str:
    """实际使用的 Engine API socket；不存在返回空串。"""
    explicit = (os.getenv(_SOCKET_ENV) or os.getenv(_KIRI_SOCKET_ENV) or "").strip()
    candidates = ([explicit] if explicit else []) + list(_SOCKET_CANDIDATES)
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.exists():
            return str(path)
    return ""


def _ping(socket: str) -> dict[str, Any] | None:
    """走 Unix socket 打 `/_ping` + `/version`；失败返回 None。"""
    try:
        transport = httpx.HTTPTransport(uds=socket)
        with httpx.Client(transport=transport, base_url="http://engine", timeout=ENGINE_PING_TIMEOUT) as client:
            ping = client.get("/_ping")
            if ping.status_code != 200 or ping.text.strip().upper() != "OK":
                return None
            try:
                version = client.get("/version").json()
            except Exception:  # noqa: BLE001 — 版本不是判定条件
                version = {}
            return {
                "api_version": str(version.get("ApiVersion") or ""),
                "engine_version": str(version.get("Version") or ""),
                "os": str(version.get("Os") or ""),
                "arch": str(version.get("Arch") or ""),
            }
    except Exception as exc:  # noqa: BLE001 — 探测失败 = 不可用
        logger.debug("kiri probe failed (socket=%s): %s", socket, exc)
        return None


def probe(*, force: bool = False) -> dict[str, Any] | None:
    """缓存的可用性探测；不可用返回 None。"""
    global _probe_at, _probe
    now = time.monotonic()
    if not force and _probe is not None and now - _probe_at < _PROBE_TTL:
        return _probe or None
    sock = socket_path()
    _probe_at = now
    _probe = None
    if not sock:
        return None
    info = _ping(sock)
    if info is None:
        return None
    info["socket"] = sock
    _probe = info
    return _probe


def reset_probe() -> None:
    """清掉探测缓存（安装/启用运行时之后调用）。"""
    global _probe_at, _probe
    _probe_at = 0.0
    _probe = None


def available() -> bool:
    return probe() is not None


def unavailable_reason() -> str:
    """给界面/拒跑错误用的**可操作**原因。"""
    system, _arch = _platform_key()
    if system == "windows":
        return (
            "Windows 上的 Kiri 通过命名管道提供 Docker API，本版没有该驱动；"
            "请使用 appcontainer/restricted 挡位。"
        )
    if system not in ("linux", "darwin"):
        return f"当前平台（{sys.platform}）不支持容器隔离挡位。"
    if not socket_path():
        asset = asset_for_platform()
        if asset is None:
            return "未检测到 Kiri，且当前发行版/架构没有可下载的安装包（请手动安装）。"
        return (
            "未检测到 Kiri。可在设置页下载运行时包（"
            f"{asset[1]}，SHA-256 已内嵌校验），或手动安装后启用。"
        )
    return "Kiri socket 存在但 Engine API 不可达（运行时未启动或权限不足）。"


def cache_dir() -> Path:
    raw = (os.getenv(_CACHE_ENV) or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".chiron" / "runtimes"
    return base / "kiri" / KIRI_VERSION


def cached_artifact() -> Path | None:
    """已下载且校验过的安装包（按文件名找）。"""
    asset = asset_for_platform()
    if asset is None:
        return None
    path = cache_dir() / asset[1]
    if not path.is_file():
        return None
    try:
        return path if _sha256(path) == asset[2] else None
    except OSError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    """流式下载到临时文件（不执行、不覆盖已存在文件）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in resp.iter_bytes(1024 * 256):
                        out.write(chunk)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def install_hint(path: Path | None = None) -> str:
    """**不代提权**的安装命令（Linux 需要 root；macOS 手动拖拽）。"""
    system, _arch = _platform_key()
    target = str(path) if path is not None else "<下载的安装包>"
    if system == "linux":
        kind = _distro_kind()
        if kind == "deb":
            return f"sudo dpkg -i {target} && sudo kiri host-access $USER"
        if kind == "rpm":
            return f"sudo rpm -U {target} && sudo kiri host-access $USER"
        return "请按发行版用包管理器安装，然后执行 sudo kiri host-access $USER"
    if system == "darwin":
        return f"open {target}  # 拖入 Applications 后打开 Kiri 并允许访问"
    return "当前平台不支持容器挡位"


def status() -> dict[str, Any]:
    """给 `/v1/system/isolation` 与 CLI 的运行时快照。"""
    system, arch = _platform_key()
    asset = asset_for_platform()
    live = probe()
    cached = cached_artifact()
    return {
        "version": KIRI_VERSION,
        "platform": f"{system}/{arch}",
        "supported": asset is not None,
        "socket": socket_path(),
        "installed": live is not None,
        "engine": live,
        "reason": "" if live is not None else unavailable_reason(),
        "asset": {
            "name": asset[1],
            "sha256": asset[2],
            "url": asset[3],
            "cached": str(cached) if cached is not None else "",
        }
        if asset is not None
        else None,
        "install_hint": install_hint(cached),
    }


@dataclass(frozen=True)
class PrepareResult:
    ok: bool
    state: dict[str, Any]
    downloaded: str = ""
    error: str = ""


def prepare() -> PrepareResult:
    """按需获取：运行时已在 → 直接成功；否则下载并校验安装包（**不安装**）。

    下载失败/哈希不符一律返回错误，且不会把 `.part` 或未校验的文件留在缓存里。
    """
    live = probe(force=True)
    if live is not None:
        return PrepareResult(ok=True, state=status())

    asset = asset_for_platform()
    if asset is None:
        return PrepareResult(ok=False, state=status(), error=unavailable_reason())

    cached = cached_artifact()
    if cached is not None:
        return PrepareResult(ok=False, state=status(), downloaded=str(cached),
                             error="安装包已下载，但仍需安装并启用运行时：" + install_hint(cached))

    _kind, name, digest, url = asset
    dest = cache_dir() / name
    try:
        _download(url, dest)
        actual = _sha256(dest)
        if actual != digest:
            dest.unlink(missing_ok=True)
            return PrepareResult(
                ok=False,
                state=status(),
                error=f"下载校验失败：期望 {digest}，实际 {actual}（已删除）",
            )
    except Exception as exc:  # noqa: BLE001 — 下载失败给可读原因，不抛给 HTTP 层
        dest.unlink(missing_ok=True)
        return PrepareResult(ok=False, state=status(), error=f"下载失败：{exc}")

    return PrepareResult(
        ok=False,
        state=status(),
        downloaded=str(dest),
        error="已下载并校验安装包，安装/启用需要管理员动作：" + install_hint(dest),
    )


def failure_message() -> str:
    """执行侧拒跑时的一句话（fail-closed，不带回退）。"""
    return (
        f"容器隔离挡位不可用：{unavailable_reason()} "
        f"（Kiri {KIRI_VERSION}；这是依赖缺失，不回退到无命名空间的执行）"
    )


__all__ = [
    "KIRI_VERSION",
    "PrepareResult",
    "asset_for_platform",
    "available",
    "cache_dir",
    "cached_artifact",
    "failure_message",
    "install_hint",
    "prepare",
    "probe",
    "reset_probe",
    "socket_path",
    "status",
    "unavailable_reason",
]
