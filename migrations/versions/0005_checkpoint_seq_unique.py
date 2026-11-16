"""检查点 `(session_id, seq, kind)` 唯一约束：把「序号不可重复」落到 DB 上。

**为什么必须有这条约束，而不是只靠引擎侧的序号计算**

`checkpoint_hooks.py` 的实现注释一直写着「这正是 `project_checkpoints` 需要
`(session_id, seq)` 唯一约束来兜底的原因」—— 但 0004 只建了普通索引，这条兜底
**从未存在**。后果是实测过的：一旦引擎侧算出重复序号（曾因 `git gc` 打包 ref
导致序号回退到 1），`write_ref` 的 CAS 会失败 ⇒ 建点返回 `None` ⇒ 端口静默不报，
而若网关侧真收到重复 `(session_id, seq, kind)`，`ON CONFLICT (id) DO NOTHING`
也只会**静默吞掉**，连"这个点没建成"都留不下来。

约束落地之后，重复会被数据库如实拒绝，网关可以据此把失败写成可见状态
（`state='failed'`）而不是当作"重复上报"处理。

**为什么带上 `kind`**

同一回合会先后建 `pre_turn` 与 `post_turn` 两个点，它们**共用同一个 seq**
（seq 是"这一轮的序号"，不是"这一行记录的序号"）。只约束 `(session_id, seq)`
会把正常的一对前后快照判成冲突。

**为什么用独立 UNIQUE INDEX 而不是 ADD CONSTRAINT**

`CREATE UNIQUE INDEX IF NOT EXISTS` 可重复执行（迁移重跑、人工补执行都安全），
而降级也只需 `DROP INDEX`，不必猜约束名。语义上与唯一约束等价，且不会在
`pg_constraint` 里留下需要特殊处理的行。

**存量数据安全性**

`id = sha256(session_id|seq|kind)[:32]`（`internal/api/project_checkpoints.go`
的 `checkpointID`）—— 主键本身就是这三列的函数，因此**不可能**存在两行同
`(session_id, seq, kind)` 而不同 `id` 的存量数据。加索引不会因历史数据失败。

`session_id` 允许为空（画布会话不上报检查点）。Postgres 的唯一索引把 NULL 视为
互不相等，所以历史里的空 session 行不会互相冲突 —— 这是期望行为，不是漏洞：
没有会话就没有"会话内序号"这个概念。

**升级注意**：本文件把 head 从 `0004_code_agent_checkpoints` 推到
`0005_checkpoint_seq_unique`。`internal/db/schema_version.go` 的
`ParseMigrationHead` 会据目录重新解析 head，存量库因此会拒绝启动并提示
`alembic upgrade head`。桌面版启动器（`desktop-local/app/launcher.py`）会自动执行
升级，这是防 schema 漂移的预期行为，不是回归。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_checkpoint_seq_unique"
down_revision: str | Sequence[str] | None = "0004_code_agent_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ux_project_checkpoints_session_seq_kind"


def upgrade() -> None:
    # 先清掉可能已存在的重复（历史缺陷期间写进去的），否则建唯一索引会失败。
    #
    # 保留哪一条：按 `created_at` 最晚的那条 —— 它对应"最近一次真正建成的快照"。
    # 早的那条即使留着也无法回滚（同 seq 的 ref 只能指向一个 commit），删掉不损失能力。
    op.execute(
        """
        DELETE FROM project_checkpoints a
         USING project_checkpoints b
         WHERE a.session_id IS NOT NULL
           AND b.session_id IS NOT NULL
           AND a.session_id = b.session_id
           AND a.seq = b.seq
           AND a.kind = b.kind
           AND a.id <> b.id
           AND (a.created_at, a.id) < (b.created_at, b.id)
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} "
        "ON project_checkpoints (session_id, seq, kind)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
