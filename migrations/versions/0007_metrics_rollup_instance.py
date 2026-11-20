"""分钟聚合的写入者身份（多副本可辨认）。

## 为什么需要这一列

`metrics_rollup_minute`（0003）只解决"分钟级持久化 + 单写者"。单写者保证**同一分钟**
只有一份数据，但**不同分钟可能由不同副本**抢到 advisory lock 写入 —— 于是窗口汇总
出来的数字是"多个副本拼起来的"，而面板上此前只能说"多副本合计"，说不出是**几个**
副本、有没有副本根本没参与。

`instance` 记录写入该行的网关实例身份（`CHIRON_INSTANCE_ID`，缺省
`<hostname>:<pid>`，见 `internal/api/monitor_rollup.go::rollupInstanceID`）。
查询侧对窗口内 distinct 计数，面板才能在 sources / note 里写明"N 个副本参与落库"。

## 默认值 '' 的含义

存量行没有身份，`NOT NULL DEFAULT ''` 让老数据不必回填也不会破主键：
查询侧把空串**排除**在副本计数之外，于是"0006 之前的行"如实呈现为
"副本数未知"，而不是被默认算成 1 个副本。

## 不建索引

唯一的读法是"窗口内所有行"，`ts_minute` 上的既有索引已经把范围收敛到分钟级行数，
再给 `instance` 建索引只会增加写放大而不改变任何查询计划。

不要改 revision / down_revision（0004/0005 与后续迁移依赖这条链）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_metrics_rollup_instance"
down_revision: str | Sequence[str] | None = "0006_price_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给分钟聚合加写入者身份列（存量行留空 = 未知）。"""
    op.execute(
        """ALTER TABLE public.metrics_rollup_minute
    ADD COLUMN IF NOT EXISTS instance text NOT NULL DEFAULT ''::text"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE public.metrics_rollup_minute DROP COLUMN IF EXISTS instance")
