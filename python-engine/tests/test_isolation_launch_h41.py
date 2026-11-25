"""H41：提权 helper 的加载目录与管道排空（PRD 9.2）。

两个缺陷都属于"平时看不出、出事就卡死 / 提权外"的类型：

1. **管道顺序读死锁**：`launch_restricted` 先读完 stdout 再读 stderr。管道缓冲区默认
   4KB，子进程往 stderr 写满之后就阻塞在 write 上，而父进程正阻塞在 stdout 的
   ReadFile 里等 EOF —— 双方互等，永不返回。只在"输出跨过缓冲区边界"时复现。

2. **提权加载用户可写目录**：`spawn_elevated` 把 UAC 进程的 cwd 指到引擎根，靠
   `-m app...` 让 Python 在 cwd 里找包。源码部署时引擎根在用户可写目录 ⇒
   能写那里的人就能让**提权**进程加载自己的 `app/isolation/elevated_helper.py`。

这里钉住判定逻辑（纯函数 / 静态守卫），真实启动流程仍需 Windows 真机。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.isolation import elevated_client

SOURCE = Path(elevated_client.__file__).read_text(encoding="utf-8")
LAUNCH_SRC = (
    Path(elevated_client.__file__).with_name("win_launch.py").read_text(encoding="utf-8")
)


# ── 1. 加载目录必须受保护 ──────────────────────────────────────────────────
def test_rejects_user_writable_engine_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """引擎根落在用户主目录/临时目录时，拒绝启动提权 helper。"""
    home = tmp_path / "home" / "alice"
    engine = home / "code" / "chiron"
    engine.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()

    ok, reason = elevated_client.helper_loading_dir_is_protected(str(engine))
    assert ok is False
    assert "用户可写" in reason


def test_rejects_temp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    engine = temp_root / "engine"
    engine.mkdir(parents=True)
    monkeypatch.setenv("TEMP", str(temp_root))
    monkeypatch.setenv("TMP", str(temp_root))
    monkeypatch.delenv("USERPROFILE", raising=False)

    ok, reason = elevated_client.helper_loading_dir_is_protected(str(engine))
    assert ok is False
    assert "用户可写" in reason


def test_accepts_protected_install_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """受保护目录（模拟 Program Files）应放行。"""
    install = tmp_path / "ProgramFiles" / "Chiron" / "engine"
    install.mkdir(parents=True)
    # 把所有"用户可写"环境变量都指到别处
    for key in ("TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HOMEDRIVE", "")

    ok, reason = elevated_client.helper_loading_dir_is_protected(str(install))
    assert ok is True, reason


def test_rejects_drive_root(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
    ok, reason = elevated_client.helper_loading_dir_is_protected(drive)
    assert ok is False
    assert "盘符根" in reason


def test_rejects_missing_dir() -> None:
    ok, reason = elevated_client.helper_loading_dir_is_protected("/definitely/not/here-xyz")
    assert ok is False
    assert "不存在" in reason


def test_spawn_elevated_checks_the_dir_before_uac() -> None:
    """守卫必须在触发 UAC **之前**：否则用户白点一次同意。"""
    src = SOURCE
    guard_at = src.find("helper_loading_dir_is_protected(working_dir)")
    shell_at = src.find('shell32.ShellExecuteW(')
    assert guard_at != -1, "spawn_elevated 必须调用目录守卫"
    assert shell_at != -1
    assert guard_at < shell_at, "守卫必须在 ShellExecuteW 之前执行"


# ── 2. 管道必须并发排空 ────────────────────────────────────────────────────
def test_both_pipes_are_drained_concurrently() -> None:
    """静态守卫 + 并发函数存在：顺序读会让 stderr 填满 4KB 时死锁。"""
    code = re.sub(r"#.*", "", LAUNCH_SRC)
    assert "_drain_both(" in code, "应有并发排空函数"
    assert "stdout, stderr = _drain_both(" in code, "调用点应改用并发版本"
    assert not re.search(r"stdout = _drain\(out_r", code), "不得保留顺序读"

    # 并发版本用线程各自阻塞读
    assert "threading.Thread" in code
    assert code.count("t.start()") >= 1 and code.count("t.join()") >= 1


def test_drain_both_returns_both_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """用假的 _drain 验证两个流都被读、且返回值对得上。"""
    calls: list[str] = []

    def fake_drain(handle, max_output):  # type: ignore[no-untyped-def]
        calls.append(str(handle))
        return f"data-{handle}"

    from app.isolation import win_launch

    monkeypatch.setattr(win_launch, "_drain", fake_drain)
    out, err = win_launch._drain_both("OUT", "ERR", 1024)
    assert (out, err) == ("data-OUT", "data-ERR")
    assert sorted(calls) == ["ERR", "OUT"], "两根管道都要读"


def test_drain_both_propagates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.isolation import win_launch

    def boom(handle, max_output):  # type: ignore[no-untyped-def]
        raise RuntimeError("read failed")

    monkeypatch.setattr(win_launch, "_drain", boom)
    with pytest.raises(RuntimeError):
        win_launch._drain_both("OUT", "ERR", 1024)
