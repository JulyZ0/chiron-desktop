"""站内通知（通知中心）。

## 为什么现在才加

`frontend-vue/src/locales/*/notifications.ts` 与账号菜单里的「通知中心」入口
是从 SaaS 分支裁剪时**残留**下来的：文案引用 `internal/notify` 与迁移 `0011`，
但本仓从来就没有通知表、没有路由、没有写入方。于是那个入口是一条约定的 404。

补齐它的正确顺序是**先有事件、再有表**：本迁移落表，网关在
`internal/api/notifications.go` 里给出读写，写入方接的是**当前真实存在**的
事件（子 Agent 运行失败、分享被管理员撤销）—— 而不是照抄 SaaS 的
支付/套餐/配额（那些在本仓没有任何产生者，接了也只会永远空白）。

## 只存 kind + payload，不存渲染结果

与前端 `notifications.ts` 的既有取舍一致：站内信按**当前界面语言**实时渲染，
所以历史通知也会跟着语言切换。服务端存的是 `kind` + `payload`（JSON），
文案由 `locales/{zh-CN,en-US,ar}/notifications.ts` 提供。

代价是：新增 kind 必须同时加三份文案，否则界面会显示原始 key。这是一条
显式的取舍 —— 相比"存死一段当时语言的文本"，它换来了三语一致与可改文案。

## `read_at` 而不是 `is_read`

与 `conversation_shares.revoked_at` 同一惯例：可空时间戳既表达"是否已读"，
也表达"什么时候读的"（将来做"仅看未读"的清理或统计时不必再加列）。
null = 未读。

## 没有外键

`user_id` / `tenant_id` 与 `pending_interactions` 一样不带 FK：通知是**追加型
事件日志**，用户被删时历史通知保留原样（删人的路径不该被一条日志卡住），
查询侧一律 `user_id = $1` 谓词兜住归属。

## `dedupe_key` 为什么必须存在

写通知的入口是 **Redis pub/sub 中继**（子 Agent 失败事件），而每个网关副本都订阅
同一条通道 ⇒ N 个副本会各自"看到同一次失败"。没有去重键就是 N 条一模一样的通知。
幂等落点是 `ux_notifications_dedupe`（部分唯一索引，NULL 允许重复 —— 同一个
`share_revoked` 若将来允许重复产生，留空即可不去重）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_inapp_notifications"
down_revision: str | Sequence[str] | None = "0001_authoritative_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE public.notifications (
    id character varying(36) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36) NOT NULL,
    kind character varying(32) NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    session_id character varying(128),
    dedupe_key character varying(128),
    read_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
)"""
    )
    # 幂等键：多副本同时收到同一条 Redis 事件时，只有一个副本能落下这条通知。
    op.execute(
        """CREATE UNIQUE INDEX ux_notifications_dedupe
    ON public.notifications USING btree (dedupe_key)
    WHERE (dedupe_key IS NOT NULL)"""
    )
    op.execute(
        """ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id)"""
    )
    # 列表查询的唯一形态是"我的、按时间倒序"：这个复合索引直接覆盖它，
    # 不必再建单列 user_id 索引（前缀已可用）。
    op.execute(
        """CREATE INDEX notifications_user_created_idx
    ON public.notifications USING btree (user_id, created_at DESC)"""
    )
    # 未读计数与"仅看未读"只关心一小部分行：部分索引让它们不随历史通知增长而变慢。
    op.execute(
        """CREATE INDEX notifications_user_unread_idx
    ON public.notifications USING btree (user_id)
    WHERE (read_at IS NULL)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.notifications CASCADE")
