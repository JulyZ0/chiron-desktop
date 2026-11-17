/**
 * 项目目录的版本控制状态与启用（还原点的前提）。
 *
 * **为什么还原点需要它**：还原点建立在 git 对象库之上 —— 引擎把工作区写成 tree、
 * 包成 commit、ref 落在 `.git/refs/chiron/...`。项目目录不是 git 工作树时，快照
 * 第一步就报 `fatal: not a git repository`，于是每一轮都留下一个 `state='failed'`
 * 的点：列表里看得见，点回滚必然失败。界面上那句"还原点功能不可用"的根因就在这里。
 *
 * **为什么状态要分 `repo` / `own_root` 两个字段**：位于某个仓库的子目录同样能建
 * 检查点（`git -C <子目录>` 会找到祖先仓库），但**不能再初始化** —— 在子目录里
 * 再 `git init` 会造出嵌套仓库，把该目录从父仓库的工作树里抠出来。前端只看一个
 * 布尔值分不出这两种情况，所以判据由服务端给出。
 *
 * **`init` 是写操作**：会在用户项目根创建 `.git`。只由用户明确同意后调用，
 * 且服务端幂等（已在仓库内不执行任何写操作）。
 */
import { api } from './index'

export interface ProjectVCS {
  /** 项目根处于某个 git 工作树内 → 检查点可用 */
  repo: boolean
  /** 工作树根目录；`repo: false` 时为空 */
  repo_root?: string
  /** 仓库根就是项目根本身。`repo: true` 且此项为 false = 项目是更大仓库的子目录 */
  own_root: boolean
  /** 机器上没有 git：这种情况该让用户去装 git，「启用」按钮没有意义 */
  git_missing?: boolean
}

function normalize(raw: unknown): ProjectVCS {
  const v = (raw ?? {}) as Record<string, unknown>
  return {
    repo: !!v.repo,
    repo_root: typeof v.repo_root === 'string' && v.repo_root ? v.repo_root : undefined,
    own_root: !!v.own_root,
    git_missing: !!v.git_missing,
  }
}

/** 读项目根此刻的版本控制状态。读不出来按"没有版本控制"处理，不抛错。 */
export async function getProjectVcs(projectId: string): Promise<ProjectVCS> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/vcs`,
  )
  return normalize(data?.data)
}

/**
 * 在项目根执行 `git init` 并返回之后的状态。
 *
 * 服务端只创建 `.git`，不暂存、不提交 —— 用户同意的是"启用还原点"，
 * 不是"把整个项目提交一遍"。返回 `repo: false` 说明初始化没生效（权限等）。
 */
export async function initProjectVcs(projectId: string): Promise<ProjectVCS> {
  const { data } = await api.post(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/vcs/init`,
  )
  return normalize(data?.data)
}
