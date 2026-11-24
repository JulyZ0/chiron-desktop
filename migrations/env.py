"""
Alembic env configuration.

Resolves the database URL from:
  1. DATABASE_DSN environment variable
  2. alembic.ini fallback

**故意不读 `POSTGRES_DSN`** —— 两个变量面向不同对象，不是笔误，也不是待修的缺口：

- ``POSTGRES_DSN``  开发环境用：用户/开发者自备的 PG（本机、Docker、共享实例）。
  由 ``config/config.go`` 与 ``desktop-local/app/config.py`` 读取，是「连哪个库」的
  常规配置。
- ``DATABASE_DSN``  安装包内用：本文件只服务**托管 PG** 的迁移通道。桌面壳拉起
  embedded PostgreSQL 后，由 ``desktop-local/app/storage/pg.py`` 在
  ``alembic upgrade head`` 前后**临时写入 / 恢复**该变量，并用 ``_dotenv_noop()``
  屏蔽 ``.env``，确保迁移只落在自己的实例上（事后还用 ``alembic_version`` 复核）。

所以在开发环境想用 ``.env`` 里的 ``POSTGRES_DSN`` 跑迁移，必须显式转一次名：

    DATABASE_DSN="$POSTGRES_DSN" python -m alembic upgrade head

两条通道分开的理由：托管实例的 DSN 由程序计算（端口、密码每次安装都可能不同），
不该被用户 ``.env`` 里的开发库地址覆盖；反过来，托管路径也不该改写开发的约定。
"""
import os
import sys
from collections.abc import Callable
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from dotenv import load_dotenv  # noqa: F401  - 兼容既有的环境依赖，真实加载走 dsn 模块
from sqlalchemy import engine_from_config, pool

# 纯函数放在 `migrations/dsn.py`（可单测，不触发迁移）：这里只负责把它们串起来。
_MIGRATIONS_DIR = Path(__file__).resolve().parent
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

from dsn import (  # noqa: E402  - 必须先补 sys.path
    load_env_files,
    normalize_dsn,
    password_of,
    sanitize_text,
    target_line,
)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 加载 .env 文件 - 支持多个位置
project_root = Path(__file__).parent.parent

# 按优先级尝试加载 .env 文件。**不覆盖显式环境变量**：旧实现带 override，会把桌面壳
# 临时写入的 `DATABASE_DSN`（指向内嵌 PG 实例）覆盖成用户 .env 里的开发库地址 ——
# 迁移打到错误的库上，且进程内看起来"成功"。
env_candidates = [
    project_root / '.env',              # 根目录 .env
    project_root / 'config' / '.env',   # config 目录 .env
]
env_loaded = load_env_files(env_candidates)

if env_loaded is not None:
    print(f"[Alembic] Loaded .env from {env_loaded} (explicit environment variables win)")
else:
    print("[Alembic] Warning: .env file not found. Searched:")
    for p in env_candidates:
        print(f"  - {p}")


def get_database_url() -> tuple[str | None, str]:
    """Resolve database URL from DATABASE_DSN env → alembic.ini fallback.

    返回 ``(dsn, source)``，``source`` 只用于迁移前摘要（不参与连接）。
    """
    # 1) DATABASE_DSN environment variable (highest priority)
    db_dsn = os.getenv("DATABASE_DSN")
    if db_dsn:
        return db_dsn, "env:DATABASE_DSN"

    # 2) alembic.ini fallback
    fallback = config.get_main_option("sqlalchemy.url")
    if fallback:
        return fallback, "alembic.ini"

    return None, "none"


def migration_head() -> str:
    """当前 migration head（打印用；拿不到也不阻断迁移）。"""
    try:
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(config).get_current_head()
        return str(head or "(unknown)")
    except Exception as exc:  # noqa: BLE001 - 摘要失败不该拦住迁移
        return f"(unknown: {type(exc).__name__})"


# Set the database URL
raw_url, url_source = get_database_url()
db_url = normalize_dsn(raw_url) if raw_url else None
# 默认只打印脱敏目标 + head（PRD 9.7 H32）：DSN 本身绝不进日志，规范化差异也不回显
# 任何凭据形态。
print(target_line(db_url, migration_head(), url_source))
if db_url and raw_url and db_url != raw_url:
    print("[Alembic] DSN normalized (scheme fixed / password percent-encoded exactly once)")
# **不把 URL 写回 alembic 的 ini 配置**：`Config.set_main_option` 走 ConfigParser，
# 编码后的密码（如 `p%40ss`）会被当成插值语法直接抛
# `ValueError: invalid interpolation syntax`，迁移连一次都不跑。URL 改为直接传给
# `context.configure` / `engine_from_config`（见下），既不经过插值，也不再落进日志。


# 迁移为**手写 SQL**（各迁移文件的 op.execute）：这里不再注册 ORM metadata。
# 2026-10-16：`models.yaml → shared/models ORM → 守卫` 的生成链路已整体移除
# （快速迭代期直接改数据库；configs/orm/V1/models.yaml 仅作 CI/CD 与 API 参考）。
# autogenerate 因此不再可用（也不需要）—— upgrade/downgrade 只按
# migrations/versions/*.py 里写的 SQL 执行。
target_metadata = None

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = db_url or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    engine_kwargs: dict[str, Any] = {"poolclass": pool.NullPool}
    if db_url:
        # 显式 URL 覆盖 ini 里的 sqlalchemy.url：不经 ConfigParser 插值，密码里的
        # `%` 不会被当成语法错误。
        engine_kwargs["url"] = db_url
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        **engine_kwargs,
    )

    with connectable.connect() as connection:
        # 先保证 `alembic_version.version_num` 够宽（M30）：alembic 自建这张表时用默认
        # `varchar(32)`，而本仓的 revision id 有 **39 字符**
        # （`0017_conversation_shares_expires_backfill`）⇒ 跑到那一步会
        # `StringDataRightTruncation: value too long for type character varying(32)`，
        # 而且**整个事务回滚**（库里连 `alembic_version` 都不会留下，症状是"迁移静默失败"）。
        #
        # 关键：**表不存在时要先预建**（带 alembic 认的形状：NOT NULL + 同名主键约束，只是列更宽）。
        # 只写 `ALTER`（表存在才改）在**首次迁移**上是空转 —— alembic 随后自己建的还是
        # `varchar(32)`，于是照旧失败。这一条是幂等的：已有表就只加宽。
        # ⚠ 只有真实库能发现它：本仓的纪律是"dev 库不迁移"，从没在干净库上跑过全量迁移。
        connection.exec_driver_sql(
            "DO $$ BEGIN "
            "IF to_regclass('public.alembic_version') IS NULL THEN "
            "CREATE TABLE public.alembic_version ("
            "version_num varchar(255) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)); "
            "ELSE "
            "ALTER TABLE public.alembic_version ALTER COLUMN version_num TYPE varchar(255); "
            "END IF; END $$;"
        )
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # 比较列类型
            render_as_batch=True,  # 支持批量操作（SQLite需要）
            include_schemas=True,  # 包含所有schema
        )

        with context.begin_transaction():
            context.run_migrations()


def _run_guarded(target: Callable[[], None]) -> None:
    """执行迁移；失败时打印**脱敏后**的错误再原样抛出（失败必须响亮，凭据不许外泄）。"""
    try:
        target()
    except BaseException as exc:  # noqa: BLE001 - 只做脱敏，语义不变
        message = sanitize_text(
            f"{type(exc).__name__}: {exc}",
            db_url or "",
            raw_url or "",
            password_of(db_url or ""),
            password_of(raw_url or ""),
        )
        print(f"[Alembic] migration failed: {message}", file=sys.stderr)
        raise


if context.is_offline_mode():
    _run_guarded(run_migrations_offline)
else:
    _run_guarded(run_migrations_online)
