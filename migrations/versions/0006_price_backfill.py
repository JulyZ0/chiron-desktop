"""价格数据可靠性：回填映射索引 + 价格源健康表。

## 为什么需要这张「价格源健康表」

用户实测的故障形态是：`model_prices` 长期为空、`deepseek-v4.1-flash` 无价，而日志里
只有引擎自己的一行 `model price fetch failed (pricetoken): ...`。网关侧的
`/v1/admin/billing/price-coverage` 只能说明"有模型没价"，**说不出为什么没价** ——
是 pricetoken 挂了、llmrates 改结构了，还是两个源报价不一致？

`price_source_status` 存**每个价格源最近一轮**的拉取事实（成功/失败/条数/耗时/脱敏
原因）。写方是 Python 引擎的同步循环（`python-engine/app/pricing/sync.py`），读方是
网关的 `GET /v1/admin/billing/price-sources`。两侧各说各话没有意义，所以落库共享。

口径取舍：

* ``source_key`` 是**拉取单元**而不是解析器名：`portkey` 按厂商分 4 个文件，
  用解析器名做键会让 4 次拉取互相覆盖（历史上 `sync_once` 踩过这个坑），
  因此键是 ``portkey:openai`` 这类形态。
* ``status`` 三态：``ok``（拉到并解析出 ≥1 条）、``empty``（成功但 0 条）、
  ``failed``（网络/HTTP/解析失败）。``empty`` 与 ``failed`` 必须分开，
  否则"源返回了空列表"会被误报成"源不可达"。
* ``error`` 由写方**脱敏后截到 200 字符**：异常文本里可能带查询串里的 token，
  也可能被防护层塞进整段响应体。这里是展示字段，不是排障全文。
* ``url_host`` 只存主机名，**不存完整 URL**：路径/查询串对判断健康没有价值，
  却可能含一次性凭证。
* 只保留每个源的**最近一次**结果（主键 upsert），不做历史表：这张表的用途是
  "现在哪个源坏了"，历史趋势由指标系统（`MODEL_PRICE_SYNC`）承担。

## 为什么同时建 llm_models(name) 索引

`POST /v1/admin/billing/backfill-provider` 只按 `llm_models.name` / `llm_models.id`
把空 provider 回填。`id` 是主键无需再加索引；`name` 在 0001 基线里**只有列没有索引**，
一旦模型目录上千行，回填会退化成整表扫描 × 每个待回填行。这里补一条普通 btree
（**不唯一**：同名模型在不同租户/网关下可能有多行，唯一索引会直接建失败）。

不要改 revision / down_revision：链头由 `internal/api/monitor_migration_test.go` 钉住。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_price_backfill"
down_revision: str | Sequence[str] | None = "0005_share_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建回填映射索引与价格源健康表。"""
    # 回填的映射键是 llm_models.name（id 有主键）。IF NOT EXISTS 让重复执行安全。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_llm_models_name "
        "ON public.llm_models USING btree (name)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.price_source_status (
            source_key  text NOT NULL,
            parser      text NOT NULL DEFAULT ''::text,
            provider    text NOT NULL DEFAULT ''::text,
            url_host    text NOT NULL DEFAULT ''::text,
            status      text NOT NULL DEFAULT 'unknown'::text,
            rows        integer NOT NULL DEFAULT 0,
            duration_ms integer NOT NULL DEFAULT 0,
            error       text NOT NULL DEFAULT ''::text,
            checked_at  timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT price_source_status_pkey PRIMARY KEY (source_key)
        )
        """
    )


def downgrade() -> None:
    """删表与索引（可重复执行用 IF EXISTS）。"""
    op.execute("DROP TABLE IF EXISTS public.price_source_status")
    op.execute("DROP INDEX IF EXISTS public.ix_llm_models_name")
