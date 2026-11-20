"""``app/migration.py`` 的契约测试：命令构造 / 超时 / 日志 / 失败分类。

这些用例**不碰真实 PG 与真实 alembic**：注入一个 ``runner`` 替身就够把"子进程返回
什么 → 我们抛什么分类"这条判定逻辑钉住。真实的 ``alembic upgrade head`` 由
``scripts/verify_p0.py`` 与 ``--print-and-exit`` 自检覆盖（在单测里起真库会慢十几秒
且容易在 CI 上撞端口）。

钉住的三件事：
1. 命令是 ``<python> -c <bootstrap> <alembic.ini> upgrade head``，且 DSN 只在环境变量里
   （不进命令行 —— 命令行会出现在进程列表里）。
2. 输出**一定**写进 ``<data-dir>/logs/migration.log``，成功与失败都写。
3. 失败分类码是壳层的协议：``migration_tool_missing`` / ``db_unreachable`` /
   ``migration_failed``。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from app.config import Settings
from app.migration import (
    CODE_DB_UNREACHABLE,
    CODE_MIGRATION_FAILED,
    CODE_TOOL_MISSING,
    MIGRATION_TIMEOUT_SECONDS,
    MigrationError,
    alembic_target,
    build_command,
    classify_failure,
    migration_log_path,
    run_alembic_upgrade,
)


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Recorder:
    """记录调用参数，按预设返回/抛出。"""

    def __init__(self, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = result if result is not None else _Proc()
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(command), kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "pg_enabled": False,
        "redis_enabled": False,
        "python": "C:/runtime/python.exe",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _dsn() -> str:
    return "postgresql://chiron:secret@127.0.0.1:50165/chiron"


def test_timeout_is_300_seconds() -> None:
    """超时是产品口径（需求写死 300s），改名/改值等于改行为。"""
    assert MIGRATION_TIMEOUT_SECONDS == 300.0


def test_command_uses_configured_python_and_not_inline_dsn(tmp_path: Path) -> None:
    command = build_command(_settings(tmp_path))
    assert command[0] == "C:/runtime/python.exe"
    assert command[1] == "-c"
    # 引导脚本负责屏蔽 dotenv 再调 alembic（见 migration.py 的 _ALEMBIC_BOOTSTRAP）
    assert "alembic" in command[2]
    assert command[-2:] == ["upgrade", "head"]
    assert not any("secret" in part for part in command), "DSN 不得出现在命令行（进程列表可见）"


def test_python_executable_falls_back_to_shared_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CHIRON_PYTHON`` 是组件管理已有的口径，迁移复用它。"""
    monkeypatch.setenv("CHIRON_PYTHON", "D:/tools/python.exe")
    settings = _settings(tmp_path, python="")
    assert settings.python_executable == "D:/tools/python.exe"


def test_dsn_gets_explicit_psycopg2_driver() -> None:
    assert alembic_target("postgresql://u:p@h:1/db") == "postgresql+psycopg2://u:p@h:1/db"
    # 已带驱动的不重复加
    assert alembic_target("postgresql+psycopg2://u:p@h:1/db") == "postgresql+psycopg2://u:p@h:1/db"


def test_success_writes_log_and_passes_dsn_via_env(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = _Recorder(_Proc(returncode=0, stdout="INFO [alembic] done\n"))

    result = run_alembic_upgrade(settings, _dsn(), runner=recorder)

    assert result["ok"] is True
    assert result["elapsed_ms"] >= 0

    _command, kwargs = recorder.calls[0]
    assert kwargs["env"]["DATABASE_DSN"] == "postgresql+psycopg2://chiron:secret@127.0.0.1:50165/chiron"
    assert kwargs["timeout"] == MIGRATION_TIMEOUT_SECONDS
    assert kwargs["check"] is False

    log = Path(result["log"])
    assert log == migration_log_path(settings)
    text = log.read_text(encoding="utf-8")
    assert "alembic upgrade head" in text
    assert "returncode: 0" in text
    assert "INFO [alembic] done" in text


def test_tool_missing_when_interpreter_absent(tmp_path: Path) -> None:
    recorder = _Recorder(raises=FileNotFoundError("no such file"))

    with pytest.raises(MigrationError) as excinfo:
        run_alembic_upgrade(_settings(tmp_path), _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_TOOL_MISSING
    # 提示必须点名解释器与配置项，用户才知道去改哪个
    assert "C:/runtime/python.exe" in str(excinfo.value)
    assert "CHIRON_LOCAL_PYTHON" in str(excinfo.value)


def test_tool_missing_when_alembic_not_installed(tmp_path: Path) -> None:
    """解释器活着但没装 alembic：退出码同样是 1，只能从输出里认出来。"""
    recorder = _Recorder(_Proc(returncode=1, stderr="ModuleNotFoundError: No module named 'alembic'"))

    with pytest.raises(MigrationError) as excinfo:
        run_alembic_upgrade(_settings(tmp_path), _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_TOOL_MISSING
    assert excinfo.value.returncode == 1


def test_db_unreachable_is_classified(tmp_path: Path) -> None:
    stderr = (
        'sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) '
        'connection to server at "127.0.0.1", port 50165 failed: Connection refused\n'
    )
    recorder = _Recorder(_Proc(returncode=1, stderr=stderr))
    settings = _settings(tmp_path)

    with pytest.raises(MigrationError) as excinfo:
        run_alembic_upgrade(settings, _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_DB_UNREACHABLE
    # 失败也写日志：DB 不可达时的完整堆栈是事后唯一证据
    text = migration_log_path(settings).read_text(encoding="utf-8")
    assert "Connection refused" in text
    assert "returncode: 1" in text


def test_migration_error_is_default_classification(tmp_path: Path) -> None:
    recorder = _Recorder(_Proc(returncode=2, stderr="alembic.util.exc.CommandError: Can't locate revision 'abc'"))

    with pytest.raises(MigrationError) as excinfo:
        run_alembic_upgrade(_settings(tmp_path), _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_MIGRATION_FAILED
    assert "退出码 2" in str(excinfo.value)


def test_timeout_is_migration_failed_and_logged(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    recorder = _Recorder(raises=subprocess.TimeoutExpired(cmd=["python"], timeout=300, output="waiting on lock"))

    with pytest.raises(MigrationError) as excinfo:
        run_alembic_upgrade(settings, _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_MIGRATION_FAILED
    assert "300s" in str(excinfo.value)
    text = migration_log_path(settings).read_text(encoding="utf-8")
    assert "waiting on lock" in text
    assert "returncode: None" in text


def test_missing_migrations_dir_fails_without_running(tmp_path: Path) -> None:
    settings = _settings(tmp_path, python="C:/runtime/python.exe")
    recorder = _Recorder()

    # 把迁移目录指到一个不存在的路径：不跑子进程，直接给出可读原因
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Settings, "migrations_dir", property(lambda self: tmp_path / "nope"))
        with pytest.raises(MigrationError) as excinfo:
            run_alembic_upgrade(settings, _dsn(), runner=recorder)

    assert excinfo.value.code == CODE_MIGRATION_FAILED
    assert "找不到迁移目录" in str(excinfo.value)
    assert recorder.calls == []


def test_classify_failure_markers() -> None:
    assert classify_failure("could not connect to server") == CODE_DB_UNREACHABLE
    assert classify_failure("No module named alembic") == CODE_TOOL_MISSING
    assert classify_failure("") == CODE_MIGRATION_FAILED
