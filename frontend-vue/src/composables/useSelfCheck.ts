/**
 * 「系统自检」的**单例**数据源（P1-9）。
 *
 * ## 为什么必须单例
 *
 * 同一个面板在三个地方出现：首页右上角的抽屉、设置面板顶部、欢迎引导第一步之后。
 * 三处各自 `onMounted` 拉一次，等于启动路径多打三组请求，而且三份 `generated_at`
 * 会让"我刚点过刷新"与"旁边那块还是 5 分钟前的数"同时成立 —— 排障页上这是最
 * 容易误导人的状态。所以状态放在模块级：谁先挂载谁触发，后来者直接读。
 *
 * ## 数据来源（全部是既有端点，本文件不新增后端）
 *
 *   1. 五项聚合清单 —— `GET /v1/admin/diagnostics/summary`
 *      （schema / tables / knowledge / checkpoints / deps；owner 可读，非 admin 403）
 *   2. 模型是否已配置 —— `GET /v1/models`（`api/index.ts:listModels`，只含 enabled 行）
 *   3. 价格覆盖率 —— `GET /v1/admin/billing/price-coverage`（**读不到就整项不显示**，
 *      不把"没权限"渲染成"覆盖率 0%"）
 *
 * ## 三条硬约束
 *
 *   - **非 admin 不下发请求**：面板整体隐藏是产品决定；把一次 403 渲染成"自检失败"
 *     会让普通用户以为系统坏了。`available` 就是这个闸门。
 *   - **任何一项失败都不抛错**：自检是辅助信息，不能阻塞主流程。失败退化成
 *     `unknown`（"看不见"），与 `error`（"有问题"）分开 —— 两者的处置正好相反。
 *   - **5 分钟缓存 + 手动刷新**：面板会随页面反复挂载，没有缓存就是启动路径上的
 *     噪音请求。失败**不缓存**（`fetchedAt` 归零），否则一次网络抖动会把"异常"
 *     状态钉在界面上 5 分钟。
 */
import { computed, ref } from 'vue'

import { api, listModels } from '../api'
import {
  getDiagnosticsSummary,
  type DiagSummaryCheck,
  type DiagSummaryStatus,
} from '../api/admin'
import { useAuthStore } from '../stores/auth'

// ── 类型 ─────────────────────────────────────────────────────────────────────

/** 与 `DiagSummaryStatus` 同集，另加 `unknown` 表示"这一项没读到" */
export type SelfCheckStatus = 'ok' | 'warn' | 'error' | 'unknown'

/** 面板里的七行：五项聚合清单 + 模型 + 价格覆盖率 */
export type SelfCheckKey =
  | 'schema'
  | 'tables'
  | 'knowledge'
  | 'checkpoints'
  | 'deps'
  | 'models'
  | 'pricing'

export interface SelfCheckItem {
  key: SelfCheckKey
  status: SelfCheckStatus
  /**
   * 后端给的观测事实（五项清单直接透传）。模型 / 价格两项为空 ——
   * 它们的文案要跟随界面语言，由面板按 `extra` 组句，不在这里拼中文字符串。
   */
  detail: string
  /** 「去修复」跳转；`null` = 这一项没有对应的修复入口（如 tables 由迁移统一处理） */
  fixPath: string | null
  /** 模型 / 价格项的数值，供面板组本地化文案 */
  extra?: {
    count?: number
    total?: number
    priced?: number
    unpriced?: number
    days?: number
  }
}

/** 缓存有效期：5 分钟。 */
export const SELF_CHECK_TTL_MS = 5 * 60_000

/**
 * 「去修复」的落点。
 *
 * 只给**真的能修**的项配路径：tables 缺表要靠迁移（`schema` 项已经指向
 * `/schema-mismatch`），checkpoints 是引擎内部保护，没有可点的页面 —— 硬塞一个
 * 跳转只会让用户点过去发现没有可操作的东西。
 */
export const SELF_CHECK_FIX_PATHS: Record<SelfCheckKey, string | null> = {
  schema: '/schema-mismatch',
  tables: null,
  knowledge: '/knowledge/settings',
  checkpoints: null,
  deps: '/admin/deps',
  models: '/models',
  pricing: '/billing',
}

/** 五项聚合清单的展示顺序（也是"先看什么"的排障次序，后端已按此返回）。 */
export const SELF_CHECK_SUMMARY_KEYS: readonly SelfCheckKey[] = [
  'schema',
  'tables',
  'knowledge',
  'checkpoints',
  'deps',
]

/** 价格覆盖率的端点（BillingView 也读它；这里只读，不写单价）。 */
const PRICE_COVERAGE_URL = '/v1/admin/billing/price-coverage'

interface PriceCoverageLite {
  period_days?: number
  total_models?: number
  priced_models?: number
  unpriced_models?: number
}

// ── 单例状态 ─────────────────────────────────────────────────────────────────

const items = ref<SelfCheckItem[]>([])
const loading = ref(false)
const error = ref('')
/** 最近一次**成功**采集的时间戳（0 = 没有可复用的结果） */
const fetchedAt = ref(0)

let inFlight: Promise<void> | null = null

function errorText(e: unknown): string {
  const err = e as { response?: { data?: { error?: unknown } }; message?: string }
  const server = err?.response?.data?.error
  if (typeof server === 'string' && server) return server
  return typeof err?.message === 'string' ? err.message : ''
}

function isFresh(now: number): boolean {
  return fetchedAt.value > 0 && now - fetchedAt.value < SELF_CHECK_TTL_MS
}

// ── 三段采集：彼此隔离，谁失败都不影响别人 ───────────────────────────────────

async function readSummary(): Promise<{ checks: DiagSummaryCheck[]; error: string }> {
  try {
    const res = await getDiagnosticsSummary()
    return { checks: Array.isArray(res.checks) ? res.checks : [], error: '' }
  } catch (e) {
    return { checks: [], error: errorText(e) }
  }
}

async function readModelCount(): Promise<{ ok: boolean; count: number; error: string }> {
  try {
    const models = await listModels()
    return { ok: true, count: Array.isArray(models) ? models.length : 0, error: '' }
  } catch (e) {
    return { ok: false, count: 0, error: errorText(e) }
  }
}

/**
 * 价格覆盖率。**读不到返回 null**（整项不显示），而不是 0%。
 * 端点回 `{}`（老后端）也按"读不到"处理：把空对象当覆盖率会渲染成
 * "0 个模型 / 0%"，读者会以为价格表坏了。
 */
async function readPriceCoverage(): Promise<PriceCoverageLite | null> {
  try {
    // `__silent`：这一项失败不该弹全局 toast —— 自检本身在检查"哪里有毛病"，
    // 再叠一条与当前操作无关的红色提示只是噪音。
    const resp = await api.get(PRICE_COVERAGE_URL, { __silent: true })
    const data = (resp?.data?.data ?? resp?.data) as PriceCoverageLite | undefined
    if (!data || typeof data.total_models !== 'number') return null
    return data
  } catch {
    return null
  }
}

function normalizeStatus(status: DiagSummaryStatus | undefined): SelfCheckStatus {
  switch (status) {
    case 'ok':
    case 'warn':
    case 'error':
    case 'unknown':
      return status
    default:
      return 'unknown'
  }
}

/** 把三段读数折成七行。纯函数，便于单测。 */
export function buildSelfCheckItems(
  checks: DiagSummaryCheck[],
  modelCount: { ok: boolean; count: number },
  coverage: PriceCoverageLite | null,
): SelfCheckItem[] {
  const byKey = new Map(checks.map(c => [c.key, c]))
  const out: SelfCheckItem[] = SELF_CHECK_SUMMARY_KEYS.map(key => {
    const hit = byKey.get(key)
    return {
      key,
      // 缺项 = 后端没报这一项（版本差异 / 该组件未部署）→ unknown，不能算 ok
      status: normalizeStatus(hit?.status),
      detail: hit?.detail ?? '',
      fixPath: SELF_CHECK_FIX_PATHS[key],
    }
  })

  out.push({
    key: 'models',
    // 读到 0 个模型是**明确的问题**（error），读不到是 unknown —— 两者处置相反
    status: modelCount.ok ? (modelCount.count > 0 ? 'ok' : 'error') : 'unknown',
    detail: '',
    fixPath: SELF_CHECK_FIX_PATHS.models,
    extra: { count: modelCount.count },
  })

  if (coverage) {
    const total = Math.max(0, Math.trunc(coverage.total_models ?? 0))
    const priced = Math.max(0, Math.trunc(coverage.priced_models ?? 0))
    const unpriced =
      typeof coverage.unpriced_models === 'number'
        ? Math.max(0, Math.trunc(coverage.unpriced_models))
        : Math.max(0, total - priced)
    out.push({
      key: 'pricing',
      // 区间内没有可计价的模型 = 没样本可判（unknown），不是"全绿"
      status: total <= 0 ? 'unknown' : unpriced > 0 ? 'warn' : 'ok',
      detail: '',
      fixPath: SELF_CHECK_FIX_PATHS.pricing,
      extra: {
        total,
        priced,
        unpriced,
        days: Math.max(0, Math.trunc(coverage.period_days ?? 90)),
      },
    })
  }

  return out
}

async function collect(): Promise<{ items: SelfCheckItem[]; error: string }> {
  // 三段**并行**：互不依赖，串行只是白等两个往返。每一段自己吞掉异常，
  // 所以 Promise.all 不会因为某一段失败而整组 reject。
  const [summary, models, coverage] = await Promise.all([
    readSummary(),
    readModelCount(),
    readPriceCoverage(),
  ])

  return {
    items: buildSelfCheckItems(summary.checks, models, coverage),
    error: summary.error || models.error,
  }
}

/**
 * 拉一次。并发调用合并成一次（三个入口可能同时挂载）。
 * `force=false` 时 5 分钟内直接复用缓存。
 */
function load(force: boolean): Promise<void> {
  if (!force && isFresh(Date.now())) return Promise.resolve()
  if (inFlight) return inFlight

  loading.value = true
  inFlight = (async () => {
    const result = await collect()
    items.value = result.items
    error.value = result.error
    // 失败不写入时间戳：下一次挂载会重试，而不是把"异常/未知"钉住 5 分钟。
    // 旧 items 保留 —— 排障时"最后一次成功的读数"比一片空白有用。
    fetchedAt.value = result.error ? 0 : Date.now()
  })().finally(() => {
    loading.value = false
    inFlight = null
  })

  return inFlight
}

/** 供测试重置模块级状态；生产代码不要调用 —— 单例的生命周期就是应用的生命周期。 */
export function resetSelfCheckForTest(): void {
  inFlight = null
  items.value = []
  loading.value = false
  error.value = ''
  fetchedAt.value = 0
}

// ── 对外接口 ─────────────────────────────────────────────────────────────────

/**
 * 面板用。返回的是**共享**状态，任何入口刷新，其它入口立刻看到同一份读数。
 *
 * `available` 是权限闸门：非 admin/owner 读不到 `/admin/diagnostics/summary`（403），
 * 所以一个请求都不发，面板整体隐藏（不报错）。
 */
export function useSelfCheck() {
  const auth = useAuthStore()

  const available = computed(() => {
    // 身份是异步到达的：这里用 computed 跟随 store，角色一到就变成可读
    try {
      return !!auth?.isAdmin
    } catch {
      return false
    }
  })

  const overall = computed<SelfCheckStatus>(() => {
    const list = items.value
    if (!list.length) return 'unknown'
    if (list.some(i => i.status === 'error')) return 'error'
    if (list.some(i => i.status === 'warn')) return 'warn'
    if (list.some(i => i.status === 'unknown')) return 'unknown'
    return 'ok'
  })

  /** 需要处理的行数（error + warn）；unknown 不算"需要处理" */
  const issueCount = computed(
    () => items.value.filter(i => i.status === 'error' || i.status === 'warn').length,
  )

  function ensureLoaded(): Promise<void> {
    if (!available.value) return Promise.resolve()
    return load(false)
  }

  function refresh(): Promise<void> {
    if (!available.value) return Promise.resolve()
    return load(true)
  }

  return {
    available,
    items,
    loading,
    error,
    fetchedAt,
    overall,
    issueCount,
    ensureLoaded,
    refresh,
  }
}
