"""依赖快照入库（多副本一致）。

## 为什么快照必须进 PG

快照此前是**纯文件**：``<data_dir>/deps-snapshots/<id>.json``。单进程单机时这没问题，
但一旦引擎多副本，问题立刻出现 —— ``/list`` 落在副本 A 上能看见的快照，落在副本 B
上就是"还没拍过"；而 ``restore`` 又必须在**装包的那个副本**上找文件。用户看到的是
一份随机出现/消失的历史，回滚功能等于不存在。

所以权威存储换成 PG：任一副本写入，所有副本都能列出、都能读。

## 失败就回退文件，而不是让整页不可用

依赖管理是"环境坏了时的最后手段"，不能因为 PG 抖动/迁移未跑就整页 502。因此引擎的
读写顺序是 **PG 优先 → 失败回退本地文件**，并在响应里用 ``storage: "pg" | "file"``
明示这一次到底用了哪个后端（前端据此提示"当前快照只在本机可见"）。
文件侧保留原有形态，回退时行为与迁移前逐字一致。

## 列的选择

* ``id`` 是 **text 主键**而不是 uuid：id 由引擎按 ``<UTC 时间戳毫秒>Z-<6 位随机>`` 生成，
  字典序即时间序。沿用同一形态，迁移期间"文件里的旧 id"与"PG 里的新 id"互不冲突，
  按 id 直接读文件与读 PG 是同一套校验（见 ``SNAPSHOT_ID_RE``）。
* ``tenant_id`` 是 ``smallint``：与 0001 基线所有业务表同口径（``tenants.id`` 是 smallint）。
  保留份数是**按租户**计的 —— 一个租户的清理不该删掉另一个租户的历史。
* ``user_id`` 可空且**不带外键**：与 ``notifications`` 同惯例，历史快照在用户被删后
  仍有排查价值，删人的路径不该被一条运维记录卡住。
* ``packages`` 是 ``jsonb`` 数组（``[{"name","version"}, …]``）：恢复时按条目逐条重新
  过规格白名单，因此它是**数据**而不是可执行参数；jsonb 让"包数/包名"可在 SQL 侧
  直接数（``jsonb_array_length``），列表页不必把几百条包名读出来再数。
* ``python`` 记录拍快照时的解释器版本：跨副本/跨升级后，"这份快照是哪套环境拍的"
  是恢复前唯一能判断兼容性的线索。

## 索引

* ``(created_at DESC)``：列表与"最近 N 份"的唯一排序形态；
* ``(tenant_id, created_at DESC)``：按租户列表 + 按租户裁剪（``DELETE … NOT IN
  (SELECT … ORDER BY created_at DESC LIMIT 20)``）直接走这条，不必回表排序。

不要改 revision / down_revision：后续迁移依赖这条链。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_deps_snapshots"
down_revision: str | Sequence[str] | None = "0003_metrics_rollup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """deps_snapshots 表 + 时间倒序索引（租户维度裁剪）。"""
    op.execute(
        """CREATE TABLE public.deps_snapshots (
    id text NOT NULL,
    tenant_id smallint DEFAULT 1 NOT NULL,
    user_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    trigger text NOT NULL,
    label text,
    python text,
    packages jsonb DEFAULT '[]'::jsonb NOT NULL
)"""
    )
    op.execute(
        """ALTER TABLE ONLY public.deps_snapshots
    ADD CONSTRAINT deps_snapshots_pkey PRIMARY KEY (id)"""
    )
    # 列表与保留策略的唯一排序形态：最新在前。
    op.execute(
        """CREATE INDEX deps_snapshots_created_at_idx
    ON public.deps_snapshots USING btree (created_at DESC)"""
    )
    # 按租户列表 + 按租户裁剪：前缀 tenant_id 让"只看自己租户的最近 20 份"不回表排序。
    op.execute(
        """CREATE INDEX deps_snapshots_tenant_created_idx
    ON public.deps_snapshots USING btree (tenant_id, created_at DESC)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.deps_snapshots CASCADE")
