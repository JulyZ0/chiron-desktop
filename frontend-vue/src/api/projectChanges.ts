/**
 * 项目改动文件的数据面（只读 Git 视图）。
 *
 * **为什么由后端跑 git**：渲染层既拿不到项目根，也不该执行外部进程 —— 详见
 * `internal/api/project_git.go` 文件头。
 *
 * **为什么没有暂存 / 丢弃 / 提交**：这三个都会写用户的仓库，其中"丢弃"还会真的
 * 回退工作区。写操作需要撤销设计与二次确认，不在只读视图的范围内。
 */
import { api } from './index'

/** 单字母状态：M 改 / A 新增 / D 删除 / R 重命名 / C 复制 / ? 未跟踪 / U 冲突 */
export type ProjectChangeStatus = 'M' | 'A' | 'D' | 'R' | 'C' | '?' | 'U' | string

export interface ProjectChange {
  path: string
  /** 仅重命名/复制时非空（porcelain 的 R/C 条目带原路径） */
  old_path?: string
  status: ProjectChangeStatus
  /** 索引里有这个文件的改动 */
  staged: boolean
  /** 工作区与索引不同 */
  unstaged: boolean
  /**
   * 行数统计。**缺省 ≠ 0**：二进制文件、重命名简写、未跟踪文件都没有统计，
   * 界面必须据此"什么都不打印"，而不是显示 +0 -0。
   */
  insertions?: number
  deletions?: number
}

export interface ProjectChanges {
  /** false = 这里没有版本控制（正常状态，不是错误） */
  repo: boolean
  /** true = 机器上没有装 git。与"没有改动"分开提示 */
  git_missing?: boolean
  branch?: string
  head?: string
  changes: ProjectChange[]
  /** 条目触到后端上限被截断。界面必须说出来 */
  truncated: boolean
}

function normalizeChange(raw: unknown): ProjectChange | null {
  const c = (raw ?? {}) as Partial<ProjectChange>
  if (typeof c.path !== 'string' || c.path === '') return null
  return {
    path: c.path,
    old_path: typeof c.old_path === 'string' && c.old_path ? c.old_path : undefined,
    status: typeof c.status === 'string' && c.status ? c.status : 'M',
    staged: !!c.staged,
    unstaged: !!c.unstaged,
    insertions: typeof c.insertions === 'number' ? c.insertions : undefined,
    deletions: typeof c.deletions === 'number' ? c.deletions : undefined,
  }
}

/** 读取项目内的改动文件。没有版本控制时返回 `repo: false`，不抛错。 */
export async function listProjectChanges(projectId: string): Promise<ProjectChanges> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/changes`,
  )
  const payload = data?.data ?? {}
  const list: unknown[] = Array.isArray(payload.changes) ? payload.changes : []
  return {
    repo: !!payload.repo,
    git_missing: !!payload.git_missing,
    branch: typeof payload.branch === 'string' ? payload.branch : undefined,
    head: typeof payload.head === 'string' ? payload.head : undefined,
    changes: list.map(normalizeChange).filter((c): c is ProjectChange => c !== null),
    truncated: !!payload.truncated,
  }
}
