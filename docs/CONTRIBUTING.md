
## 快速开始

```bash
python scripts/init.py           # 交互式初始化：生成 APP_SECRET、写 .env、自检依赖
python run.py setup              # 安装 Python / 前端依赖
python -m alembic upgrade head   # 建库到基线（需 PostgreSQL 可达）
python run.py start              # 网关 :8080 + 引擎 :8000 + MCP Broker :8001
# 打开前端注册第一个账号 —— 它自动成为系统管理员（owner）
# （仅源码态如此：桌面端没有注册概念，见下方「桌面端」）
```

> **本机需要先退出沙箱隔离**：引擎默认 `SANDBOX_BACKEND=service`（工具执行下沉到独立
> 受限容器），本机通常没有该容器，探测失败会**拒绝启动**（fail-closed）。
> 请在 `.env` 设 `SANDBOX_BACKEND=local`（或 `DEGRADED_MODE=true` 允许回退进程内执行）。

> 非交互场景：`python scripts/init.py --check`（只自检）、
> `python scripts/init.py --set POSTGRES_DSN=... --migrate`（写 .env 并执行迁移）。

## 关键配置

| 变量 | 说明 |
|---|---|
| `APP_SECRET` | **必须**：部署级主密钥（≥32 字符）。派生 `JWT_SECRET`/`INTERNAL_TOKEN`，并加密后台敏感配置 |
| `POSTGRES_DSN`、`REDIS_ADDR`（引擎用 `REDIS_URL`） | 基础设施连接串 |
| `SANDBOX_BACKEND` | `service`（默认，下沉独立容器）或 `local`（进程内，仅本机开发） |
| `DEGRADED_MODE` | 默认 `false`：Redis 不可用时网关与引擎**拒绝启动**；仅本机开发设 `true` |
| `TURN_RETENTION_DAYS` / `TASK_IDEMPOTENCY_RETENTION_DAYS` | 保留策略天数（默认 30），防长期运行表膨胀 |
| `ALLOW_SCHEMA_DRIFT` | 默认 `false`：启动时校验数据库迁移版本，不一致**拒绝启动** |

## 常用命令

```bash
go build -mod=mod ./...                   # Go 编译（-mod=mod：排除 vendor/ 参考源码目录）
go test -mod=mod ./...                    # Go 测试
python -m pytest python-engine/tests -q   # 引擎测试
python -m pip install -r requirements-migrate.txt   # 迁移依赖（仅迁移需要）
python -m alembic upgrade head            # 迁移到最新
python -m alembic heads                   # 迁移链检查（应只有一个 head）
make build                                # 见 Makefile（fmt/lint/test/build）
make check                                # 提交前快检：Go build/vet + 文档守卫
make check-all                            # 全量：再加 Python(pytest/ruff/mypy) 与前端(build/test)
```

> **`go` 命令请保留 `-mod=mod`**：仓库的 `vendor/` 下放的是参考源码（不含 `vendor/modules.txt`），
> 而 Go 只要看到 `vendor/` 就按 vendor 模式做一致性检查并报 `inconsistent vendoring`。
> `run.py`、`Makefile`、`Dockerfile` 已在编译入口显式排除该目录。

## 目录结构

```
cmd/               网关与 CLI（chiron 网关；chiron-cli 管理本地启动的进程）
internal/          Go 网关实现（api、auth、bloom、broadcast、db、engine、session、storage…）
python-engine/     引擎实现（app/agent、queue、api、tools、knowledge、memory…）
desktop-local/     数据栈引导器（embedded PG + bundled Redis）与本机组件管理（`python -m app.launcher`）
frontend-vue/      Vue 3 前端（Vite + Electron）
migrations/        Alembic 迁移（单一权威基线 + 手写 SQL）
market/skills/     内置技能资产（`{name}/SKILL.md` 目录型技能），运行时经内置源读取
shared/            跨端契约事实源（如 workstations.json，由三端契约测试比对）
pageindex-service/ 文档索引服务（知识库页面级检索）
sandbox-service/   工具执行沙箱服务
```

## 文档

| 想知道 | 去哪 |
|---|---|
| 本地模式的完整契约（运行模式、preload 暴露面、本地身份、数据隔离、组件、推理代理、用量） | [本地模式契约](docs/desktop-local/contracts.md) |

## 数据库迁移

**应用不自行迁移**（网关启动只做只读的 schema 版本校验）。迁移是发布流程的一次性步骤：

```bash
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://user:pwd@host:5432/db' \
  python -m alembic -c alembic.ini upgrade head        # 在线迁移
python -m alembic -c alembic.ini upgrade head --sql > upgrade.sql   # 离线：生成 DDL 供审阅
```

- **单一权威基线**：`migrations/versions/` 只有一个基线，全新库 `alembic upgrade head` 即为终态；
- **应用侧不写 DDL**：Go 的 `EnsureTables` 与 Python 的建表逻辑改为**只读校验**，缺失时明确报错并提示跑迁移；
- **升级既有库**：表已齐全的环境执行 `alembic stamp head`（只改版本号、不重放 DDL）；
- 迁移链必须**单一 head**（分叉会导致启动校验失败）。


