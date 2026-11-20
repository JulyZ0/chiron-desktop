"""随包资产与运行时路径的统一解析（与 Go 侧 ``config/resources.go`` 同一优先级）。

## 为什么需要

打包态引擎的 CWD 是 ``<安装根>/resources/python-engine``（只读），而命令行启动
（开发/运维）时又是仓库根。任何 ``Path("logs")`` / ``Path("data")`` 这样的相对
路径都会随 CWD 漂移：写审计时 mkdir 失败、被各自的 try/except 吞掉 ⇒ 审计静默全丢；
启动 banner 里写相对路径 ⇒ 排障的人根本不知道进程把它解析到了哪。

## 优先级（按顺序取第一个**存在**的）

1. ``CHIRON_RESOURCES_DIR``：桌面壳注入的资产根（打包态 = ``<安装根>/resources``）；
2. ``<python-engine 的父目录>/resources``：安装包里再套一层 resources 的布局；
3. ``<python-engine 的父目录>``：当前 electron-builder 形态（同级的 migrations / web / uv）；
4. 开发态仓库根：从 CWD 与引擎目录向上找含 ``migrations/versions`` 的目录。

显式 env 只决定**先看哪里**，指错时继续看后面的候选 —— 配错路径应该降级，
不该让引擎起不来。所有返回值都是绝对路径；找不到时返回 ``None``，由调用方
决定"报 unknown"还是"报错"。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 资产根显式覆盖（桌面壳 ``local-stack.cjs`` 注入）
RESOURCES_DIR_ENV = "CHIRON_RESOURCES_DIR"
#: 迁移目录显式覆盖（运维逃生口，压过一切资源根探测）
MIGRATIONS_DIR_ENV = "MIGRATIONS_DIR"

_MIGRATIONS_REL = Path("migrations") / "versions"


def engine_dir() -> Path:
    """``python-engine/`` 的绝对路径（本文件在 ``app/`` 下）。"""
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    """引擎的父目录：打包态是 ``resources/``，开发态是仓库根。"""
    return engine_dir().parent


def resources_dir() -> Path | None:
    """``CHIRON_RESOURCES_DIR``（未设置返回 ``None``，不猜）。"""
    raw = (os.environ.get(RESOURCES_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def _find_repo_root(starts: list[Path]) -> Path | None:
    """从 ``starts`` 逐级向上找含 ``migrations/versions`` 的目录。"""
    for start in starts:
        try:
            current = start.resolve()
        except OSError:  # pragma: no cover - resolve 失败极罕见
            continue
        for candidate in (current, *current.parents):
            if (candidate / _MIGRATIONS_REL).is_dir():
                return candidate
    return None


def candidate_roots() -> list[Path]:
    """按优先级排列的候选资产根（绝对路径、已去重）。"""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except OSError:  # pragma: no cover
            return
        key = str(resolved).lower() if os.name == "nt" else str(resolved)
        if key in seen:
            return
        seen.add(key)
        roots.append(resolved)

    add(resources_dir())
    add(install_root() / "resources")
    add(install_root())
    add(_find_repo_root([Path.cwd(), engine_dir()]))
    return roots


def resource_root() -> Path | None:
    """第一个**存在**的候选资产根；全都不可用时返回 ``None``。"""
    for root in candidate_roots():
        if root.is_dir():
            return root
    return None


def resource_path(rel: str | Path) -> Path | None:
    """资产根下 ``rel`` 的绝对路径；所有候选根都没有它时返回 ``None``。

    拒绝 ``..`` 上跳：这个入口只用于"资产根下的固定资产"，任何上跳都会越界。
    """
    relative = Path(rel)
    if relative.is_absolute():
        return relative
    if ".." in relative.parts:
        return None
    for root in candidate_roots():
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


def migrations_dir() -> Path | None:
    """Alembic 迁移目录：``MIGRATIONS_DIR`` 优先，否则资产根下的 ``migrations/versions``。"""
    raw = (os.environ.get(MIGRATIONS_DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return resource_path(_MIGRATIONS_REL)


def web_root() -> Path | None:
    """前端产物目录（含 index.html）：打包态 ``<资源根>/web``，开发态 ``frontend-vue/dist``。"""
    for root in candidate_roots():
        for rel in (Path("web"), Path("frontend-vue") / "dist"):
            directory = root / rel
            if (directory / "index.html").is_file():
                return directory
    return None


def data_dir(configured: str = "") -> Path:
    """引擎持久数据根（绝对）。

    ``DATA_DIR``/``settings.data_dir`` 给了就用它；否则 ``<CWD>/data`` —— 与历史
    默认的**位置语义**一致，只是不再返回相对路径（banner 与错误信息里要能照抄）。
    """
    raw = (configured or "").strip()
    base = Path(raw).expanduser() if raw else Path.cwd() / "data"
    return base.resolve()


def log_dir(configured: str = "", data_root: str = "") -> Path:
    """审计/崩溃日志目录（绝对）。

    优先级：``LOG_DIR``（``settings.log_dir``）→ ``<data_dir>/logs`` → ``<CWD>/logs``。
    后两档以前是相对路径：打包态 CWD 只读，mkdir 静默失败、审计全丢；改成绝对路径
    后同样的失败至少会落到一个可诊断的位置（并仍然只告警，不影响主流程）。
    """
    raw = (configured or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = (data_root or "").strip()
    if root:
        return (Path(root).expanduser() / "logs").resolve()
    return (Path.cwd() / "logs").resolve()
