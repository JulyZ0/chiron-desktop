import api from './index'

import { projectParams } from '../composables/useProject'
import { t } from '../i18n'
// ── 长期记忆（记忆四层架构 L2 档案卡：跨会话留存） ──

export type MemorySlot = 'identity' | 'preference' | 'decision' | 'fact'
export type MemorySource = 'user_confirmed' | 'derived' | 'tool_written'

/** 记忆的范围：`global` = 所有项目通用，`project` = 某个项目专属。 */
export type MemoryScope = 'global' | 'project'

export interface MemoryEntry {
  id: string
  slot: string
  slot_label: string
  key: string
  value: string
  confidence: number
  source: string
  source_label: string
  has_embedding: boolean
  access_count: number
  last_accessed_at: string | null
  status: string
  created_at: string
  updated_at: string
  /** 条目所属项目（空串 = 全局） */
  project?: string
  /** 由 project 推出的展示口径；旧后端不带该字段 */
  scope?: MemoryScope
}

export interface MemorySearchHit extends MemoryEntry {
  similarity: number
  score: number
}

export interface OrganizeResult {
  backfilled: number
  merged: number
  archived: number
  evicted: number
  errors: string[]
}

export interface OrganizeStatus {
  running: boolean
  started_at: number | null
  finished_at: number | null
  result: OrganizeResult | null
  error: string | null
}

/** 整理状态的空态：任何一层拿不到 status 时的兜底（模板无条件读 `organize.running`）。 */
function emptyOrganizeStatus(): OrganizeStatus {
  return { running: false, started_at: null, finished_at: null, result: null, error: null }
}

/**
 * 解开网关的 `{success, data}` 包装（直接连引擎时是裸 JSON，原样返回）。
 *
 * 为什么必须有这一层：桌面端 `/v1/memory/*` 全部经 Go 网关转发，而网关统一用
 * `OK()` 包成 `{success:true, data:<引擎响应>}`（见 internal/api/response.go），
 * 引擎自己的响应又带 `success` 字段 —— 前端拿到的是**两层**。
 *
 * 漏解一层不会报 HTTP 错，而是静默拿到 `undefined`：`startOrganize()` 曾经就是
 * 这样把 `organize` 写成 `undefined`，模板读 `organize.running` 抛 TypeError，
 * 整页被 ErrorBoundary 接住，用户看到「页面渲染出错」。
 *
 * 判据用 `!== undefined` 而不是 `??`：`data: null` 是有效业务值（如空的单条资源），
 * 不该被当成"没有包装"再退回去取外层对象。
 */
function unwrap<T>(payload: unknown): T {
  const wrapped = payload as { data?: unknown } | null | undefined
  if (wrapped && typeof wrapped === 'object' && wrapped.data !== undefined) {
    return wrapped.data as T
  }
  return payload as T
}

export interface ProfileListResponse {
  entries: MemoryEntry[]
  counts: Record<string, number>
  total: number
  slots: { slot: string; label: string }[]
  organize: OrganizeStatus
}

/** 整卡列表（按槽位分组统计）。C3：只列**当前项目**（空 = 未分组）的条目。 */
export async function listMemory(includeArchived = false): Promise<ProfileListResponse> {
  const { data } = await api.get('/v1/memory/profile', {
    params: { ...(includeArchived ? { archived: 'true' } : {}), ...projectParams() },
  })
  const body = unwrap<Partial<ProfileListResponse>>(data)
  return {
    entries: body?.entries ?? [],
    counts: body?.counts ?? {},
    total: body?.total ?? 0,
    slots: body?.slots ?? [],
    // 列表响应自带的整理态也可能是缺的（旧后端），同样兜底成空态
    organize: body?.organize ?? emptyOrganizeStatus(),
  }
}

/** 新建 / 更新一条记忆（同 slot+key 自动更新） */
export async function upsertMemory(body: {
  slot: MemorySlot
  key: string
  value: string
  confidence?: number
  source?: MemorySource
}, opts?: {
  /**
   * 写入范围：不传 = 当前项目（`projectParams()` 的既有行为）；
   * 传 `''` = 全局；传项目名 = 该项目。
   *
   * 为什么要能覆盖：在**项目视图**里新建记忆，用户仍可能想让它"对所有项目生效"。
   * 若只能跟着视图走，用户就得先切到全局视图再建 —— 一次本可避免的上下文切换。
   * 传 `''` 时必须显式给（而不是"没传"），因为"没传"要表示"跟随当前视图"。
   */
  project?: string
}): Promise<{
  entry: MemoryEntry
  created: boolean
  duplicate_of?: MemoryEntry
  evicted?: number
  /** 与已确认值冲突时后端登记待裁决（不覆盖），由记忆页处理 —— 见 SaveToMemoryDialog */
  conflict?: { old_value?: unknown; new_value?: unknown }
}> {
  const params =
    opts && opts.project !== undefined
      ? opts.project
        ? { project: opts.project }
        : {}
      : projectParams()
  const { data } = await api.post('/v1/memory/profile', body, { params })
  return unwrap(data)
}

/** 编辑已有记忆（按 id） */
export async function updateMemory(body: {
  id: string
  key?: string
  value?: string
  confidence?: number
  source?: MemorySource
}): Promise<{ entry: MemoryEntry }> {
  const { data } = await api.put('/v1/memory/profile', body, { params: projectParams() })
  return unwrap(data)
}

/** 删除单条记忆 */
export async function deleteMemory(id: string): Promise<void> {
  await api.delete(`/v1/memory/profile/${encodeURIComponent(id)}`, {
    params: projectParams(),
  })
}

/**
 * 切换某条记忆的范围：全局 ↔ 某个项目（`toProject` 空串 = 全局）。
 *
 * 与 `updateMemory` 分开是因为语义不同：更新是"就地改内容"，按当前项目精确匹配；
 * 换范围要跨范围动作，所以显式带上"我当前在哪个范围"（`?project=`，由
 * `projectParams()` 提供）与目标范围。**只有当前范围里看得见的条目才挪得动** ——
 * 项目视图里动不了全局条目，这也是"全局条目只读"的实现方式。
 */
export async function moveMemoryScope(id: string, toProject: string): Promise<{ entry: MemoryEntry }> {
  const { data } = await api.post(
    `/v1/memory/profile/${encodeURIComponent(id)}/scope`,
    { to_project: toProject },
    { params: projectParams() },
  )
  return unwrap(data)
}

/** 清空**当前项目**的记忆（需 confirm=true） */
export async function clearMemory(): Promise<{ deleted: number }> {
  const { data } = await api.post(
    '/v1/memory/profile/clear',
    { confirm: true },
    { params: projectParams() },
  )
  return unwrap(data)
}

/** L3 对话摘要：记忆页「摘要」分区与检索结果的 L3 区是同一形态，故共用此类型 */
export interface MemorySummary {
  id: string
  session_id: string
  content: string
  topics: string[]
  turn_range: number[]
  access_count: number
  created_at: number | null
  has_embedding: boolean
  status: string
}

/** @deprecated 用 MemorySummary（同一形态；旧名带检索语义，用在摘要列表里会误导） */
export type MemorySearchSummary = MemorySummary

export interface MemorySearchResponse {
  query: string
  /** results 的匹配方式：语义，或嵌入不可用时的关键词降级 */
  mode: 'semantic' | 'keyword'
  /** L2 命中数（不含 summaries） */
  count: number
  results: MemorySearchHit[]
  /** L3 区：同一 query 在历史对话摘要里的命中 */
  summaries: MemorySearchSummary[]
  summary_count: number
  /**
   * L3 的检索方式（M22）：`semantic`（按查询向量筛过）/ `recent`（没做向量，取最近）/
   * `keyword`（嵌入不可用时的关键词降级）/ `empty`（没命中，或 L3 不可用）。
   *
   * ⚠ 与上面的 `mode`（L2）**是两回事**：两条链路各算各的，可以同时不同
   * （例如 L2 走关键词、L3 走向量）。旧后端不带这个字段时为 `undefined`。
   */
  summary_mode?: 'semantic' | 'recent' | 'keyword' | 'empty'
}

/** 检索记忆：返回 L2 条目命中 + L3 摘要命中两个独立结果区 */
export async function searchMemory(
  query: string,
  opts?: { top_k?: number; slot?: MemorySlot },
): Promise<MemorySearchResponse> {
  const { data } = await api.post(
    '/v1/memory/search',
    {
      query,
      top_k: opts?.top_k ?? 10,
      slot: opts?.slot,
    },
    { params: projectParams() },
  )
  // 和解包前同样的坑：漏解一层后 `results` / `mode` 都是 undefined，
  // 检索永远显示"没找到相似记忆" —— 症状像"库里没数据"，其实是解析错。
  const body = unwrap<Partial<MemorySearchResponse>>(data)
  const results = body?.results ?? []
  const summaries = body?.summaries ?? []
  return {
    query: body?.query ?? query,
    mode: body?.mode ?? 'keyword',
    count: body?.count ?? results.length,
    results,
    summaries,
    summary_count: body?.summary_count ?? summaries.length,
    summary_mode: body?.summary_mode,
  }
}

/** 触发**当前项目**的异步智能整理（去重 / 归档 / 补嵌入） */
export async function startOrganize(): Promise<{ started: boolean; status: OrganizeStatus }> {
  const { data } = await api.post('/v1/memory/organize', undefined, {
    params: projectParams(),
  })
  const body = unwrap<{ started?: boolean; status?: OrganizeStatus }>(data)
  // `status` 兜底：`runOrganize` 会把它直接写进 `organize`，模板随后无条件读
  // `organize.running` —— 任何一层缺字段都会让整页崩进 ErrorBoundary。
  return { started: body?.started === true, status: body?.status ?? emptyOrganizeStatus() }
}

/** 列出 L3 对话摘要（记忆页「摘要」分区） */
export async function listSummaries(
  limit = 50,
): Promise<{ summaries: MemorySummary[]; count: number }> {
  const { data } = await api.get('/v1/memory/summaries', { params: { limit } })
  const body = unwrap<{ summaries?: MemorySummary[]; count?: number }>(data)
  const summaries = body?.summaries ?? []
  return { summaries, count: body?.count ?? summaries.length }
}

/** 整理任务状态 */
export async function getOrganizeStatus(): Promise<OrganizeStatus> {
  const { data } = await api.get('/v1/memory/organize/status', {
    params: projectParams(),
  })
  // 网关包一层 `{success, data}`，引擎再包一层 `{success, status}` —— 逐层解开。
  const body = unwrap<{ status?: OrganizeStatus }>(data)
  return body?.status ?? emptyOrganizeStatus()
}

export const MEMORY_SLOTS: { slot: MemorySlot; label: string }[] = [
  { slot: 'identity', label: t('admin.identity') },
  { slot: 'preference', label: t('settings.preferences') },
  { slot: 'decision', label: t('common.key_decisions') },
  { slot: 'fact', label: t('common.long_term_facts') },
]

// ── 冲突裁决 API ──

export interface MemoryConflict {
  conflict_id: string
  slot: string
  item_key: string
  old_value: string
  new_value: string
  source: string
  created_at: number
}

export interface ConflictListResponse {
  conflicts: MemoryConflict[]
  count: number
}

/** 获取待裁决冲突列表 */
export async function listConflicts(): Promise<ConflictListResponse> {
  const { data } = await api.get('/v1/memory/conflicts')
  const body = unwrap<{ conflicts?: MemoryConflict[]; count?: number }>(data)
  const conflicts = body?.conflicts ?? []
  return { conflicts, count: body?.count ?? conflicts.length }
}

export interface ResolveConflictResponse {
  conflict_id: string
  final_value: string
  resolution: string
  slot: string
  item_key: string
  profile_update?: {
    success: boolean
    item: string | null
  }
}

/**
 * 裁决冲突
 * @param conflictId 冲突 ID
 * @param resolution 裁决方式: 'keep_old' | 'use_new' | 'manual'
 * @param manualValue 手动修改值（仅当 resolution === 'manual'）
 */
export async function resolveConflict(
  conflictId: string,
  resolution: 'keep_old' | 'use_new' | 'manual',
  manualValue?: string,
): Promise<ResolveConflictResponse> {
  const body: { resolution: string; manual_value?: string } = { resolution }
  if (resolution === 'manual' && manualValue !== undefined) {
    body.manual_value = manualValue
  }
  const { data } = await api.post(`/v1/memory/conflicts/${encodeURIComponent(conflictId)}/resolve`, body)
  return unwrap<{ conflict: ResolveConflictResponse }>(data)?.conflict
}

/** 删除冲突（用户否认） */
export async function deleteConflict(conflictId: string): Promise<{ deleted: string }> {
  const { data } = await api.delete(`/v1/memory/conflicts/${encodeURIComponent(conflictId)}`)
  return unwrap(data)
}
