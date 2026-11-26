"""L17：后台命令输出必须有界（不能 `communicate()` 全量进内存）。"""

from __future__ import annotations

import pytest

from app.tools import job_runner


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = _FakeStream(chunks)
        self.returncode: int | None = 0
        self.killed = False

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def _wait_killed(self) -> int:
        return self.returncode or 0


@pytest.mark.asyncio
async def test_output_over_limit_is_truncated_and_process_killed(monkeypatch) -> None:
    proc = _FakeProc([b"a" * 60000, b"b" * 60000])

    async def _kill(p) -> None:  # noqa: ANN001
        p.kill()

    monkeypatch.setattr(job_runner, "_kill_proc", _kill)
    out, overflowed = await job_runner._communicate_capped(proc, 100000)

    assert overflowed is True
    assert len(out) == 100000, "超过上限的部分不能留在内存里"
    assert proc.killed is True


@pytest.mark.asyncio
async def test_output_under_limit_is_kept_verbatim() -> None:
    proc = _FakeProc([b"hello", b" world"])
    out, overflowed = await job_runner._communicate_capped(proc, 1024)
    assert out == b"hello world"
    assert overflowed is False
    assert proc.killed is False
