"""容器执行后端（`container` 隔离挡位）—— 通过 Kiri 的 Docker Engine API 跑一次性容器。

## 边界（与 H2 一致：依赖缺失即拒跑，不回退）

* **引擎不可用** ⇒ 直接返回错误，**不回退**到无命名空间的 `run_in_sandbox` 本地路径；
* **镜像不存在** ⇒ 报"请先预拉镜像"，不自动 pull（拉取会触达网络，属于部署方决策）；
* 容器规格固定为保守集合：`--network none`、只读根文件系统、`CAP_DROP=ALL`、
  `no-new-privileges`、pids/memory 上限、只挂工作区、`/tmp` 用 tmpfs。
  不暴露 privileged / cap-add / host network 的开关 —— 能放宽这些的沙箱就不是沙箱。

## 为什么命令是 argv 而不是 shell 字符串

上游 `run_in_sandbox` 已经用 `create_subprocess_exec` 的做法解析出 argv；
容器也按 argv 直接执行（`Cmd`），不引入 `/bin/sh -c` —— shell 会重新打开
元字符注入面，而命令白名单/逃逸正则只看得到原始文本。

## 工作区语义

容器把**同一个工作区路径**bind mount 进去（`workspace:workspace:rw`），`WorkingDir`
也在其中。命令产生的文件属于容器内的 uid —— 默认取宿主当前 uid:gid
（`SANDBOX_CONTAINER_USER` 可覆盖），避免 root daemon 在用户项目里留下 root 属主文件。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.isolation import kiri

logger = logging.getLogger(__name__)

#: 默认镜像：只保证有 python；团队部署应按项目工具链覆盖
#: （`SANDBOX_CONTAINER_IMAGE`，例如自建 `chiron-sandbox-<stack>`）。
DEFAULT_IMAGE = "python:3.12-slim"

#: 资源上限（与本地基线的 `MEM_LIMIT_BYTES=512MB` 对齐）。
DEFAULT_MEMORY_MB = 512
DEFAULT_PIDS = 256
DEFAULT_TMPFS_MB = 64

_IMAGE_ENV = "SANDBOX_CONTAINER_IMAGE"
_USER_ENV = "SANDBOX_CONTAINER_USER"
_MEMORY_ENV = "SANDBOX_CONTAINER_MEMORY_MB"
_PIDS_ENV = "SANDBOX_CONTAINER_PIDS"

#: 等待容器结束的额外宽限（秒）：超时后 kill 也要给 API 留一点时间。
_KILL_GRACE = 5.0


@dataclass(frozen=True)
class ContainerLimits:
    memory_mb: int = DEFAULT_MEMORY_MB
    pids: int = DEFAULT_PIDS
    tmpfs_mb: int = DEFAULT_TMPFS_MB


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def image_name() -> str:
    return (os.getenv(_IMAGE_ENV) or "").strip() or DEFAULT_IMAGE


def container_user() -> str:
    """容器内 uid:gid。默认跟随宿主当前用户，避免 root 属主文件。"""
    explicit = (os.getenv(_USER_ENV) or "").strip()
    if explicit:
        return explicit
    if hasattr(os, "getuid"):
        return f"{os.getuid()}:{os.getgid()}"
    return ""


def limits() -> ContainerLimits:
    return ContainerLimits(
        memory_mb=_env_int(_MEMORY_ENV, DEFAULT_MEMORY_MB),
        pids=_env_int(_PIDS_ENV, DEFAULT_PIDS),
    )


def build_create_body(
    *,
    argv: list[str],
    workspace: str,
    workdir: str,
    image: str = "",
    user: str = "",
    box: ContainerLimits | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造 `POST /containers/create` 的请求体（纯函数，便于单测）。

    安全项**不可通过参数放宽**（没有 privileged/cap_add/network 参数）：
    这是挡位语义的一部分，不是可调项。
    """
    box = box or limits()
    root = os.path.abspath(workspace)
    cwd = os.path.abspath(workdir or root)
    body: dict[str, Any] = {
        "Image": image or image_name(),
        "Cmd": list(argv),
        "WorkingDir": cwd,
        "Env": [
            "HOME=/tmp",
            "LANG=C.UTF-8",
            "PYTHONDONTWRITEBYTECODE=1",
            "CHIRON_SANDBOX=container",
        ],
        "Labels": {
            "chiron.sandbox": "container",
            "chiron.runtime": f"kiri-{kiri.KIRI_VERSION}",
            **(labels or {}),
        },
        "NetworkDisabled": True,
        "AttachStdout": False,
        "AttachStderr": False,
        "Tty": False,
        "OpenStdin": False,
        "HostConfig": {
            # 只挂工作区；路径保持一致，命令里的相对/绝对路径语义不变。
            "Binds": [f"{root}:{root}:rw"],
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,size={box.tmpfs_mb}m"},
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": box.pids,
            "Memory": box.memory_mb * 1024 * 1024,
            "Init": True,
            "IpcMode": "private",
            "AutoRemove": False,
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
        },
    }
    user = user or container_user()
    if user:
        body["User"] = user
    return body


def demux_logs(raw: bytes) -> tuple[str, str]:
    """拆 Docker 的多路复用日志帧（8 字节头：stream + 3 保留 + 4 字节长度）。

    没有合法头时按**单一 stdout** 处理：部分引擎/配置返回未分帧文本，
    丢掉它比拆错更糟。
    """
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    index = 0
    framed = False
    while index + 8 <= len(raw):
        stream = raw[index]
        size = int.from_bytes(raw[index + 4 : index + 8], "big")
        if stream not in (1, 2) or index + 8 + size > len(raw):
            break
        framed = True
        payload = raw[index + 8 : index + 8 + size]
        (stdout if stream == 1 else stderr).append(payload)
        index += 8 + size
    if not framed:
        return raw.decode("utf-8", errors="replace"), ""
    return (
        b"".join(stdout).decode("utf-8", errors="replace"),
        b"".join(stderr).decode("utf-8", errors="replace"),
    )


def _labels() -> dict[str, str]:
    """尽力带上 tenant/session 标签（取不到就不带，不影响执行）。"""
    out: dict[str, str] = {"chiron.run": uuid.uuid4().hex[:12]}
    try:
        from app.tools.context import get_session_id, get_tenant_id

        tenant = str(get_tenant_id() or "").strip()
        session = str(get_session_id() or "").strip()
        if tenant:
            out["chiron.tenant"] = tenant[:64]
        if session:
            out["chiron.session"] = session[:64]
    except Exception:  # noqa: BLE001 — 标签是观测增强
        pass
    return out


class _Engine:
    """最小 Engine API 客户端（create/start/wait/logs/kill/remove）。"""

    def __init__(self, socket: str) -> None:
        self.socket = socket
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> _Engine:
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=self.socket),
            base_url="http://engine",
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "engine client used outside context manager"
        return self._client

    async def create(self, body: dict[str, Any]) -> str:
        name = f"chiron-sbx-{uuid.uuid4().hex[:10]}"
        resp = await self.client.post("/containers/create", params={"name": name}, json=body)
        if resp.status_code == 404:
            detail = _json_error(resp)
            raise RuntimeError(
                f"镜像不可用（{body.get('Image')}）：{detail or 'engine 返回 404'}；"
                f"请在部署机上预先拉取，或用 {_IMAGE_ENV} 指定镜像"
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"创建容器失败（{resp.status_code}）：{_json_error(resp)}")
        container_id = str((resp.json() or {}).get("Id") or "")
        if not container_id:
            raise RuntimeError("创建容器失败：响应缺少 Id")
        return container_id

    async def start(self, container_id: str) -> None:
        resp = await self.client.post(f"/containers/{container_id}/start")
        if resp.status_code not in (204, 304):
            raise RuntimeError(f"启动容器失败（{resp.status_code}）：{_json_error(resp)}")

    async def wait(self, container_id: str) -> int:
        resp = await self.client.post(
            f"/containers/{container_id}/wait", params={"condition": "not-running"}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"等待容器失败（{resp.status_code}）：{_json_error(resp)}")
        payload = resp.json() or {}
        try:
            return int(payload.get("StatusCode", 0))
        except (TypeError, ValueError):
            return 0

    async def logs(self, container_id: str) -> tuple[str, str]:
        resp = await self.client.get(
            f"/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": "all"},
        )
        if resp.status_code >= 400:
            return "", f"读取容器日志失败（{resp.status_code}）：{_json_error(resp)}"
        return demux_logs(resp.content)

    async def kill(self, container_id: str) -> None:
        try:
            await self.client.post(f"/containers/{container_id}/kill", params={"signal": "SIGKILL"})
        except Exception as exc:  # noqa: BLE001 — kill 失败只影响收尾
            logger.warning("container kill failed (%s): %s", container_id[:12], exc)

    async def remove(self, container_id: str) -> None:
        try:
            await self.client.delete(
                f"/containers/{container_id}", params={"force": "true", "v": "true"}
            )
        except Exception as exc:  # noqa: BLE001 — 删除失败留待运维清理
            logger.warning("container remove failed (%s): %s", container_id[:12], exc)


def _json_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or "")
    except Exception:  # noqa: BLE001
        pass
    return (resp.text or "").strip()[:300]


def _truncate(text: str) -> tuple[str, bool]:
    from app.tools.sandbox import truncate_execute_output

    return truncate_execute_output(text)


async def exec_in_container(
    argv: list[str],
    *,
    workspace: str,
    workdir: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """在一次性容器里执行 *argv*（命令已由上层校验）。

    返回形状与本地执行一致：`{stdout, stderr, exit_code, truncated}`；
    失败/超时返回 `{"error": ...}`（`error="timeout"` 与本地路径同口径）。
    **引擎不可用时直接拒跑**，绝不回退。
    """
    live = kiri.probe(force=True)
    if live is None:
        return {"error": kiri.failure_message()}
    sock = str(live.get("socket") or kiri.socket_path())
    if not sock:
        return {"error": kiri.failure_message()}

    body = build_create_body(
        argv=argv,
        workspace=workspace,
        workdir=workdir or workspace,
        labels=_labels(),
    )
    try:
        async with _Engine(sock) as engine:
            container_id = await engine.create(body)
            try:
                await engine.start(container_id)
                timed_out = False
                try:
                    exit_code = await asyncio.wait_for(engine.wait(container_id), timeout=timeout)
                except TimeoutError:
                    timed_out = True
                    exit_code = -1
                    await engine.kill(container_id)
                    try:
                        await asyncio.wait_for(engine.wait(container_id), timeout=_KILL_GRACE)
                    except Exception:  # noqa: BLE001 — 已 kill，等不到也无妨
                        pass
                stdout, stderr = await engine.logs(container_id)
            finally:
                await engine.remove(container_id)
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — 引擎/网络异常同样拒跑（不回退）
        logger.warning("container exec failed: %s: %s", type(exc).__name__, exc)
        return {"error": f"容器执行失败：{type(exc).__name__}: {exc}"}

    if timed_out:
        return {"error": "timeout", "timeout": timeout, "container_id": container_id}

    stdout_text, out_cut = _truncate(stdout)
    stderr_text, err_cut = _truncate(stderr)
    return {
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": exit_code,
        "truncated": bool(out_cut or err_cut),
        "container_id": container_id,
    }


__all__ = [
    "DEFAULT_IMAGE",
    "ContainerLimits",
    "build_create_body",
    "container_user",
    "demux_logs",
    "exec_in_container",
    "image_name",
    "limits",
]
