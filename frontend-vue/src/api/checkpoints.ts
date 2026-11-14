/**
 * 还原点（检查点）的数据面：时间线 + 回滚。
 *
 * **为什么要读 Chiron 自己的表，而不是 git log**：这些点由引擎在每次回合边界
 * 自动创建，含义是「agent 动手之前/之后的世界长什么样」。`git log` 里看不到
 * 未跟踪文件的 CAS 内容（`node_modules` 之外的临时产物、用户被 .gitignore 忽略的
 * 草稿），而回滚恰恰需要它们 —— 清单存在 `checkpoint_untracked` 里（见
 * `internal/api/project_checkpoints.go`）。
 *
 * **为什么回滚没有「预览差异」接口**：网关侧的 `restore` 会先算出计划再落地，
 * 但它不返回计划。要看差异用户应该切到「改动」页签 —— 那是当前工作区 vs HEAD，
 * 与「当前 vs 某个还原点」口径不同，不能混着展示。
 */
import { api } from './index'

/** `pre_turn` = 回合开始前，`post_turn` = 回合正常结束后。 */
export type CheckpointKind = 'pre_turn' | 'post_turn' | string

/** `ok` = 完整写出；其它值说明这次快照本身就没写全，不能拿来回滚。 */
export type CheckpointState = 'ok' | string

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
  /** 非空说明这个点创建失败过，值是可读的错误码 */
  error_code?: string
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
    applied_at: str(c.applied_at) || undefined,
    created_at: str(c.created_at),
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
): Promise<ProjectCheckpoint[]> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/checkpoints${query}`,
  )
  const payload = data?.data ?? {}
  const list: unknown[] = Array.isArray(payload.checkpoints) ? payload.checkpoints : []
  return list
    .map(normalizeCheckpoint)
    .filter((c): c is ProjectCheckpoint => c !== null)
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
