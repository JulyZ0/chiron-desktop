"""团队账号：状态、首登改密与登录时间。

## 为什么需要这些列

团队模式没有注册页，账号由后台创建、一次性交付初始密码。没有下面三列，
"禁用账号"与"首次登录必须改密"只能靠约定，落不到数据上：

- ``username``：可选的登录标识，与邮箱二选一使用；大小写不敏感唯一（按租户）。
- ``nickname``：展示名（欢迎语、个人信息页）。为空时读取侧回落到 ``name`` /
  ``username`` / ``email``，存量用户无需回填。
- ``status``：``active`` / ``disabled`` / ``deleted``。登录与 AuthMiddleware
  都按它拒绝请求；删除采用**软删除**（保留消息归属与审计），同时把 email 改写为
  ``deleted+<id>@invalid.local`` 以释放 ``(tenant_id, email)`` 唯一索引。
- ``must_change_password``：管理员创建 / 重置密码后置真，除改密、登出、profile
  之外的接口一律拒绝（由网关中间件判定）。
- ``password_changed_at``：改密与重置的事实时间，供审计与排障。
- ``last_login_at``：管理台展示"最近登录"，也是判断账号是否闲置的依据。

## 为什么没有 session_epoch 列

批量下线已经由 Redis 的 ``jwt:epoch:<userID>`` 实现（见
``internal/api/session_revoke.go``）：纪元是撤销时刻的 Unix 秒，中间件比对
``epoch > token.iat``。把它再落到数据库会造成两个真相源 —— 一个在 Redis、
一个在 PG，迟早漂移。这里不重复建列。

## 兼容性

全部列带默认值，存量行自动是 ``active`` / 不强制改密，行为与升级前一致。
``ADD COLUMN IF NOT EXISTS`` 让重复执行安全（与仓库其它迁移一致）。

不要改 revision / down_revision：链头由 ``internal/api/monitor_migration_test.go``
与 ``scripts/check_migration_head.py`` 钉住。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_team_accounts"
down_revision: str | Sequence[str] | None = "0007_metrics_rollup_instance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS username character varying(64)"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS nickname character varying(128)"
    )
    # 用户名大小写不敏感唯一；空串/NULL 不参与唯一约束（存量用户没有用户名）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_tenant_username "
        "ON public.users (tenant_id, lower(username)) "
        "WHERE username IS NOT NULL AND username <> ''"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS status character varying(16) "
        "NOT NULL DEFAULT 'active'"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS password_changed_at timestamp with time zone"
    )
    op.execute(
        "ALTER TABLE public.users "
        "ADD COLUMN IF NOT EXISTS last_login_at timestamp with time zone"
    )
    # 管理台按租户列成员/筛选状态；登录只查 (tenant_id, email) 唯一索引，不依赖它。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_tenant_status "
        "ON public.users USING btree (tenant_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_users_tenant_status")
    op.execute("DROP INDEX IF EXISTS public.ux_users_tenant_username")
    for column in (
        "last_login_at",
        "password_changed_at",
        "must_change_password",
        "status",
        "nickname",
        "username",
    ):
        op.execute(f"ALTER TABLE public.users DROP COLUMN IF EXISTS {column}")
