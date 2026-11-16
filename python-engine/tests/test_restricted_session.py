"""`RestrictedSession` / `_RestrictedWorkerIO` 的替身测试（无需管理员权限）。

真实受限进程要 `CreateProcessAsUserW` + `SeIncreaseQuotaPrivilege`（普通账户实测
`err=5`），因此这里用假 kernel32 与假会话锁定**适配层语义**：按流取值、超时终结、
`str`/`bytes` 写入、行缓冲的 EOF 收尾。

这些是 `terminal.py` / `run_code.py` 依赖的契约。它们漂移时的症状是「命令莫名跑满
timeout」或「IPC 丢消息」—— 在真实受限环境里都难以复现，所以必须在这里钉住。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from app.isolation import win_launch
from app.tools.run_code import _RestrictedStderr, _RestrictedStdout

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


def _set_ref(ref: Any, value: int) -> None:
    """把值写回 ctypes 引用。

    调用方传的是 `ctypes.byref(obj)`，拿到的是 `CArgObject` —— 它本身没有
    `.value` setter，需要回到被引用的原对象上赋值。
    """
    getattr(ref, "_obj", ref).value = value


class _FakeKernel32:
    """只实现 `RestrictedSession` 实际调用到的 6 个入口。"""

    def __init__(self, *, exited: bool = False, exit_code: int = 0, write_ok: bool = True) -> None:
        self.exited = exited
        self.exit_code = exit_code
        self.write_ok = write_ok
        self.terminated: list[int] = []
        self.closed: list[Any] = []
        self.written: list[bytes] = []

    def WaitForSingleObject(self, handle: Any, ms: int) -> int:
        return WAIT_OBJECT_0 if self.exited else WAIT_TIMEOUT

    def GetExitCodeProcess(self, handle: Any, ref: Any) -> bool:
        _set_ref(ref, self.exit_code)
        return True

    def TerminateProcess(self, handle: Any, code: int) -> bool:
        self.terminated.append(code)
        self.exited = True
        self.exit_code = code
        return True

    def CloseHandle(self, handle: Any) -> bool:
        self.closed.append(handle)
        return True

    def WriteFile(self, handle: Any, data: bytes, length: int, written: Any, overlapped: Any) -> bool:
        if not self.write_ok:
            return False
        self.written.append(bytes(data))
        _set_ref(written, length)
        return True

    def ReadFile(self, *args: Any) -> bool:
        return False


def _session(k32: _FakeKernel32 | None = None) -> win_launch.RestrictedSession:
    return win_launch.RestrictedSession(
        pid=4242,
        hprocess="hp",
        hthread="ht",
        stdin_write="stdin",
        k32=k32 or _FakeKernel32(),
    )


# ── 按流取值：合并取会让行式 IPC 丢消息 ────────────────────────────


def test_take_stream_isolates_out_and_err():
    session = _session()
    session._out_chunks.extend([b"line-a\n"])
    session._err_chunks.extend([b"warn\n"])

    assert session.take_stream("out") == "line-a\n"
    # 取 stdout 不能连带清空 stderr —— 否则 worker 的报错就永久丢了
    assert session.take_stream("err") == "warn\n"
    assert session.take_stream("out") == ""


def test_take_output_is_incremental():
    session = _session()
    session._out_chunks.append(b"one")
    assert session.take_output() == ("one", "")

    session._err_chunks.append(b"two")
    assert session.take_output() == ("", "two")
    # 取完即清空，重复调用不再返回旧数据
    assert session.take_output() == ("", "")


def test_take_stream_decodes_invalid_utf8_without_raising():
    session = _session()
    session._out_chunks.append(b"\xff\xfe bad")
    assert "bad" in session.take_stream("out")


# ── 生命周期 ──────────────────────────────────────────────────────


def test_poll_reports_exit_code_once_exited():
    session = _session(_FakeKernel32(exited=True, exit_code=7))
    assert session.poll() == 7
    assert session.returncode == 7


def test_poll_returns_none_while_running():
    session = _session(_FakeKernel32(exited=False))
    assert session.poll() is None
    # 重复轮询不应把「还在跑」缓存成退出码
    assert session.returncode is None


def test_wait_sync_terminates_on_timeout():
    k32 = _FakeKernel32(exited=False)
    session = _session(k32)

    code = session.wait_sync(timeout=0.05)

    assert code == 124
    # 超时必须真的终结进程，否则调用方线程会一直挂在轮询里
    assert k32.terminated == [124]


def test_wait_sync_returns_immediately_when_exited():
    session = _session(_FakeKernel32(exited=True, exit_code=0))
    assert session.wait_sync(timeout=5.0) == 0


def test_close_releases_handles_and_is_idempotent():
    k32 = _FakeKernel32()
    session = _session(k32)

    session.close()
    first = len(k32.closed)
    session.close()

    assert first == 3  # stdin 写端 + 进程 + 线程
    assert len(k32.closed) == first


# ── stdin 适配：两条调用方共用 ────────────────────────────────────


def test_stdin_write_accepts_str_and_bytes():
    k32 = _FakeKernel32()
    session = _session(k32)

    session.stdin.write("hello")
    session.stdin.write(b"world")

    assert k32.written == [b"hello", b"world"]


def test_stdin_write_raises_when_pipe_closed():
    session = _session(_FakeKernel32(write_ok=False))
    with pytest.raises(BrokenPipeError):
        session.stdin.write(b"x")


def test_stdin_flush_and_drain_are_noop():
    session = _session()
    session.stdin.flush()  # 同步实现下没有缓冲要冲刷；缺少它会在 IPC 首条消息就崩
    asyncio.run(session.stdin.drain())


# ── Popen 适配层：行缓冲与 EOF 收尾 ───────────────────────────────


class _FakeStreamSession:
    """`_RestrictedStdout` / `_RestrictedStderr` 只需要 `take_stream` + `returncode`。"""

    def __init__(self, chunks: list[str], exit_code: int | None = None) -> None:
        self._chunks = list(chunks)
        self._exit_code = exit_code
        self.pid = 1

    def take_stream(self, which: str) -> str:
        return self._chunks.pop(0) if self._chunks else ""

    @property
    def returncode(self) -> int | None:
        return self._exit_code


def test_restricted_stdout_splits_lines_across_chunks():
    fake = _FakeStreamSession(["partial", " tail\nsecond\n"], exit_code=0)
    stdout = _RestrictedStdout(fake)

    assert stdout.readline() == "partial tail\n"
    assert stdout.readline() == "second\n"


def test_restricted_stdout_returns_tail_then_eof_after_exit():
    fake = _FakeStreamSession(["no-newline"], exit_code=0)
    stdout = _RestrictedStdout(fake)

    assert stdout.readline() == "no-newline"
    assert stdout.readline() == ""


def test_restricted_stderr_iterates_until_eof():
    fake = _FakeStreamSession(["a\nb\n"], exit_code=0)
    lines = list(_RestrictedStderr(fake))

    assert lines == ["a\n", "b\n"]


# ── terminal：受限档哨兵必须落在 workspace ────────────────────────


class _FakeShellStdin:
    """收到命令脚本后「执行」它：建出 out/code/done 三个哨兵。"""

    def __init__(self) -> None:
        self.workdirs: list[Path] = []

    def write(self, data: bytes | str) -> None:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        match = re.search(r'> "([^"]+)[\\/]out\.txt"', text)
        if not match:
            return
        workdir = Path(match.group(1))
        self.workdirs.append(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "out.txt").write_text("boundary-ok\n", encoding="utf-8")
        (workdir / "code.txt").write_text("0", encoding="utf-8")
        (workdir / "done.txt").write_text("", encoding="utf-8")

    async def drain(self) -> None:
        return None


class _FakeShell:
    def __init__(self) -> None:
        self.stdin = _FakeShellStdin()
        self.returncode: int | None = None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_restricted_shell_writes_sentinels_under_workspace():
    """受限会话的哨兵目录必须落在 workspace。

    受限进程只被授权写工作区，往系统 `%TEMP%` 写会 Access Denied；哨兵写不出来
    ⇒ `done_file` 永不出现 ⇒ 每条命令都跑满 timeout 才返回「超时」。命令其实
    早就成功了，这是静默失败。
    """
    from app.tools.sandbox import workspace_dir
    from app.tools.terminal import PersistentTerminal

    terminal = PersistentTerminal()
    fake = _FakeShell()
    key = "restricted-sentinel"
    terminal._procs[key] = fake
    terminal._sessions[key] = fake  # 标记为受限档：execute() 据此选哨兵目录

    try:
        result = await terminal.execute(key, "echo hi")
    finally:
        terminal._procs.clear()
        terminal._sessions.clear()

    assert result["output"] == "boundary-ok\n"
    assert result["persistent"] is True
    assert len(fake.stdin.workdirs) == 1
    assert fake.stdin.workdirs[0].parent == workspace_dir()


@pytest.mark.asyncio
async def test_non_restricted_shell_uses_system_temp():
    """反向对照：普通档不该把临时目录塞进用户工作区（否则工作区会被临时文件污染）。"""
    from app.tools.sandbox import workspace_dir
    from app.tools.terminal import PersistentTerminal

    terminal = PersistentTerminal()
    fake = _FakeShell()
    key = "plain-sentinel"
    terminal._procs[key] = fake

    try:
        await terminal.execute(key, "echo hi")
    finally:
        terminal._procs.clear()
        terminal._sessions.clear()

    assert fake.stdin.workdirs[0].parent != workspace_dir()
