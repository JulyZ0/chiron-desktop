"""容器执行后端：规格不可放宽、失败拒跑、超时杀容器、日志解复用。

这些用例**不需要真的安装 Kiri**：Engine 客户端被替身替换，只验证协议与安全不变量。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.isolation import IsolationDecision, Level
from app.isolation import container as container_mod
from app.isolation.container import build_create_body, demux_logs, exec_in_container


def test_create_body_is_hardened(tmp_path: Path):
    body = build_create_body(
        argv=["python", "-c", "print(1)"],
        workspace=str(tmp_path),
        workdir=str(tmp_path),
        image="example:tag",
        user="1000:1000",
    )
    host = body["HostConfig"]
    assert body["Image"] == "example:tag"
    assert body["Cmd"] == ["python", "-c", "print(1)"]
    assert body["WorkingDir"] == str(tmp_path)
    assert body["User"] == "1000:1000"
    assert host["NetworkMode"] == "none"
    assert body["NetworkDisabled"] is True
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["PidsLimit"] > 0 and host["Memory"] > 0
    # 只挂工作区（同路径），不挂宿主其它目录
    assert host["Binds"] == [f"{tmp_path}:{tmp_path}:rw"]
    assert "/tmp" in host["Tmpfs"]
    # 没有任何放宽入口
    assert "Privileged" not in host
    assert "CapAdd" not in host
    assert host["NetworkMode"] != "host"


def test_create_body_never_accepts_privileged_or_host_network(tmp_path: Path):
    body = build_create_body(argv=["true"], workspace=str(tmp_path), workdir=str(tmp_path))
    host = body["HostConfig"]
    assert "Privileged" not in body and "Privileged" not in host
    assert host["NetworkMode"] == "none"


def test_demux_logs_framed():
    raw = b"\x01\x00\x00\x00\x00\x00\x00\x03out" + b"\x02\x00\x00\x00\x00\x00\x00\x03err"
    assert demux_logs(raw) == ("out", "err")


def test_demux_logs_raw_fallback():
    assert demux_logs(b"plain text") == ("plain text", "")


@pytest.mark.asyncio
async def test_engine_unavailable_refuses_without_fallback(monkeypatch):
    monkeypatch.setattr(container_mod.kiri, "probe", lambda **_: None)
    out = await exec_in_container(["true"], workspace="/tmp", workdir="/tmp", timeout=5)
    assert "error" in out
    assert "容器隔离挡位不可用" in out["error"]
    assert "stdout" not in out


class _FakeEngine:
    """极简 Engine 替身：记录调用，按脚本返回。"""

    created: list[dict[str, Any]] = []
    started: list[str] = []
    removed: list[str] = []
    killed: list[str] = []
    wait_mode = "ok"

    def __init__(self, socket: str) -> None:
        self.socket = socket
        _FakeEngine.created.clear()
        _FakeEngine.started.clear()
        _FakeEngine.removed.clear()
        _FakeEngine.killed.clear()

    async def __aenter__(self) -> _FakeEngine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def create(self, body: dict[str, Any]) -> str:
        _FakeEngine.created.append(body)
        return "cid-1"

    async def start(self, container_id: str) -> None:
        _FakeEngine.started.append(container_id)

    async def wait(self, container_id: str) -> int:
        # 第一次（被 kill 之前）挂住，kill 之后立即返回 —— 模拟真实引擎。
        if _FakeEngine.wait_mode == "timeout" and container_id not in _FakeEngine.killed:
            await asyncio.sleep(10)
        return 0

    async def logs(self, container_id: str) -> tuple[str, str]:
        return "hello\n", "warn\n"

    async def kill(self, container_id: str) -> None:
        _FakeEngine.killed.append(container_id)

    async def remove(self, container_id: str) -> None:
        _FakeEngine.removed.append(container_id)


@pytest.mark.asyncio
async def test_exec_success_returns_stdout_and_cleans_up(monkeypatch):
    monkeypatch.setattr(container_mod.kiri, "probe", lambda **_: {"socket": "/tmp/fake.sock"})
    monkeypatch.setattr(container_mod, "_Engine", _FakeEngine)
    _FakeEngine.wait_mode = "ok"

    out = await exec_in_container(["echo", "hi"], workspace="/tmp", workdir="/tmp", timeout=5)
    assert out["stdout"] == "hello\n"
    assert out["stderr"] == "warn\n"
    assert out["exit_code"] == 0
    assert _FakeEngine.started == ["cid-1"]
    assert _FakeEngine.removed == ["cid-1"]
    assert _FakeEngine.created[0]["HostConfig"]["NetworkMode"] == "none"


@pytest.mark.asyncio
async def test_exec_timeout_kills_container(monkeypatch):
    monkeypatch.setattr(container_mod.kiri, "probe", lambda **_: {"socket": "/tmp/fake.sock"})
    monkeypatch.setattr(container_mod, "_Engine", _FakeEngine)
    _FakeEngine.wait_mode = "timeout"

    out = await exec_in_container(["sleep", "10"], workspace="/tmp", workdir="/tmp", timeout=0.01)
    assert out.get("error") == "timeout"
    assert _FakeEngine.killed == ["cid-1"], "超时必须真的杀容器"
    assert _FakeEngine.removed == ["cid-1"]


@pytest.mark.asyncio
async def test_sandbox_refuses_when_container_requested_but_missing(monkeypatch):
    from app.tools import sandbox

    monkeypatch.setattr(
        "app.isolation.resolve_level",
        lambda *a, **k: IsolationDecision(Level.CONTAINER, Level.NONE, ["未检测到 Kiri"], below_floor=True),
    )
    out = await sandbox.run_in_sandbox('python -c "pass"', timeout=5)
    assert "sandbox unavailable" in out.get("error", "")
    assert "Kiri" in out.get("reason", "")


@pytest.mark.asyncio
async def test_sandbox_dispatches_to_container_when_effective(monkeypatch):
    from app.isolation import container as container_module
    from app.tools import sandbox

    monkeypatch.setattr(
        "app.isolation.resolve_level",
        lambda *a, **k: IsolationDecision(Level.CONTAINER, Level.CONTAINER, []),
    )
    calls: list[list[str]] = []

    async def _fake(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(list(argv))
        return {"stdout": "container-ran", "stderr": "", "exit_code": 0, "truncated": False}

    monkeypatch.setattr(container_module, "exec_in_container", _fake)
    out = await sandbox.run_in_sandbox('python -c "pass"', timeout=5)
    assert out["stdout"] == "container-ran"
    assert calls and calls[0][0] == "python"
