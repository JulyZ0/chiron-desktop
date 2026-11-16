"""会话地图改造：Workspace 即项目。

**语义**（见 `docs/project-management-design.md`）

`session_map_workspaces` 一个 workspace 要么是**项目**（`root_path` 非空，绑定一个本地
文件夹），要么是**画布**（`root_path` 为空，旧形态）。不设第三种形态。

**归属关系复用 `session_map_nodes`，靠 `edge_kind='project'` 标记**

不新建 `project_sessions` 表：`session_map_nodes` 已有 `workspace_id`/`session_id` 与
`ON DELETE CASCADE` 到 `sessions`（`0001_authoritative_baseline.py:792`），血缘与分叉
逻辑也都在。代价是这张表同时承载两种用途，因此**画布的读与删必须一律带
`n.edge_kind <> 'project'`** —— 漏一处就会把归属记录当成节点画出来，或者把归属删光。

**为什么这两条唯一索引是必需的，而不是靠应用层检查**

`session_map_nodes_session_idx` 是普通索引（`0001:741`），同一会话可以出现在任意多个
节点里。若不约束，同一个会话会被绑到多个项目，「移动会话到另一个项目」就退化成
「多归属」，侧边栏必然重复显示同一会话。索引把不变式 I1/I2 落到 DB 上：

  I1  UNIQUE(session_id) WHERE edge_kind='project'                一个会话至多属于一个项目
  I2  UNIQUE(workspace_id, session_id) WHERE edge_kind='project'  项目内不重复

**升级注意**：`internal/db/schema_version.go` 的 `ParseMigrationHead` 按目录解析 head，
加本文件后 head 由 `0002_superagent_takeover` 变为 `0003_project_binding`，存量库会
拒绝启动并提示 `alembic upgrade head`。这是防 schema 漂移的预期行为，不是回归。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003_project_binding"
down_revision: Union[str, Sequence[str], None] = "0002_superagent_takeover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # root_path：项目绑定的本地文件夹。空串 = 画布，存量数据因此行为不变。
    # varchar(1024) 而非 text：Windows 长路径上限 32767，但项目路径实际不会更长，
    # 定长能让索引开销可预期；超长路径在 API 层直接拒绝（不靠 DB 截断）。
    op.execute(
        "ALTER TABLE session_map_workspaces ADD COLUMN IF NOT EXISTS "
        "root_path varchar(1024) NOT NULL DEFAULT ''"
    )
    # sort_index：侧边栏项目排序。同值时按 name 兜底，避免顺序抖动。
    op.execute(
        "ALTER TABLE session_map_workspaces ADD COLUMN IF NOT EXISTS "
        "sort_index integer NOT NULL DEFAULT 0"
    )

    # I1：一个会话至多属于一个项目。
    # 排除 session_id 为空的占位行与空串（便签没有会话）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_smn_project_session "
        "ON session_map_nodes (session_id) "
        "WHERE edge_kind = 'project' AND session_id IS NOT NULL AND session_id <> ''"
    )
    # I2：同一项目内一个会话只有一条归属记录。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_smn_project_workspace_session "
        "ON session_map_nodes (workspace_id, session_id) "
        "WHERE edge_kind = 'project' AND session_id IS NOT NULL"
    )
    # 侧边栏按「项目 → 会话」列表读取，走 (workspace_id, edge_kind) 过滤 + 时间排序。
    # 已有索引只覆盖 workspace_id，含 edge_kind 时能少扫画布节点（大画布可能上千行）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_smn_project_lookup "
        "ON session_map_nodes (workspace_id, edge_kind, created_at)"
    )


def downgrade() -> None:
    # 逆序删除：先索引后列。归属记录本身随索引消失而失去约束，但仍留在表里 ——
    # 降级不应删数据，用户回滚到旧版本时那些行会被当成画布节点显示（hidden=true
    # 之外还需注意），这比丢数据可接受。
    op.execute("DROP INDEX IF EXISTS ix_smn_project_lookup")
    op.execute("DROP INDEX IF EXISTS ux_smn_project_workspace_session")
    op.execute("DROP INDEX IF EXISTS ux_smn_project_session")
    op.execute("ALTER TABLE session_map_workspaces DROP COLUMN IF EXISTS sort_index")
    op.execute("ALTER TABLE session_map_workspaces DROP COLUMN IF EXISTS root_path")
