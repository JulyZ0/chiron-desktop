"""``app/paths.py`` 的解析优先级（打包态 / 开发态）。

与 Go 侧 ``config/resources_test.go`` 对称：用临时目录摆出安装包布局，
断言 migrations / web 解析到**绝对路径**，并覆盖"env 指错时继续往下看"。

这些用例只 import ``app.paths``（纯 stdlib），不拉起 Settings/DB ——
否则一条路径单测就得先满足一串启动期校验（APP_SECRET 等）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import paths


def _mk_migrations(root: Path) -> Path:
    versions = root / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "0001_baseline.py").write_text("# fixture\n", encoding="utf-8")
    return versions


@pytest.fixture()
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """模拟安装包布局：``<install>/resources/{python-engine,migrations,web}``。"""
    resources = tmp_path / "install" / "resources"
    engine = resources / "python-engine"
    (engine / "app").mkdir(parents=True)
    _mk_migrations(resources)
    (resources / "web").mkdir(parents=True)
    (resources / "web" / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.delenv(paths.RESOURCES_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.MIGRATIONS_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "engine_dir", lambda: engine)
    # CWD 与安装目录无关（打包态从开始菜单启动就是这样）。
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return resources


def test_packaged_layout_resolves_absolute_migrations(packaged: Path) -> None:
    assert paths.resource_root() == packaged.resolve()
    got = paths.migrations_dir()
    assert got == (packaged / "migrations" / "versions").resolve()
    assert got.is_absolute()


def test_packaged_layout_resolves_web_root(packaged: Path) -> None:
    assert paths.web_root() == (packaged / "web").resolve()


def test_env_override_wins_over_packaged(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    _mk_migrations(pinned)
    monkeypatch.setenv(paths.RESOURCES_DIR_ENV, str(pinned))

    assert paths.migrations_dir() == (pinned / "migrations" / "versions").resolve()
    assert paths.candidate_roots()[0] == pinned.resolve()


def test_broken_env_falls_through(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # env 指到空目录（少一层 / 指到 data 目录的典型错配）：必须继续看安装目录。
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    monkeypatch.setenv(paths.RESOURCES_DIR_ENV, str(wrong))

    assert paths.migrations_dir() == (packaged / "migrations" / "versions").resolve()


def test_migrations_env_beats_resolver(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit-migrations"
    explicit.mkdir()
    monkeypatch.setenv(paths.MIGRATIONS_DIR_ENV, str(explicit))
    assert paths.migrations_dir() == explicit


def test_resource_path_rejects_parent_traversal(packaged: Path) -> None:
    for rel in ("../etc/passwd", "a/../../b"):
        assert paths.resource_path(rel) is None


def test_missing_resource_returns_none(
    packaged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-install"
    (empty / "python-engine").mkdir(parents=True)
    monkeypatch.setattr(paths, "engine_dir", lambda: empty / "python-engine")
    # 只测"安装目录里没有资产"这一档；仓库根回落单独由 dev 用例覆盖。
    # （--basetemp 落在仓库内时，向上走会真的命中仓库的 migrations，必须摘掉。）
    monkeypatch.setattr(paths, "_find_repo_root", lambda starts: None)
    assert paths.resource_path("migrations/versions") is None
    assert paths.web_root() is None


def test_log_dir_is_always_absolute(tmp_path: Path) -> None:
    # 1) 显式 LOG_DIR 原样（绝对化）
    assert paths.log_dir(str(tmp_path / "logs")) == (tmp_path / "logs").resolve()
    # 2) 无 LOG_DIR 时挂在 data_dir 下 —— 打包态与 deps 快照同一根
    assert paths.log_dir("", str(tmp_path / "data")) == (tmp_path / "data" / "logs").resolve()
    # 3) 都没有：CWD/logs 的**绝对**路径（位置语义与历史一致，但可照抄）
    fallback = paths.log_dir("", "")
    assert fallback.is_absolute()
    assert fallback == (Path.cwd() / "logs").resolve()


def test_data_dir_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert paths.data_dir(str(tmp_path / "data")) == (tmp_path / "data").resolve()
    monkeypatch.chdir(tmp_path)
    fallback = paths.data_dir("")
    assert fallback.is_absolute()
    assert fallback == tmp_path / "data"


def test_dev_repo_root_found_by_walking_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开发态：CWD 在仓库深处，向上才能看到 migrations/versions。"""
    repo = tmp_path / "chiron-desktop"
    versions = _mk_migrations(repo)
    engine = tmp_path / "go-build" / "python-engine"
    (engine / "app").mkdir(parents=True)
    deep = repo / "python-engine" / "app" / "api"
    deep.mkdir(parents=True)

    monkeypatch.delenv(paths.RESOURCES_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.MIGRATIONS_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "engine_dir", lambda: engine)
    monkeypatch.chdir(deep)

    assert paths.migrations_dir() == versions.resolve()
    assert os.sep in str(paths.migrations_dir())
