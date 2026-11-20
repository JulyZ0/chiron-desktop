"""分享运营：访问日志与保护字段。

## 为什么需要访问日志

`conversation_shares` 只有 `created_at` / `revoked_at` / `expires_at` —— 运营问
「这条链接到底有没有人看、是不是正在被转发」时没有任何依据。`share_access_log`
记**每一次成功打开**（不是每次请求：404/410/密码错误的请求不记，否则"攻击面有多大"
会伪装成"有多少人看过"）。

## 隐私：只存哈希与分类，不存原文

- `ip_hash`：sha256 加域分隔符后的十六进制摘要，**不落明文 IP**。目的是"同一个来访者
  算同一个"，够了；不做地理定位，因此不需要还原能力。
- `ua_class`：`bot` / `mobile` / `desktop` / `other` 四类，用来区分"爬虫刷量"与"真人"。
  完整 UA 保留在 web 服务器日志里，这里不重复存（那是一份可指纹化的长字符串）。
- `referer_host`：**只存主机名**，不存路径与查询串 —— 来源页的 query 里可能有一次性
  token / 会话标识，落库等于把它们复制到一个权限更松的表里。

## 没有外键

与 `notifications` / `pending_interactions` 同一惯例：日志是追加型事实，分享行被删
（当前没有删除路径，但将来可能有）不该被一条历史记录卡住。查询一律
`share_id = $1` 谓词兜住归属；无主行由 `share_access_log_retention`（运维按需）清理。

## 索引

唯一的查询形态是"某条分享最近 N 天的访问"与"总次数 / 最近一次"：
`(share_id, at DESC)` 同时覆盖两种，不必再加单列索引。

## 保护列

- `password_hash`：NULL = 无需密码。非 NULL 是 **bcrypt** 摘要（与登录口令同一算法，
  见 `internal/api/auth.go`），绝不存明文，也绝不存可逆加密。
- `max_views`：NULL = 不限次。正整数是"最多可被打开的次数"，达到后返回 410
  `share_view_limit`。计数来源就是上面那张日志表（不另设计数器列 —— 双写必然漂移）。

不要改 revision / down_revision。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_share_ops"
down_revision: str | Sequence[str] | None = "0004_deps_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE public.share_access_log (
    id bigserial NOT NULL,
    share_id text NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    ip_hash text,
    ua_class text,
    referer_host text
)"""
    )
    op.execute(
        """ALTER TABLE ONLY public.share_access_log
    ADD CONSTRAINT share_access_log_pkey PRIMARY KEY (id)"""
    )
    # 列表聚合（views_24h / views_total / last_viewed_at）与限额计数都走这个索引。
    op.execute(
        """CREATE INDEX ix_share_access_log_share_at
    ON public.share_access_log USING btree (share_id, at DESC)"""
    )
    # 保护字段：均为 NULL 安全（NULL = 未启用），存量行不需要回填。
    op.execute("ALTER TABLE public.conversation_shares ADD COLUMN password_hash text")
    op.execute("ALTER TABLE public.conversation_shares ADD COLUMN max_views integer")


def downgrade() -> None:
    op.execute("ALTER TABLE public.conversation_shares DROP COLUMN IF EXISTS max_views")
    op.execute("ALTER TABLE public.conversation_shares DROP COLUMN IF EXISTS password_hash")
    op.execute("DROP INDEX IF EXISTS public.ix_share_access_log_share_at")
    op.execute("DROP TABLE IF EXISTS public.share_access_log")
