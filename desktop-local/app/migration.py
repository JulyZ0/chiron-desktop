"""显式执行 ``alembic upgrade head``（子进程 + 超时 + 日志 + 失败分类）。

**为什么从"进程内 import alembic"改成"起子进程"**：

1. 进程内调用会把 alembic/sqlalchemy 的整个依赖树拉进引导器进程。引导器只负责起
   PG/Redis，一旦迁移依赖缺失（打包漏了 alembic），报错发生在 import 处，
   表现为"引导器崩了"，与"迁移失败"混为一谈，用户无法判断该装什么。
2. 迁移是**外部步骤**（发布流程/DBA 也跑同一条命令）。用同一条 ``alembic upgrade head``
   命令行，本地失败与线上失败的形态一致，日志可以互相照抄。
3. 子进程有真实超时（``MIGRATION_TIMEOUT_SECONDS``）。进程内调用没有可中断的中途点，
   卡在锁等待（另一个迁移会话持有 advisory lock）时引导器会静默挂住，Electron 只能等
   180s 超时并报"引导器没就绪"——真因（迁移在等锁）被完全埋掉。

**失败分类**（``MigrationError.code``，直接进 ``launcher-error`` 事件的 ``code`` 字段）：

===========================  ==========================================================
``migration_tool_missing``   解释器或 alembic 不存在（打包漏装依赖 / CHIRON_PYTHON 指错）
``db_unreachable``           迁移跑起来了，但连不上库（端口错、PG 没起、认证失败）
``migration_failed``         迁移脚本自身报错（含超时）。其它一切归到这里
===========================  ==========================================================

三条都是"启动前的确定性失败"，不做重试：迁移半途重跑可能落在脏 schema 上，
把一次可诊断的失败变成不可诊断的状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.config import Settings

# 迁移超时：仓库里最大的一次迁移在慢盘上实测 40s 量级，300s 是"足够宽但不会无限挂住"。
MIGRATION_TIMEOUT_SECONDS = 300.0

# 日志文件名（落在 ``<data-dir>/logs/`` 下，与 postgres.log / redis.log 同目录）。
MIGRATION_LOG_NAME = "migration.log"

# 失败分类码。Electron 侧（local-stack.cjs / error.html）按这些值给中文提示，
# 改名等于改协议 —— 测试里钉住了。
CODE_TOOL_MISSING = "migration_tool_missing"
CODE_DB_UNREACHABLE = "db_unreachable"
CODE_MIGRATION_FAILED = "migration_failed"

# 「连不上库」的特征串。alembic 把 sqlalchemy 的异常原样打到 stderr，这里只做
# **归类**，不做提示语生成 —— 原始输出会完整写进日志，人可以直接看。
_DB_UNREACHABLE_MARKERS = (
    "could not connect to server",
    "connection refused",
    "connection to server",
    "server closed the connection",
    "could not translate host name",
    "name or service not known",
    "nodename nor servname",
    "password authentication failed",
    "timeout expired",
    "operationalerror",
    "is the server running",
)

# 「工具缺失」的特征串：``python -m alembic`` 在解释器活着但没装 alembic 时
# 退出码同样是 1，只靠 FileNotFoundError 抓不到这一半。
_TOOL_MISSING_MARKERS = (
    "no module named alembic",
    "no module named 'alembic'",
    "no module named \"alembic\"",
    "alembic: command not found",
    "不是内部或外部命令",
)

# 子进程里先屏蔽 dotenv，再调 alembic。
#
# 为什么必须这么做（与 pg.py 里 ``_dotenv_noop`` 同一条理由，但这里跨进程）：
# 仓库根 ``migrations/env.py`` 在模块顶层 ``load_dotenv(..., override=True)``，
# 一份带 ``DATABASE_DSN`` 的开发 ``.env`` 会**覆盖**我们传进去的本地实例 DSN ——
# 迁移就会打到开发库上。进程内可以打补丁，子进程只能在自己的启动引导里做同一件事。
_ALEMBIC_BOOTSTRAP = (
    "import sys\n"
    "try:\n"
    "    import dotenv\n"
    "    dotenv.load_dotenv = lambda *a, **k: False\n"
    "except Exception:\n"
    "    pass\n"
    "from alembic.config import main as _alembic_main\n"
    "sys.exit(_alembic_main(argv=['-c', sys.argv[1], *sys.argv[2:]]))\n"
)


class MigrationError(RuntimeError):
    """迁移失败，带可分类的 ``code`` 与日志路径。

    继承 ``RuntimeError``：既有的 ``except RuntimeError`` 兜底路径（Electron 侧只看
    ``error`` 字符串）不变；新增的 ``code`` 让壳层能给出"该装什么/该查什么"的提示。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        returncode: int | None = None,
        log_path: str = "",
        output_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.log_path = log_path
        self.output_tail = output_tail


def alembic_target(dsn: str) -> str:
    """把 ``postgresql://`` 换成显式 psycopg2 驱动。

    SQLAlchemy 2.1 起 ``postgresql://`` 默认走 psycopg(v3)；迁移环境装的是
    psycopg2-binary（与 ``run_sql`` 同源）。不显式指定就会报
    ``ModuleNotFoundError: No module named 'psycopg'`` —— 看起来像缺依赖，其实是驱动名。
    """
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg2://" + dsn[len("postgresql://") :]
    return dsn


def classify_failure(output: str) -> str:
    """按输出归类失败原因。默认 ``migration_failed``（迁移脚本自身的问题）。"""
    haystack = (output or "").lower()
    if any(marker in haystack for marker in _TOOL_MISSING_MARKERS):
        return CODE_TOOL_MISSING
    if any(marker in haystack for marker in _DB_UNREACHABLE_MARKERS):
        return CODE_DB_UNREACHABLE
    return CODE_MIGRATION_FAILED


def migration_log_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / "logs" / MIGRATION_LOG_NAME


def _write_log(
    settings: Settings,
    *,
    command: Sequence[str],
    cwd: Path,
    returncode: int | None,
    elapsed_ms: float,
    output: str,
) -> str:
    """把一次迁移的完整输出追加到日志，返回日志路径（失败也不抛）。"""
    path = migration_log_path(settings)
    header = "\n".join(
        [
            f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} alembic upgrade head =====",
            f"cwd: {cwd}",
            f"command: {' '.join(command)}",
            f"returncode: {returncode}",
            f"elapsed_ms: {elapsed_ms}",
        ]
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(header + "\n")
            handle.write(output or "")
            if output and not output.endswith("\n"):
                handle.write("\n")
    except OSError:
        # 日志写不进去不该掩盖迁移结果：结果本身已经在返回值/异常里
        return ""
    return str(path)


def build_command(settings: Settings) -> list[str]:
    """构造迁移命令。

    ``python -m alembic`` 的等价物，但先过一层屏蔽 dotenv 的引导（见
    ``_ALEMBIC_BOOTSTRAP``）。解释器来自配置：``CHIRON_LOCAL_PYTHON`` →
    ``CHIRON_PYTHON`` → 当前解释器，与组件管理（``CHIRON_PYTHON``）同一口径。
    """
    return [
        settings.python_executable,
        "-c",
        _ALEMBIC_BOOTSTRAP,
        str(settings.alembic_ini),
        "upgrade",
        "head",
    ]


def run_alembic_upgrade(
    settings: Settings,
    dsn: str,
    *,
    timeout: float = MIGRATION_TIMEOUT_SECONDS,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行 ``alembic upgrade head``，成功返回结果字典，失败抛 :class:`MigrationError`。

    ``runner`` 只为单测注入（默认 ``subprocess.run``）；生产路径永远不传。
    """
    migrations_dir = Path(settings.migrations_dir)
    if not migrations_dir.is_dir():
        raise MigrationError(
            CODE_MIGRATION_FAILED,
            f"找不到迁移目录：{migrations_dir}（检查 MIGRATIONS_DIR / 安装包完整性）",
        )

    target = alembic_target(dsn)
    command = build_command(settings)
    # cwd 固定在仓库根：``alembic.ini`` 里的 ``script_location = migrations`` 是相对路径。
    cwd = migrations_dir.parent
    env = {**os.environ, "DATABASE_DSN": target}
    run = runner or subprocess.run
    started = time.perf_counter()

    try:
        proc = run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MigrationError(
            CODE_TOOL_MISSING,
            f"迁移工具缺失：找不到解释器 {command[0]!r}"
            f"（用 CHIRON_LOCAL_PYTHON 指定一个装了 alembic 的 python）",
        ) from exc
    except PermissionError as exc:
        raise MigrationError(
            CODE_TOOL_MISSING,
            f"迁移工具不可执行：{command[0]!r}（{exc}）",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        output = _decode(exc.stdout) + _decode(exc.stderr)
        log_path = _write_log(
            settings,
            command=command,
            cwd=cwd,
            returncode=None,
            elapsed_ms=elapsed_ms,
            output=output,
        )
        raise MigrationError(
            CODE_MIGRATION_FAILED,
            f"alembic upgrade head 超过 {timeout:.0f}s 未返回（可能是迁移锁等待或脚本卡住，见 {log_path or '日志'}）",
            log_path=log_path,
            output_tail=output[-2000:],
        ) from exc

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    output = f"{getattr(proc, 'stdout', '') or ''}{getattr(proc, 'stderr', '') or ''}"
    log_path = _write_log(
        settings,
        command=command,
        cwd=cwd,
        returncode=getattr(proc, "returncode", None),
        elapsed_ms=elapsed_ms,
        output=output,
    )

    returncode = getattr(proc, "returncode", 1)
    if returncode != 0:
        code = classify_failure(output)
        tail = output.strip()[-2000:]
        raise MigrationError(
            code,
            f"alembic upgrade head 失败（退出码 {returncode}）：{tail or '无输出'}",
            returncode=returncode,
            log_path=log_path,
            output_tail=tail,
        )

    return {
        "ok": True,
        "command": " ".join(command[:1] + ["alembic", "upgrade", "head"]),
        "elapsed_ms": elapsed_ms,
        "migrations_dir": str(migrations_dir),
        "log": log_path,
    }


def _decode(value: Any) -> str:
    """``TimeoutExpired.stdout/stderr`` 可能是 bytes（``text=True`` 下也不例外）。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def python_hint() -> str:
    """当前解释器信息，供日志/错误排查使用。"""
    return f"{sys.executable} ({sys.version.split()[0]})"
