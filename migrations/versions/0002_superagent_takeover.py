"""Super-agent 接管：配置表与用户级总设置。

**为什么是两张表而不是一张**

接管配置是「一个会话一行」（会话维度：开关、节奏、预算、熔断状态），而总开关 /
上限 / 让步窗口是「一个用户一行」（用户维度）。合表会让后者在每一行上重复，改一次
要 UPDATE N 行，且并发改两行会得到互相矛盾的值。拆开之后各自的唯一键就是它们的
真实粒度。

**为什么预算按天重置而不是滑动窗口**

滑动窗口要保存每次消耗的时间戳（或一串桶），读放大且需要清理；按天重置只需一个
`budget_day` 列：读到时若是昨天就归零。用户对「今天用了多少」的理解也天然是按天的。

**改 schema 的方式：不要修改 0001，追加新的 revision。**
单一 head 是硬要求，分叉会被 `internal/db/schema_version.go` 直接判为错误。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_superagent_takeover"
down_revision: Union[str, Sequence[str], None] = "0001_authoritative_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 列类型必须与既有表对齐：`tenant_id` 是 smallint（同 agents）、`user_id` 是
# varchar(36)（同 agents）。对不上时 JOIN 会报 operator does not exist，而不是隐式转换。
_DDL: tuple[str, ...] = (
    """CREATE TABLE public.superagent_takeovers (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    session_id character varying(64) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    schedule_kind character varying(16) DEFAULT 'interval'::character varying NOT NULL,
    once_at timestamp without time zone,
    interval_seconds integer DEFAULT 1800,
    tools_mode character varying(16) DEFAULT 'auto'::character varying NOT NULL,
    daily_token_budget integer DEFAULT 120000 NOT NULL,
    tokens_used_today integer DEFAULT 0 NOT NULL,
    budget_day date,
    last_run_at timestamp without time zone,
    last_verdict character varying(24),
    last_reason text,
    melt_state character varying(16) DEFAULT ''::character varying NOT NULL,
    melt_reason text,
    cooldown_until timestamp without time zone,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
)""",
    """CREATE TABLE public.superagent_settings (
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    max_sessions integer DEFAULT 5 NOT NULL,
    day_token_budget integer DEFAULT 600000 NOT NULL,
    idle_minutes integer DEFAULT 10 NOT NULL,
    updated_at timestamp without time zone DEFAULT now()
)""",
    """ALTER TABLE ONLY public.superagent_takeovers
    ADD CONSTRAINT superagent_takeovers_pkey PRIMARY KEY (id)""",
    """ALTER TABLE ONLY public.superagent_settings
    ADD CONSTRAINT superagent_settings_pkey PRIMARY KEY (tenant_id, user_id)""",
    # 同一用户对同一会话只能有一条接管配置：重复行会让"这一拍该不该跑"没有唯一答案，
    # 也让 UI 上的开关与后端状态对不上。
    """ALTER TABLE ONLY public.superagent_takeovers
    ADD CONSTRAINT superagent_takeovers_tenant_id_user_id_session_id_key
    UNIQUE (tenant_id, user_id, session_id)""",
    # 巡检按「该用户的启用行」拉取，这个索引就是那条查询的访问路径。
    """CREATE INDEX idx_superagent_takeovers_user
    ON public.superagent_takeovers USING btree (tenant_id, user_id, enabled)""",
)

_TABLES: tuple[str, ...] = (
    "superagent_settings",
    "superagent_takeovers",
)


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    """只删本次新增的两张表。索引与约束随表 CASCADE，不必单独写。"""
    for name in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS public.{name} CASCADE")
