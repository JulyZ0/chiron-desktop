"""人格（persona）—— 把原「行为塑形」从用户设置搬到后台管理，并支持对话内选择。

Revision ID: 0009_personas
Revises: 0008_team_accounts
Create Date: 2026-11-22

## 为什么是租户级表，而不是复用 harness_profiles

`harness_profiles` 是「按模型前缀自动追加系统提示」的机制：它没有名字、没有展示面，
用户也无法在对话里挑选。人格需要**可命名、可展示、可一键切换**，并且要与会话的
实际行为对上 —— 用一张独立的表表达，语义最直白。

## 预设「猫娘」

预置人格随迁移写入每个已存在的租户；新租户由后台新增即可。`preset_key` 用来标识
内置来源（不影响用户编辑），并在 (tenant_id, preset_key) 上唯一，避免重复播种。

## 默认人格

`is_default` 每租户最多一条（部分唯一索引）。默认人格会在**用户未显式选择**时生效；
预置的猫娘**不是**默认 —— 否则升级后所有对话的语气会被悄悄改掉。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_personas"
down_revision: str | Sequence[str] | None = "0008_team_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CAT_GIRL_PROMPT = """你是一只温柔、活泼的猫娘助手。

- 说话带一点猫咪的俏皮感：句尾偶尔用「喵」，但不要每句都加，更不能影响信息密度。
- 用「我」自称；称呼用户为「主人」或对方给的昵称，语气亲切但不谄媚。
- 专业内容优先：技术结论、代码、操作步骤必须准确完整，卖萌不能替代内容。
- 被纠正时坦率承认并改正，不要用撒娇敷衍过去。
"""


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS public.personas (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    name character varying(100) NOT NULL,
    description text NOT NULL DEFAULT '',
    system_prompt text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    is_default boolean NOT NULL DEFAULT false,
    preset_key character varying(64),
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
)"""
    )
    op.execute(
        "ALTER TABLE ONLY public.personas ADD CONSTRAINT personas_pkey PRIMARY KEY (id)"
    )
    op.execute(
        "ALTER TABLE ONLY public.personas "
        "ADD CONSTRAINT personas_tenant_id_fkey FOREIGN KEY (tenant_id) "
        "REFERENCES public.tenants(id) ON DELETE CASCADE"
    )
    # 同租户重名会让人格选择器出现两条一模一样的条目 —— 直接禁掉（大小写不敏感）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_personas_tenant_name "
        "ON public.personas (tenant_id, lower(name))"
    )
    # 每租户最多一个默认人格：部分唯一索引比应用层判重可靠（并发保存也不会出双默认）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_personas_tenant_default "
        "ON public.personas (tenant_id) WHERE is_default"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_personas_tenant_preset "
        "ON public.personas (tenant_id, preset_key) WHERE preset_key IS NOT NULL"
    )

    # 预设「猫娘」：只给已存在的租户播种；重复执行安全。
    prompt = _CAT_GIRL_PROMPT.replace("'", "''")
    op.execute(
        f"""INSERT INTO public.personas
                (id, tenant_id, name, description, system_prompt, enabled, is_default, preset_key)
             SELECT gen_random_uuid()::text, t.id, '猫娘',
                    '温柔活泼、带一点猫咪语气的助手（可在对话中切换）',
                    '{prompt}',
                    true, false, 'catgirl'
               FROM public.tenants t
             ON CONFLICT DO NOTHING"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ux_personas_tenant_preset")
    op.execute("DROP INDEX IF EXISTS public.ux_personas_tenant_default")
    op.execute("DROP INDEX IF EXISTS public.ux_personas_tenant_name")
    op.execute("DROP TABLE IF EXISTS public.personas")
