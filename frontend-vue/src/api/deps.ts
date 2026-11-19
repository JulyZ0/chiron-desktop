/**
 * Python 依赖管理（后台 `/admin/deps`）的 HTTP 客户端与**校验函数**。
 *
 * 校验之所以放在这里而不是只在视图里：安装/卸载最终会变成 pip 命令，前端这一层的
 * 唯一职责是"在按下按钮之前就告诉用户哪里不对"，不能靠后端 400 兜。规则必须与后端
 * **逐字一致** —— python-engine/app/api/deps.py 的 `PIP_SPEC_RE` / `PACKAGE_NAME_RE`、
 * internal/api/deps_admin.go 的同名正则；三处任何一处放宽，其余两处就成了摆设。
 */
import api from './index'

// ── 类型（与引擎返回的 JSON 字段一一对应）─────────────────────────────────

export interface DepPackage {
  name: string
  version: string
}

export interface DepListResult {
  /** 引擎解释器的 Python 版本（展示"这些包装在哪"） */
  python: string
  /** 引擎解释器的绝对路径：用户据此确认"装的是不是同一个 venv" */
  executable: string
  packages: DepPackage[]
  count: number
}

export interface PypiResult {
  name: string
  version: string
  summary: string
  requires_python?: string
  homepage?: string
}

export interface DepSearchResult {
  query: string
  results: PypiResult[]
  count: number
  /** PyPI 上是否存在该项目（与"网络不可用"是两件事，后者会抛错） */
  found?: boolean
  cached?: boolean
}

export type DepJobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface DepJob {
  id: string
  kind: 'install' | 'uninstall'
  target: string
  status: DepJobStatus
  message: string
  /** 0-100；仅在 running/succeeded/failed 时有意义（queued 为 0） */
  progress: number
  output: string
  returncode?: number | null
  created_at: number
  finished_at?: number | null
}

export interface DepUninstallResult {
  ok: boolean
  name: string
  output: string
}

// ── 校验（纯函数，vitest 直接覆盖）──────────────────────────────────────

/**
 * 允许的 pip 规格：`name` 或 `name<op>version`。
 *
 * 名称必须以字母/数字开头并结尾 —— 这条约束挡住了 pip 的"参数即路径"语义：
 * `.env` / `-r` 都只由名称字符合法组成，但前者会被当成"从本地目录安装"、后者会被
 * 当成 `--requirement` 的缩写（从而把 `.env` 里的密钥读成需求清单）。
 */
export const PIP_SPEC_RE = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:[<>=!~]=?[A-Za-z0-9.*+_-]+)?$/

/** 卸载只接受纯包名（带版本约束的卸载没有意义）。 */
export const PACKAGE_NAME_RE = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/

/** 与后端 `MAX_SPEC_LEN` 一致 */
export const MAX_SPEC_LEN = 128

/** 校验失败的稳定原因码；调用方据此取 i18n 文案（`admin.deps_error_<code>`）。 */
export type DepValidationError = 'required' | 'too_long' | 'invalid'

function validate(raw: string, re: RegExp): DepValidationError | null {
  const value = (raw || '').trim()
  if (!value) return 'required'
  if (value.length > MAX_SPEC_LEN) return 'too_long'
  if (!re.test(value)) return 'invalid'
  return null
}

/** 校验安装规格；返回 null 表示合法。 */
export function validatePipSpec(raw: string): DepValidationError | null {
  return validate(raw, PIP_SPEC_RE)
}

/** 校验卸载包名；返回 null 表示合法。 */
export function validatePackageName(raw: string): DepValidationError | null {
  return validate(raw, PACKAGE_NAME_RE)
}

// ── HTTP ────────────────────────────────────────────────────────────────

/** 网关 `OK()` 会包一层 `{success, data}`；引擎直连形态没有包装，两种都认。 */
function unwrap<T>(payload: unknown): T {
  return ((payload as { data?: T } | undefined)?.data ?? payload) as T
}

export async function listDeps(): Promise<DepListResult> {
  const resp = await api.get('/v1/admin/deps/list')
  const data = unwrap<DepListResult>(resp.data)
  return {
    python: data?.python ?? '',
    executable: data?.executable ?? '',
    packages: Array.isArray(data?.packages) ? data.packages : [],
    count: typeof data?.count === 'number' ? data.count : 0,
  }
}

/**
 * PyPI 查询。**注意语义是"精确项目名"**：PyPI 的 JSON API 没有关键词搜索端点，
 * 传 `requests` 能查到 `requests` 本身，传 `http lib` 不会返回候选列表。
 */
export async function searchPypi(q: string): Promise<DepSearchResult> {
  const resp = await api.get('/v1/admin/deps/search', { params: { q } })
  const data = unwrap<DepSearchResult>(resp.data)
  return {
    query: data?.query ?? q,
    results: Array.isArray(data?.results) ? data.results : [],
    count: typeof data?.count === 'number' ? data.count : 0,
    found: data?.found,
    cached: data?.cached,
  }
}

/** 创建安装任务；返回的 job 需要轮询 `getDepJob` 看结果。 */
export async function installDep(spec: string): Promise<DepJob> {
  const resp = await api.post('/v1/admin/deps/install', { spec })
  const data = unwrap<{ job?: DepJob }>(resp.data)
  if (!data?.job) throw new Error('install job missing in response')
  return data.job
}

export async function uninstallDep(name: string): Promise<DepUninstallResult> {
  const resp = await api.post('/v1/admin/deps/uninstall', { name })
  return unwrap<DepUninstallResult>(resp.data)
}

export async function getDepJob(id: string): Promise<DepJob> {
  const resp = await api.get(`/v1/admin/deps/jobs/${encodeURIComponent(id)}`)
  return unwrap<DepJob>(resp.data)
}

/** 任务是否已结束（用于停止轮询）。 */
export function isTerminalJob(status: DepJobStatus | string): boolean {
  return status === 'succeeded' || status === 'failed'
}

/**
 * 比较两个版本号，返回 `latest` 是否比 `installed` 更新。
 *
 * 只做「点分数字」的朴素比较（`2.31.0` vs `2.32.3`）：后台页只用来提示"有更新"，
 * 完整 PEP 440 语义（epoch、pre-release、local version）在这里没有收益 —— 判断错了
 * 也只是少/多一个徽标，不会影响实际安装（安装永远由 pip 决定版本）。
 * 任一版本段含非数字时退化为字符串不等比较（宁可多提示，不误导成"已是最新"）。
 */
export function isNewerVersion(latest: string, installed: string): boolean {
  const a = (latest || '').trim()
  const b = (installed || '').trim()
  if (!a || !b || a === b) return false
  const splitA = a.split('.')
  const splitB = b.split('.')
  const numeric = splitA.every(p => /^\d+$/.test(p)) && splitB.every(p => /^\d+$/.test(p))
  if (!numeric) return a !== b
  const len = Math.max(splitA.length, splitB.length)
  for (let i = 0; i < len; i++) {
    const x = Number(splitA[i] ?? 0)
    const y = Number(splitB[i] ?? 0)
    if (x > y) return true
    if (x < y) return false
  }
  return false
}
