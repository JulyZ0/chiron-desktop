"""知识库「启用 / 停用」用独立布尔列 `enabled`，**不复用 `status`**。

## 为什么不给 `status` 加一个 'disabled' 取值

`knowledge_bases.status` 已经是**索引状态机**的载体，取值由
``pageindex-service/app/index_worker.py`` 写入：``indexing``（开始）→ ``ready`` /
``error``（结束），建库时落 ``disabled`` 表示「还没索引过」。也就是说它表达的是
**"这个库的索引现在是什么成色"**，而且它的迁移方向是**执行者驱动**的：worker 拿到
任务就写 ``indexing``，跑完写 ``ready``/``error``，不经过用户。

"用户是否启用这个库"是**用户意图**，与索引成色互相独立：一个 ``ready`` 的库可以被
停用（保留索引、不再参与检索），一个 ``disabled``（尚未索引）的库也可以被启用后
立刻构建。两者要能同时成立 —— 塞进同一列就必然出现"停用把索引状态抹掉"或
"索引完成把停用覆盖成启用"的二选一，而且任一方写入都会静默改写另一方的语义
（worker 每轮索引都会 ``UPDATE ... SET status = ...``，用户停用会被下一次索引冲掉）。

独立布尔列之后两者正交：``status`` 只由索引流程写，``enabled`` 只由用户的开关写，
``_serialize_kb`` 同时暴露，检索/索引两条路径各自读自己关心的那一个。

## 默认值

``NOT NULL DEFAULT true``：存量库都是用户建好、本来就能检索的，迁过来必须继续可用；
新库建出来就启用（用户不会先建一个"停用的库"再启用 —— 建完不索引只是 ``status``
停在 ``disabled``，与启用无关）。

索引只用 ``(enabled)`` 单列：检索路径按 ``id`` 主键取行后再判，不需要全表扫；
这里建的是部分索引（只索引被停用的行），命中的是"停用库"这个少数集合。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_kb_enabled"
down_revision: str | Sequence[str] | None = "0005_checkpoint_seq_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_knowledge_bases_disabled"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_bases "
        "ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true"
    )
    # 部分索引：正常情况下停用是少数，索引因此很小；检索侧不需要它（按主键取行），
    # 但"列出被停用的库"这类运维查询会用到。
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} "
        "ON knowledge_bases (tenant_id, user_id) WHERE enabled = false"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS enabled")
