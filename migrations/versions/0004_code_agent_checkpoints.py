"""代码 Agent 检查点：时间线主表与未跟踪文件清单。

**为什么是两张表**

`project_checkpoints` 是「一个检查点一行」：它记录的是 git 层的提交对象（commit/tree）、
归属（项目/会话/回合）与状态。`checkpoint_untracked` 是它指向的**自有 CAS** 清单，
一个检查点对应 N 行。合并成一张表就得在主行上塞数组或 JSON，无法按 sha256 建索引，
也就无法回答「哪些 blob 已经没有任何检查点引用」—— 而这是保留策略唯一的判据。

**为什么未跟踪文件不进用户 `.git/objects`**

见 `docs/code-agent-checkpoint-design.md` D4：agent 新建文件的数量不可控，塞进用户对象库
会污染仓库体积、拖慢用户的 `git gc`。自有存储按 sha256 分片去重，同一文件在多个检查点
只存一份。

**`rollback_epoch` 不建字段**

D2 要求「init 之前的会话禁止回滚」。这里不引入新列，而是用 `kind='init_epoch'` 的那条
记录承担 epoch 起点：查询「最近一条 kind='init_epoch' 且 state='ok' 的记录」，任何检查点
只有 `created_at >= 它` 才可回滚。理由：`git init` 被重复执行、或项目根被改绑时，只需
追加一条新的 `init_epoch`，旧 epoch 自动失效 —— 若做成列，就要回头 UPDATE 全表。

**不加外键到 `session_map_workspaces`**

检查点是历史记录，项目被删除后它仍有价值（用户可能只是想解除绑定）。加
`ON DELETE CASCADE` 会在删项目时静默抹掉全部历史，这与「检查点必须能用于审计」冲突。
归属靠 `workspace_id` 索引查询即可。

**升级注意**：本文件把 head 从 `0003_project_binding` 推到 `0004_code_agent_checkpoints`。
`internal/db/schema_version.go` 的 `ParseMigrationHead` 会据目录重新解析 head，存量库
因此会拒绝启动并提示 `alembic upgrade head` —— 这是防 schema 漂移的预期行为。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_code_agent_checkpoints"
down_revision: str | Sequence[str] | None = "0003_project_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 列类型与既有表对齐：`tenant_id` 是 smallint（同 agents / superagent_takeovers），
# `user_id` 是 varchar(36)。对不上时 JOIN 报 operator does not exist，而非隐式转换。
_DDL: tuple[str, ...] = (
    """CREATE TABLE project_checkpoints (
    id character varying(64) NOT NULL,
    tenant_id smallint NOT NULL,
    user_id character varying(36),
    workspace_id character varying(64) NOT NULL,
    session_id character varying(36),
    turn_id character varying(36),
    seq bigint NOT NULL,
    commit_sha character varying(64) NOT NULL,
    tree_sha character varying(64) NOT NULL,
    parent_sha character varying(64),
    head_sha character varying(64),
    ref_name character varying(255) NOT NULL,
    kind character varying(16) NOT NULL,
    state character varying(16) DEFAULT 'ok'::character varying NOT NULL,
    error_code character varying(64),
    error_msg text,
    label character varying(255) DEFAULT ''::character varying NOT NULL,
    file_count integer DEFAULT 0 NOT NULL,
    skipped_count integer DEFAULT 0 NOT NULL,
    byte_size bigint DEFAULT 0 NOT NULL,
    branch_name character varying(255),
    applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_checkpoints_pkey PRIMARY KEY (id)
)""",
    # 时间线按会话倒序读：seq 是会话内单调递增的序号，回滚只能回更小的 seq。
    """CREATE INDEX ix_project_checkpoints_session
    ON project_checkpoints (session_id, seq DESC)""",
    # 项目视图按创建时间倒序；两个索引服务于两条不同的读取路径，不能合并。
    """CREATE INDEX ix_project_checkpoints_project
    ON project_checkpoints (workspace_id, created_at DESC)""",
    # 清单：一个检查点引用 N 个未跟踪文件（路径 → 内容地址）。
    #
    # **主键是 (checkpoint_id, path)** 而不是 id：同一检查点内一个路径只能有一个版本，
    # 用复合主键天然表达这一点，省掉一个自增列与自己维护的唯一约束。
    # `ON DELETE CASCADE`：删检查点时清单必须一起走，否则会留下永远无人引用的行。
    """CREATE TABLE checkpoint_untracked (
    checkpoint_id character varying(64) NOT NULL,
    path character varying(1024) NOT NULL,
    sha256 character varying(64) NOT NULL,
    size bigint DEFAULT 0 NOT NULL,
    CONSTRAINT checkpoint_untracked_pkey PRIMARY KEY (checkpoint_id, path),
    CONSTRAINT checkpoint_untracked_checkpoint_fk FOREIGN KEY (checkpoint_id)
        REFERENCES project_checkpoints (id) ON DELETE CASCADE
)""",
    # 保留策略的核心查询：给定 blob 的 sha256，判断是否还有任何检查点引用它。
    # 没有这个索引就只能全表扫，而清单行数随检查点数线性增长。
    """CREATE INDEX ix_checkpoint_untracked_sha
    ON checkpoint_untracked (sha256)""",
)


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)


def downgrade() -> None:
    # 先删子表再删主表：反过来的话外键会让 DROP TABLE 失败。
    # 降级**删除数据**是本表的固有代价 —— 检查点的权威副本在 git 对象库与自有 CAS
    # 目录里，删掉这些行只会丢失「时间线索引」，不会丢失快照本身。
    op.execute("DROP TABLE IF EXISTS checkpoint_untracked")
    op.execute("DROP TABLE IF EXISTS project_checkpoints")
