"""市场安装记录：回答「这条装过没有」，并把幂等落在唯一约束上。

**为什么需要这张表，而不是靠"按名字去 agents / plugins.json 里找"**

三类落点的名字**都是用户可改的**：agent 可以在工作台改名、MCP 插件可以在插件页改
名、知识库本来就是用户起的名字（还允许重名）。任何"装完之后按名字反查"的实现，
在用户改过一次名之后就会失效 —— 表现是市场页的「已安装」徽标消失、再点一次安装
又会装出第二份，用户看到的是"我明明装过了"。市场需要一个**不随名字漂移**的锚点。

锚点就是市场条目的主键本身：`(source, kind, market_id)`。目录里条目的身份是
`(Source, ID)`（见 `internal/market/catalog.go` 的 `Find`），同名条目在不同源是
不同东西 —— 安装记录因此必须带上 `source`，否则"我装的是哪个源的这条"无从追溯。
再加上 `(tenant_id, user_id)` 表达「谁装的」：安装记录是**用户级**事实，
同租户另一个用户装了同一条目与他无关（各自有自己的落点实例）。

**为什么唯一约束是幂等的落点**

重复安装必须返回"已安装"而不是报错，也不能装出第二份。把这条规则交给代码判断
会有一个绕不开的竞态：两个并发请求都查不到记录、都去落地、都去插入。约束放在
`(tenant_id, user_id, source, kind, market_id)` 上之后，第二次插入在 DB 层就被
拒绝，写入路径可以用 `ON CONFLICT DO UPDATE` 收敛成一次成功的 upsert ——
「幂等」于是成为一条**数据不变量**，而不是一段需要被小心维护的分支。

**为什么 `enabled` 默认 false**

外部源条目的内容不随二进制发布，装完默认**禁用**（不自启动、不自动执行），由用户
显式启用；内置源条目在写入时显式传 `enabled=true`。DDL 取 false 是因为"默认安全"
必须是**表级**的 —— 任何忘记赋值的写入路径都应该落到禁用，而不是落到可用。

**为什么 `ref` 是 json 而不是若干具体列**

落点引用的形状是 per-kind 的：agent 是 `agent_id`，mcp 是插件名，skill 是技能名，
knowledge 是 `kb_id`。拆成四个列会让表里永远有三列是 NULL，而且每加一类落点就要
改表；用 json 存一个 `map[string]any` 则把形状的决定权交给 `Installer` 实现
（`internal/market/install.go` 的 `InstalledRecord.Ref`）。它是**卸载时的唯一依据**：
外部源下线后目录里已经找不到该条目，卸载只能靠它。

**升级注意**：本文件把 head 从 `0005_checkpoint_seq_unique` 推到
`0006_market_installations`。`internal/db/schema_version.go` 的
`ParseMigrationHead` 会据目录重新解析 head，存量库因此会拒绝启动并提示
`alembic upgrade head`。桌面版启动器（`desktop-local/app/launcher.py`）会自动执行
升级，这是防 schema 漂移的预期行为，不是回归。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_market_installations"
down_revision: str | Sequence[str] | None = "0005_checkpoint_seq_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE = "ux_market_installations_owner_item"
_OWNER_INDEX = "ix_market_installations_owner"


def upgrade() -> None:
    # 列类型与既有表对齐：`tenant_id` 是 smallint（同 agents / project_checkpoints），
    # `user_id` 是 varchar(36)。对不上时 JOIN 会报 operator does not exist，
    # 而不是隐式转换 —— 这类错误只在真连库时才暴露。
    op.execute(
        """CREATE TABLE market_installations (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    source character varying(64) NOT NULL,
    kind character varying(16) NOT NULL,
    market_id character varying(255) NOT NULL,
    ref json NOT NULL DEFAULT '{}'::json,
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT market_installations_pkey PRIMARY KEY (id)
)"""
    )
    # 幂等的落点：同一用户对同一条目只可能有一行。用独立 UNIQUE INDEX 而不是
    # ADD CONSTRAINT —— `CREATE UNIQUE INDEX IF NOT EXISTS` 可重复执行，
    # 降级也只需 DROP INDEX，不必猜约束名。
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE} "
        "ON market_installations (tenant_id, user_id, source, kind, market_id)"
    )
    # 主读取路径是「我的已安装列表」（市场页徽标 + 安装列表页）：
    # 按 (tenant_id, user_id) 定位，再按 kind 可选过滤。
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_OWNER_INDEX} "
        "ON market_installations (tenant_id, user_id, kind)"
    )


def downgrade() -> None:
    # 降级**删除安装记录**是本表的固有代价：这些行只是「装过没有」的索引，
    # 真正的落点（agents 行 / plugins.json / 技能目录 / 知识库）都不在这里，
    # 删掉它们不会丢失已安装的东西本身。
    op.execute(f"DROP INDEX IF EXISTS {_OWNER_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_UNIQUE}")
    op.execute("DROP TABLE IF EXISTS market_installations")
