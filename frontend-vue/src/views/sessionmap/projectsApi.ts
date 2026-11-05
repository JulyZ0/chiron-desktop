/**
 * 项目的**数据面**（会话地图改造后新增的一层）。
 *
 * 与 `./api.ts` 的分工：
 *   - `api.ts` 管**画布**（viewport + nodes 整体读写）；
 *   - 这里管**项目**（绑定文件夹、列表、会话归属）。
 *
 * 两者共用同一张后端表，但语义不同 —— 混在一个文件里早晚会有人拿"保存画布"
 * 的 body 去覆盖归属。后端也一样分成两组端点（见 `internal/api/sessionmap_projects.go`）。
 *
 * 错误处理：`bindSession` 的 409 需要调用方分支（提示"已在别的项目，是否移动"），
 * 所以这里**不吞异常**，原样抛给调用方用 `errorStatus` / `serverErrorMessage` 判断。
 */
import { api } from '../../api'

import type {
  ProjectBindConflict,
  ProjectDraft,
  ProjectSessionSummary,
  ProjectSummary,
} from './types'

function normalizeProject(raw: unknown): ProjectSummary {
  const p = (raw ?? {}) as Partial<ProjectSummary>
  return {
    id: typeof p.id === 'string' ? p.id : '',
    name: typeof p.name === 'string' ? p.name : '',
    root_path: typeof p.root_path === 'string' ? p.root_path : '',
    sort_index: typeof p.sort_index === 'number' ? p.sort_index : 0,
    session_count: typeof p.session_count === 'number' ? p.session_count : 0,
    last_active_at: typeof p.last_active_at === 'string' ? p.last_active_at : '',
    updated_at: typeof p.updated_at === 'string' ? p.updated_at : '',
  }
}

function normalizeSession(raw: unknown): ProjectSessionSummary {
  const s = (raw ?? {}) as Partial<ProjectSessionSummary>
  return {
    id: typeof s.id === 'string' ? s.id : '',
    title: typeof s.title === 'string' ? s.title : '',
    status: typeof s.status === 'string' ? s.status : '',
    updated_at: typeof s.updated_at === 'string' ? s.updated_at : '',
    pinned: !!s.pinned,
    bound_at: typeof s.bound_at === 'string' ? s.bound_at : '',
  }
}

/** 列出全部项目（只含 `root_path` 非空的 workspace）。 */
export async function listProjects(): Promise<ProjectSummary[]> {
  const { data } = await api.get('/v1/session-map/projects')
  const list = data?.data?.projects
  if (!Array.isArray(list)) return []
  return list.map(normalizeProject).filter((p: ProjectSummary) => p.id !== '')
}

/**
 * 用本地文件夹建项目。
 *
 * `root_path` 的合法性**由后端判定**（绝对路径 / 存在 / 是目录 / 非系统目录）：
 * 前端选目录时已经过滤过一轮，但那只是即时反馈，不能替代后端校验。
 */
export async function createProject(draft: ProjectDraft & { root_path: string }): Promise<ProjectSummary> {
  const { data } = await api.post('/v1/session-map/projects', draft)
  return normalizeProject(data?.data)
}

/** 局部更新项目。只提交要改的字段（见 `ProjectDraft` 的注释）。 */
export async function patchProject(id: string, draft: ProjectDraft): Promise<void> {
  await api.patch(`/v1/session-map/projects/${encodeURIComponent(id)}`, draft)
}

/** 删除项目。会话本身不会被删除（I5）。 */
export async function deleteProject(id: string): Promise<void> {
  await api.delete(`/v1/session-map/projects/${encodeURIComponent(id)}`)
}

/** 列出某项目内的会话（按置顶 + 最近更新排序）。 */
export async function listProjectSessions(projectId: string): Promise<ProjectSessionSummary[]> {
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/sessions`,
  )
  const list = data?.data?.sessions
  if (!Array.isArray(list)) return []
  return list.map(normalizeSession).filter((s: ProjectSessionSummary) => s.id !== '')
}

/**
 * 把会话归入项目。
 *
 * `force=false` 时会话已被别的项目占用会抛 409，`error.response.data.data.owner_project_id`
 * 给出占用方（见 `ProjectBindConflict`）。调用方应提示用户后带 `force=true` 重试 ——
 * 那是**移动**语义，后端在同一事务里删旧插新。
 */
export async function bindSession(
  projectId: string,
  sessionId: string,
  force = false,
): Promise<void> {
  const qs = force ? '?force=true' : ''
  await api.put(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}${qs}`,
  )
}

/**
 * 把会话移出项目。会话保留，只是回到「未归入项目」分组。
 *
 * 后端 `DELETE` 幂等：本来就不在项目里也返回成功。
 */
export async function unbindSession(projectId: string, sessionId: string): Promise<void> {
  await api.delete(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}`,
  )
}

/**
 * 列出不属于任何项目的会话。
 *
 * 改造前建的所有会话都没有归属记录；没有这个分组，它们会在侧边栏改造后
 * 全部消失 —— 对用户而言等同于"会话丢了"。
 */
export async function listUnassignedSessions(): Promise<ProjectSessionSummary[]> {
  const { data } = await api.get('/v1/session-map/unassigned-sessions')
  const list = data?.data?.sessions
  if (!Array.isArray(list)) return []
  return list.map(normalizeSession).filter((s: ProjectSessionSummary) => s.id !== '')
}

/** 从 409 响应里取出占用方项目 ID；不是这类冲突则返回 null。 */
export function bindConflictOwner(error: unknown): string | null {
  const err = error as {
    response?: { status?: number; data?: { data?: Partial<ProjectBindConflict> } }
  }
  if (err?.response?.status !== 409) return null
  const owner = err.response.data?.data?.owner_project_id
  return typeof owner === 'string' && owner ? owner : null
}

/**
 * 反查一个会话属于哪个项目。
 *
 * 返回 `null` 表示**不属于任何项目**（未归组的会话），那是正常状态而不是错误 ——
 * 调用方据此让"项目文件"那一源整体缺席，而不是弹一个"找不到"。
 */
export async function sessionProject(
  sessionId: string,
): Promise<{ id: string; name: string; root_path: string } | null> {
  if (!sessionId) return null
  const { data } = await api.get(
    `/v1/session-map/sessions/${encodeURIComponent(sessionId)}/project`,
  )
  const p = data?.data?.project
  if (!p || typeof p.id !== 'string' || !p.id) return null
  return {
    id: p.id,
    name: typeof p.name === 'string' ? p.name : '',
    root_path: typeof p.root_path === 'string' ? p.root_path : '',
  }
}
