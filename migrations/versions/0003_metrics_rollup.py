"""运行时监控的分钟级持久化聚合。

## 为什么需要这张表

网关的「运行时监控」页此前只有**进程内**的 60 分钟环缓冲采样
（`internal/monitor/window.go`）。它有两条硬边界：

1. **单副本视角**：多副本部署时，每个副本只看得见自己处理的请求，
   面板上的「近 15 分钟请求量」其实是「本副本的 15 分钟」。
2. **重启即清零**：进程一重启，趋势图归零，而排障往往正是从重启前后开始。

本迁移把**已走完的分钟**落成 PG 聚合，于是：

- overview 的 performance 聚合 = 「窗口内 rollup 汇总（多副本合计）+
  本进程实时尾巴」；
- 后台可以看到「最近 24h 历史趋势」，跨越重启；
- 写入是**单写者**（PG advisory lock，见 `internal/api/monitor_rollup.go`），
  多副本不会重复写。

## 主键为什么是 (ts_minute, route_class)

需求描述里的「`ts_minute` PK」在本仓落成**复合主键**：同一分钟要能同时存在
多个 `route_class`（/v1/chat、/v1/admin、/healthz 的尾延迟完全不同，混成一个
数字就解释不了）。复合主键同时保证 upsert 幂等：
`ON CONFLICT (ts_minute, route_class) DO UPDATE`。

`route_class` 是**低基数**分类（路径首段），不是完整路径 —— 否则每个 URL 一行，
聚合表本身变成逐请求日志。

## `count` / `errors` / `p95_ms` 的口径

- `count`：该分钟该分类的**全部**已完成请求数（含 101/304）；
- `errors`：其中 5xx 数（与面板「错误率 = 5xx / 全部样本」一致）；
- `p95_ms`：该分钟该分类的 P95，只统计可计耗时的响应（与窗口采样同口径）。
  跨分钟/跨分类**不可加**，查询侧取 MAX 并标注为近似值。

## 索引

主键 `(ts_minute, route_class)` 已能服务 `ts_minute >= $1 AND ts_minute < $2`
的范围扫（前导列）。额外给 `ts_minute DESC` 一个索引，让「最近 N 小时、按时间倒序」
的查询不必回表排序 —— 这是历史趋势端点的固定形状。

不要改 revision / down_revision：后续迁移依赖这条链。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_metrics_rollup"
down_revision: str | Sequence[str] | None = "0002_inapp_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建 metrics_rollup_minute 表（分钟聚合，多副本可汇总）。"""
    op.execute(
        """
        CREATE TABLE public.metrics_rollup_minute (
            ts_minute timestamp with time zone NOT NULL,
            route_class text NOT NULL DEFAULT ''::text,
            count bigint NOT NULL DEFAULT 0,
            errors bigint NOT NULL DEFAULT 0,
            p95_ms double precision,
            CONSTRAINT metrics_rollup_minute_pkey PRIMARY KEY (ts_minute, route_class)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_metrics_rollup_minute_ts
            ON public.metrics_rollup_minute USING btree (ts_minute DESC)
        """
    )


def downgrade() -> None:
    """删表（索引随之删除）。"""
    op.execute("DROP TABLE IF EXISTS public.metrics_rollup_minute")
