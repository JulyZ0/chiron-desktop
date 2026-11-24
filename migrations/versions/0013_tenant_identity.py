"""Tenant-scoped idempotency key and case-insensitive unique email.

Revision ID: 0013_tenant_identity
Revises: 0012_session_fold_lineage
Create Date: 2026-11-24

H33：让「租户、幂等、邮箱唯一性」三者口径一致。

1) task_idempotency
   主键此前是 `idempotency_key` 单列 —— 一个租户的幂等键会挡住另一个租户的
   同名键（跨租户互相干扰，且幂等键本身通常来自客户端，等于给了一个跨租户的
   探测/阻塞面）。改为 `(tenant_id, idempotency_key)` 复合主键，并保留一个
   `idempotency_key` 上的普通索引：既按 key 单独查（运维/清理）仍有索引可用。

2) users.email
   登录自 C1 起用 `lower(email)` 匹配，而唯一索引建在**原始** email 上
   （`ux_users_email`，大小写敏感）⇒ `Bob@X.com` 与 `bob@x.com` 能同时存在，
   而登录只能命中其中一行：另一行永远登不进去。
   本迁移做三件事：
     a. 存量规范化：`email = lower(btrim(email))`；
     b. 冲突处理：规范化后仍重复的行，保留最早创建的一条（created_at 升序、
            并列按 id 决定），其余转存到 `identity_email_conflicts` 并把
            `users.email` 置 NULL（保留行/~密码/~归属，只是不再占用邮箱）。
            置 NULL 而非改名是有意的：任何改名都等于替用户换了一个登录名，
            而这里的事实是"这两行本来就是同一个邮箱"，只能由运维去看冲突表
            决定留哪条 —— 记录里保留了原邮箱，可人工恢复。
     c. 唯一索引与登录谓词对齐：`UNIQUE (lower(email)) WHERE email IS NOT NULL`。

幂等性：所有语句都可重复执行（IF EXISTS / IF NOT EXISTS / ON CONFLICT DO
NOTHING / 条件 UPDATE），重跑不会二次改数据。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_tenant_identity"
down_revision: str | Sequence[str] | None = "0012_session_fold_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── 1) 幂等键：主键含租户 ──
    op.execute("ALTER TABLE public.task_idempotency DROP CONSTRAINT IF EXISTS task_idempotency_pkey")
    op.execute(
        "ALTER TABLE public.task_idempotency "
        "ADD CONSTRAINT task_idempotency_pkey PRIMARY KEY (tenant_id, idempotency_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_idempotency_key "
        "ON public.task_idempotency (idempotency_key)"
    )

    # ── 2) 邮箱：规范化 + 冲突隔离 + lower(email) 唯一 ──
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.identity_email_conflicts (
            user_id character varying(36) NOT NULL,
            tenant_id smallint NOT NULL,
            email character varying(255),
            kept_user_id character varying(36),
            detected_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT identity_email_conflicts_pkey PRIMARY KEY (user_id)
        )
        """
    )

    op.execute(
        """
        UPDATE public.users
           SET email = lower(btrim(email)), updated_at = now()
         WHERE email IS NOT NULL
           AND email <> lower(btrim(email))
        """
    )

    # 冲突隔离：同一 lower(email) 只保留最早的一行，其余进冲突表并释放邮箱。
    op.execute(
        """
        INSERT INTO public.identity_email_conflicts (user_id, tenant_id, email, kept_user_id)
        SELECT id, tenant_id, email, kept_user_id
          FROM (
                SELECT u.id,
                       u.tenant_id,
                       u.email,
                       first_value(u.id) OVER w AS kept_user_id,
                       row_number() OVER w AS rn
                  FROM public.users u
                 WHERE u.email IS NOT NULL
                   AND btrim(u.email) <> ''
                WINDOW w AS (PARTITION BY lower(btrim(u.email))
                             ORDER BY u.created_at NULLS LAST, u.id)
               ) ranked
         WHERE rn > 1
        ON CONFLICT (user_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE public.users
           SET email = NULL, updated_at = now()
         WHERE id IN (SELECT user_id FROM public.identity_email_conflicts)
           AND email IS NOT NULL
        """
    )

    op.execute("DROP INDEX IF EXISTS public.ux_users_email")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower
            ON public.users (lower(email))
         WHERE email IS NOT NULL AND btrim(email) <> ''
        """
    )


def downgrade() -> None:
    # 恢复大小写敏感的全局唯一索引。若升级后已有大小写变体共存（升级期间新注册的），
    # 这一步会失败 —— 这正确：降级前必须人工清理冲突，不能静默丢约束。
    op.execute("DROP INDEX IF EXISTS public.ux_users_email_lower")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON public.users (email)")
    op.execute("DROP TABLE IF EXISTS public.identity_email_conflicts")

    op.execute("DROP INDEX IF EXISTS public.ix_task_idempotency_key")
    op.execute("ALTER TABLE public.task_idempotency DROP CONSTRAINT IF EXISTS task_idempotency_pkey")
    op.execute(
        "ALTER TABLE public.task_idempotency "
        "ADD CONSTRAINT task_idempotency_pkey PRIMARY KEY (idempotency_key)"
    )
