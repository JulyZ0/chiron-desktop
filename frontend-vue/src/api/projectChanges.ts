/**
 * 项目改动文件的数据面（只读 Git 视图 + N5 索引写操作）。
 *
 * **为什么由后端跑 git**：渲染层既拿不到项目根，也不该执行外部进程 —— 详见
 * `internal/api/project_git.go` 文件头。
 *
 * **写操作只开放 stage/unstage（N5 P1）**：两者都不改工作区内容，误点可以反向撤销；
 * commit/discard/分支在后续阶段，且必须带二次确认。
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
  return normalizeChanges(data?.data)
}

function normalizeChanges(payload: unknown): ProjectChanges {
  const p = (payload ?? {}) as Partial<ProjectChanges>
  const list: unknown[] = Array.isArray(p.changes) ? p.changes : []
  return {
    repo: !!p.repo,
    git_missing: !!p.git_missing,
    branch: typeof p.branch === 'string' ? p.branch : undefined,
    head: typeof p.head === 'string' ? p.head : undefined,
    changes: list.map(normalizeChange).filter((c): c is ProjectChange => c !== null),
    truncated: !!p.truncated,
  }
}

/** 暂存路径（N5 P1）。`all=true` 时暂存当前全部改动。 */
export async function stageProjectPaths(
  projectId: string,
  body: { paths?: string[]; all?: boolean },
): Promise<ProjectChanges> {
  const { data } = await api.post(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/git/stage`,
    body.all ? { all: true } : { paths: body.paths || [] },
  )
  return normalizeChanges(data?.data)
}

/** 取消暂存（N5 P1）。工作区内容不动。 */
export async function unstageProjectPaths(
  projectId: string,
  body: { paths?: string[]; all?: boolean },
): Promise<ProjectChanges> {
  const { data } = await api.post(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/git/unstage`,
    body.all ? { all: true } : { paths: body.paths || [] },
  )
  return normalizeChanges(data?.data)
}

/** 单个文件的统一 diff（只读）。 */
export interface ProjectDiff {
  repo: boolean
  git_missing?: boolean
  path: string
  /**
   * 未跟踪文件没有可对照的基线：后端不返回 diff，界面改为打开文件预览。
   * 不要在这里合成"+ 每一行"的假 diff —— 行号与基线都会是假的。
   */
  untracked: boolean
  binary: boolean
  truncated: boolean
  diff: string
}

/** 读取单个文件相对 HEAD 的 diff；`path` 必须是相对项目根的 POSIX 路径。 */
export async function readProjectDiff(projectId: string, path: string): Promise<ProjectDiff> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/diff`,
    { params: { path } },
  )
  return normalizeDiff(data?.data ?? {}, path)
}

/** F5：一条提交（只读展示）。 */
export interface ProjectCommit {
  sha: string
  short: string
  subject: string
  author: string
  date: string
}

export interface ProjectLog {
  repo: boolean
  git_missing?: boolean
  branch?: string
  upstream?: string
  ahead: number
  behind: number
  commits: ProjectCommit[]
}

function normalizeDiff(payload: Record<string, unknown>, path: string): ProjectDiff {
  return {
    repo: !!payload.repo,
    git_missing: !!payload.git_missing,
    path: typeof payload.path === 'string' && payload.path ? payload.path : path,
    untracked: !!payload.untracked,
    binary: !!payload.binary,
    truncated: !!payload.truncated,
    diff: typeof payload.diff === 'string' ? payload.diff : '',
  }
}

/** 最近提交 + 分支领先/落后（只读）。 */
export async function readProjectLog(projectId: string, limit = 30): Promise<ProjectLog> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/git-log`,
    { params: { limit } },
  )
  const payload: Record<string, unknown> = data?.data ?? {}
  const rows = Array.isArray(payload.commits) ? payload.commits : []
  return {
    repo: !!payload.repo,
    git_missing: !!payload.git_missing,
    branch: typeof payload.branch === 'string' ? payload.branch : undefined,
    upstream: typeof payload.upstream === 'string' ? payload.upstream : undefined,
    ahead: typeof payload.ahead === 'number' ? payload.ahead : 0,
    behind: typeof payload.behind === 'number' ? payload.behind : 0,
    commits: rows.map((raw) => {
      const c = (raw ?? {}) as Partial<ProjectCommit>
      return {
        sha: typeof c.sha === 'string' ? c.sha : '',
        short: typeof c.short === 'string' ? c.short : '',
        subject: typeof c.subject === 'string' ? c.subject : '',
        author: typeof c.author === 'string' ? c.author : '',
        date: typeof c.date === 'string' ? c.date : '',
      }
    }).filter(c => c.sha !== ''),
  }
}

/** 某次提交的 diff（只读）。 */
export async function readCommitDiff(projectId: string, sha: string): Promise<ProjectDiff> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/commit`,
    { params: { sha } },
  )
  return normalizeDiff(data?.data ?? {}, sha)
}
