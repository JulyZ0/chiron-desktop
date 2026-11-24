# ruff: noqa: E402 - 本文件按路径加载 migrations/dsn.py（不连数据库、不触发迁移）
"""H32：迁移 DSN 安全与显式配置优先（纯函数回归，不连 PG）。

``migrations/env.py`` 是 alembic 入口脚本，import 即执行迁移，无法直接单测；这里按
文件路径加载 ``migrations/dsn.py``，逐条验证 PRD 9.7 H32 的验收点：

- 显式环境变量优先（``.env`` 不覆盖已设置的 ``DATABASE_DSN``）；
- 默认不输出 DSN（日志只出现脱敏串）；
- 密码只经 URL 编码一次（原始/已编码两种输入都收敛，且重复调用幂等）；
- 迁移前打印脱敏目标与 migration head，错误文本不含凭据。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_DSN_PATH = REPO_ROOT / "migrations" / "dsn.py"
_ENV_PATH = REPO_ROOT / "migrations" / "env.py"


def _load_dsn_module():
    spec = importlib.util.spec_from_file_location("chiron_migrations_dsn", _DSN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dsn = _load_dsn_module()


class TestRedaction:
    def test_password_with_at_and_colon_is_not_leaked(self):
        raw = "postgresql://chiron:p@ss:w0rd@db.internal:5432/chiron"
        redacted = dsn.redact_dsn(raw)
        assert redacted == "postgresql://chiron:***@db.internal:5432/chiron"
        assert "p@ss" not in redacted
        assert "w0rd" not in redacted

    def test_no_password_keeps_host_only(self):
        assert (
            dsn.redact_dsn("postgresql://chiron@db.internal:5432/chiron")
            == "postgresql://chiron@db.internal:5432/chiron"
        )

    def test_unparsable_input_is_fully_masked(self):
        for raw in ("garbage", "postgresql://", "user:pass@host/db", ""):
            assert dsn.redact_dsn(raw) in ("(redacted)", "(none)")
        assert dsn.redact_dsn(None) == "(none)"


class TestSingleEncoding:
    def test_raw_password_is_encoded_once(self):
        normalized = dsn.normalize_dsn("postgres://chiron:p@ss:w0rd@db.internal:5432/chiron")
        assert normalized == "postgresql://chiron:p%40ss%3Aw0rd@db.internal:5432/chiron"

    def test_already_encoded_password_is_not_double_encoded(self):
        normalized = dsn.normalize_dsn(
            "postgresql://chiron:p%40ss%3Aw0rd@db.internal:5432/chiron"
        )
        assert normalized == "postgresql://chiron:p%40ss%3Aw0rd@db.internal:5432/chiron"
        assert "%2540" not in normalized

    def test_normalize_is_idempotent(self):
        samples = [
            "postgres://chiron:p%40ss@db.internal:5432/chiron",
            "postgresql://chiron:p@ss@db.internal/chiron",
            "postgresql://chiron@db.internal:5432/chiron",
            "sqlite:///./local.db",
        ]
        for raw in samples:
            once = dsn.normalize_dsn(raw)
            assert dsn.normalize_dsn(once) == once, raw

    def test_password_without_special_characters_is_unchanged(self):
        raw = "postgresql://chiron:simple123@db.internal:5432/chiron"
        assert dsn.normalize_dsn(raw) == raw


class TestExplicitConfigPrecedence:
    def test_dotenv_does_not_override_explicit_environment(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "DATABASE_DSN=postgresql://dotenv:dotenv@127.0.0.1:5432/dev\n"
            "CHIRON_ONLY_IN_DOTENV=from-dotenv\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("DATABASE_DSN", "postgresql://managed:secret@127.0.0.1:55432/app")
        monkeypatch.delenv("CHIRON_ONLY_IN_DOTENV", raising=False)

        assert dsn.load_env_files([tmp_path / "missing.env", env_file]) == env_file
        # 显式变量优先（托管 PG 通道的核心保证）
        assert dsn.password_of("") == ""
        import os

        assert os.environ["DATABASE_DSN"] == "postgresql://managed:secret@127.0.0.1:55432/app"
        # 未显式设置的值仍由 .env 补齐
        assert os.environ["CHIRON_ONLY_IN_DOTENV"] == "from-dotenv"

    def test_missing_env_files_report_none(self, tmp_path):
        assert dsn.load_env_files([tmp_path / "nope.env"]) is None

    def test_env_py_loads_dotenv_without_override(self):
        source = _ENV_PATH.read_text(encoding="utf-8")
        assert "override=True" not in source
        assert "load_env_files(" in source

    def test_env_py_never_writes_the_url_back_into_ini(self):
        """编码后的密码含 `%`，写回 alembic ini 会触发 ConfigParser 插值异常。"""
        source = _ENV_PATH.read_text(encoding="utf-8")
        assert 'set_main_option("sqlalchemy.url"' not in source
        assert "engine_kwargs[\"url\"] = db_url" in source


class TestBannerAndErrorSanitizing:
    def test_target_line_shows_redacted_target_and_head(self):
        line = dsn.target_line(
            "postgresql://chiron:p@ss@db.internal:5432/chiron",
            "0017_conversation_shares_expires_backfill",
            "env:DATABASE_DSN",
        )
        assert "p@ss" not in line
        assert "postgresql://chiron:***@db.internal:5432/chiron" in line
        assert "head=0017_conversation_shares_expires_backfill" in line
        assert "source=env:DATABASE_DSN" in line

    def test_sanitize_text_masks_raw_and_encoded_password(self):
        raw = "postgresql://chiron:p@ss:w0rd@db.internal:5432/chiron"
        message = (
            "sqlalchemy.exc.OperationalError: could not connect to "
            f"{raw} (also tried postgresql://chiron:p%40ss%3Aw0rd@db.internal:5432/chiron)"
        )
        cleaned = dsn.sanitize_text(
            message, raw, dsn.password_of(raw), dsn.password_of(dsn.normalize_dsn(raw))
        )
        assert "p@ss" not in cleaned
        assert "w0rd" not in cleaned
        assert "p%40ss" not in cleaned
        assert "***" in cleaned

    def test_sanitize_text_leaves_unrelated_text_alone(self):
        assert dsn.sanitize_text("connection refused", "abcdef") == "connection refused"
