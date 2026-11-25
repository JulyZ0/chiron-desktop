/**
 * 还原点（检查点）的数据面：时间线 + 预览 + 回滚 + 回收。
 *
 * **为什么要读 Chiron 自己的表，而不是 git log**：这些点由引擎在每次回合边界
 * 自动创建，含义是「agent 动手之前/之后的世界长什么样」。`git log` 里看不到
 * 未跟踪文件的 CAS 内容（`node_modules` 之外的临时产物、用户被 .gitignore 忽略的
 * 草稿），而回滚恰恰需要它们 —— 清单存在 `checkpoint_untracked` 里（见
 * `internal/api/project_checkpoints.go`）。
 *
 * **预览与回滚是两条端点**：预览（`POST /v1/checkpoints/{id}/diff`）**只读**，
 * 在引擎侧用临时索引/临时 CAS 算差异，工作区一个字节都不动。它回答的问题与
 * 「改动」页签不同：那边是「当前工作区 vs HEAD」，这边是「当前 vs 某个还原点」，
 * 两者口径不同，不能混着展示。
 */
import { api } from './index'

/** `pre_turn` = 回合开始前，`post_turn` = 回合正常结束后。 */
export type CheckpointKind = 'pre_turn' | 'post_turn' | string

/** `ok` = 完整写出；其它值说明这次快照本身就没写全，不能拿来回滚。 */
export type CheckpointState = 'ok' | string

/**
 * 规模统计：时间线头部显示「当前快照多大 / 近 30 天平均多大」。
 *
 * `sample_count === 0` 时平均值没有意义（会显示 0），前端据此显示"暂无数据"。
 */
export interface CheckpointStats {
  /** 统计窗口内的成功检查点数 */
  sample_count: number
  avg_file_count: number
  avg_byte_size: number
  window_days: number
  /** 当前工作区快照（仅预览响应里有） */
  current_files?: number
  current_bytes?: number
  /** 未跟踪 CAS 的规模 */
  store_files?: number
  store_bytes?: number
}

/**
 * 回滚差异预览。**全部是"会做什么"，不是"做了什么"** —— 这条端点绝不动文件。
 */
export interface CheckpointDiffPreview {
  /** 内容会被改回快照版本 */
  restored: string[]
  /** 快照里有、工作区没有，回滚会新建 */
  added: string[]
  /** 当前有、目标没有。**默认不删**（与 `allow_delete: false` 同口径） */
  removed: string[]
  /** 本会出现在 `removed`、但命中保护名单（密钥/噪声）而永不删除 */
  protected_removed: string[]
  /** 目标里是 gitlink 等无法恢复的内容 */
  unsupported: string[]
  /**
   * `true` = 这个点的未跟踪清单读不到，预览**只覆盖 git 跟踪的文件**。
   * 必须显示：否则"预览里没有差异"会被理解成"回滚不会动任何东西"。
   */
  untracked_omitted: boolean
  stats: CheckpointStats
}

/** 回收结果（`dry_run` 时 `deleted`/`freed_bytes` 是计划值）。 */
export interface CheckpointGcResult {
  scanned: number
  referenced: number
  deleted: number
  freed_bytes: number
  dry_run: boolean
  expired: number
  orphan: number
  kept_young: number
  store_files: number
  store_bytes: number
  refs: number
  commits: number
  tree_paths: number
  dangling_refs: string[]
  planned: string[]
  retention_days: number
  /**
   * **服务端签发**的计划摘要（H73）。
   *
   * dry-run 时由网关按计划内容算出并下发；执行阶段必须原样回传，网关会重算比对，
   * 不一致直接 409。老网关不返回该字段 ⇒ 前端回退到本地摘要，但那种情况下
   * 执行请求也会被新网关以 400（缺摘要）拒绝 —— 两侧必须同版本。
   */
  plan_digest?: string
  stats: CheckpointStats
}

/** 时间线 + 规模统计。两者一起返回，省一次往返。 */
export interface CheckpointListResult {
  checkpoints: ProjectCheckpoint[]
  stats: CheckpointStats | null
}

export interface ProjectCheckpoint {
  id: string
  /** 会话内递增序号，时间线排序依据（倒序 = 最新在前） */
  seq: number
  kind: CheckpointKind
  state: CheckpointState
  /** 用户或系统给这个点起的名字；空串表示没起 */
  label: string
  commit_sha: string
  ref_name: string
  session_id?: string
  file_count: number
  /** 因为体积/密钥被跳过的文件数。非 0 意味着这个点还原得**不完整** */
  skipped_count: number
  byte_size: number
  /** 非空说明这个点创建失败过，值是可读的错误码（git / io / unknown） */
  error_code?: string
  /** 失败原因的可读描述（`state != 'ok'` 时非空）。只有错误码时用户仍不知道为什么 */
  error_msg?: string
  /** 非空 = 这个点曾被回滚到过，界面据此标记 */
  applied_at?: string
  created_at: string
}

export interface RestoreResult {
  ok: boolean
  /** 内容被覆盖回快照版本的文件 */
  written: string[]
  /** 快照里有、工作区没有，因此被新建出来的文件 */
  added: string[]
  deleted: string[]
  pruned_dirs: string[]
  /** 逐个文件的失败原因。**非空时 ok 为 false**，界面必须列出来 */
  failed: Array<{ path: string; error: string }>
  /**
   * 快照之后新增、但本次**没有删**的文件。
   * `allow_delete: false` 时这就是全部「当前有、目标没有」的路径 ——
   * 必须显示，否则用户会以为工作区已经完全回到那一刻了。
   */
  skipped_removed: string[]
  /** 命中保护名单（.env / 密钥类）而没有删除的文件 */
  protected_removed: string[]
  unsupported: string[]
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
}

function normalizeCheckpoint(raw: unknown): ProjectCheckpoint | null {
  const c = (raw ?? {}) as Record<string, unknown>
  const id = str(c.id)
  if (!id) return null
  return {
    id,
    seq: num(c.seq),
    kind: str(c.kind) || 'pre_turn',
    state: str(c.state) || 'ok',
    label: str(c.label),
    commit_sha: str(c.commit_sha),
    ref_name: str(c.ref_name),
    session_id: str(c.session_id) || undefined,
    file_count: num(c.file_count),
    skipped_count: num(c.skipped_count),
    byte_size: num(c.byte_size),
    error_code: str(c.error_code) || undefined,
    error_msg: str(c.error_msg) || undefined,
    applied_at: str(c.applied_at) || undefined,
    created_at: str(c.created_at),
  }
}

function normalizeStats(raw: unknown): CheckpointStats | null {
  if (!raw || typeof raw !== 'object') return null
  const s = raw as Record<string, unknown>
  // 全部字段都可能缺失（老网关没有 stats）；缺了就按 0 处理，但不返回 null ——
  // 返回 null 会让界面以为"没有统计数据"，而真相只是这个字段还没上线。
  return {
    sample_count: num(s.sample_count),
    avg_file_count: num(s.avg_file_count),
    avg_byte_size: num(s.avg_byte_size),
    window_days: num(s.window_days) || 30,
    current_files: num(s.current_files),
    current_bytes: num(s.current_bytes),
    store_files: num(s.store_files),
    store_bytes: num(s.store_bytes),
  }
}

/**
 * 读项目的时间线。`sessionId` 非空时只看这一个会话的点。
 *
 * 没有还原点不是错误 —— 返回空数组。项目刚建、或者还没跑过回合，都属于正常。
 */
export async function listProjectCheckpoints(
  projectId: string,
  sessionId?: string,
): Promise<CheckpointListResult> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/checkpoints${query}`,
  )
  const payload = data?.data ?? {}
  const list: unknown[] = Array.isArray(payload.checkpoints) ? payload.checkpoints : []
  return {
    checkpoints: list
      .map(normalizeCheckpoint)
      .filter((c): c is ProjectCheckpoint => c !== null),
    stats: normalizeStats(payload.stats),
  }
}

/**
 * 预览"回到这个还原点"会动哪些文件。**只读**：引擎用临时索引/临时 CAS 取快照。
 *
 * 失败（点不存在 / 不是你的 / 工作区已移走）会抛错，由调用方就地显示 ——
 * 预览失败不该静默变成"没有差异"。
 */
export async function previewCheckpointDiff(
  checkpointId: string,
): Promise<CheckpointDiffPreview> {
  const { data } = await api.post(
    `/v1/checkpoints/${encodeURIComponent(checkpointId)}/diff`,
    {},
  )
  const p = data?.data ?? {}
  return {
    restored: strList(p.restored),
    added: strList(p.added),
    removed: strList(p.removed),
    protected_removed: strList(p.protected_removed),
    unsupported: strList(p.unsupported),
    // 缺省按 true 处理：看不到这个字段时**不能**声称"未跟踪部分也预览到了"。
    untracked_omitted: p.untracked_omitted === undefined ? true : !!p.untracked_omitted,
    stats: normalizeStats(p.stats) ?? {
      sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30,
    },
  }
}

/**
 * 回收未跟踪 CAS 里"没人引用且已过保留期"的分片（平台 owner）。
 *
 * `dryRun` 默认 **true**：先看计划再决定。网关侧同样把缺省的 `dry_run` 当 true。
 */
export async function runCheckpointGc(options: {
  retentionDays?: number
  dryRun?: boolean
  workspaceId?: string
  /**
   * 预览（dry-run）时算出的计划摘要，执行阶段原样回传。
   *
   * 网关据此校验「要执行的计划」与「用户看过的计划/目标」一致（PRD H73）：
   * 破坏性操作不能按当前 UI 状态重新选目标。老网关会忽略这个字段。
   */
  planDigest?: string
} = {}): Promise<CheckpointGcResult> {
  const body: Record<string, unknown> = {}
  if (options.retentionDays !== undefined) body.retention_days = options.retentionDays
  if (options.dryRun !== undefined) body.dry_run = options.dryRun
  if (options.workspaceId) body.workspace_id = options.workspaceId
  if (options.planDigest) body.plan_digest = options.planDigest
  const { data } = await api.post('/v1/admin/checkpoints/gc', body)
  const p = data?.data ?? {}
  return {
    scanned: num(p.scanned),
    referenced: num(p.referenced),
    deleted: num(p.deleted),
    freed_bytes: num(p.freed_bytes),
    dry_run: p.dry_run === undefined ? true : !!p.dry_run,
    expired: num(p.expired),
    orphan: num(p.orphan),
    kept_young: num(p.kept_young),
    store_files: num(p.store_files),
    store_bytes: num(p.store_bytes),
    refs: num(p.refs),
    commits: num(p.commits),
    tree_paths: num(p.tree_paths),
    dangling_refs: strList(p.dangling_refs),
    planned: strList(p.planned),
    retention_days: num(p.retention_days) || 30,
    plan_digest: typeof p.plan_digest === 'string' ? p.plan_digest : undefined,
    stats: normalizeStats(p.stats) ?? {
      sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30,
    },
  }
}

/**
 * 把一个还原点重新应用到项目工作区。
 *
 * `allowDelete` 默认 **false**，与后端一致：「回滚」在用户心里的默认含义是
 * 「把改过的文件改回去」，不是「删掉我之后新建的文件」。不删的路径会在
 * `skipped_removed` 里如实返回，让用户带着知情再决定要不要开这个开关。
 */
export async function restoreCheckpoint(
  checkpointId: string,
  options: { allowDelete?: boolean; paths?: string[] } = {},
): Promise<RestoreResult> {
  const body: Record<string, unknown> = {}
  if (options.allowDelete !== undefined) body.allow_delete = options.allowDelete
  if (options.paths !== undefined) body.paths = options.paths
  const { data } = await api.post(
    `/v1/checkpoints/${encodeURIComponent(checkpointId)}/restore`,
    body,
  )
  const p = data?.data ?? {}
  const rawFailed: unknown[] = Array.isArray(p.failed) ? p.failed : []
  return {
    ok: !!p.ok,
    written: strList(p.written),
    added: strList(p.added),
    deleted: strList(p.deleted),
    pruned_dirs: strList(p.pruned_dirs),
    failed: rawFailed
      .map((f: unknown) => {
        const o = (f ?? {}) as Record<string, unknown>
        const path = str(o.path)
        return path ? { path, error: str(o.error) } : null
      })
      .filter((f): f is { path: string; error: string } => f !== null),
    skipped_removed: strList(p.skipped_removed),
    protected_removed: strList(p.protected_removed),
    unsupported: strList(p.unsupported),
  }
}
