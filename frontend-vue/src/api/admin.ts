import api from './index'

// ── Types ──

export interface AdminMetrics {
  concurrent_connections: number
  queue_backlog: number
  cache_hit_rate: number
  api_latency_p99: number
  [key: string]: unknown
}

export interface AdminUser {
  id: string
  email: string
  name: string
  role: string
  created_at: string
  updated_at: string
}

export interface SystemInfo {
  version: string
  uptime_seconds: number
  database: string
  redis: string
  [key: string]: unknown
}

export interface StorageConfig {
  backend: string
  config: Record<string, unknown>
}

export interface RedisInfo {
  mode: string
  stats: Record<string, unknown>
}

export interface QueueStats {
  stream_key: string
  task_queue_length: number
  vip_queue_length: number
  groups: number
  consumers: number
  lag: number
  throughput_qps: number
  active_requests: number
  waiting_tasks: QueueTask[]
}

export interface QueueTask {
  task_id: string
  user_id: string
  content: string
  queued_at: string
  position: number
  is_vip: boolean
}

export interface CacheStats {
  total_hit_rate: number
  total_requests: number
  total_hits: number
  total_misses: number
  l1_hits: number
  l2_hits: number
  l3_hits: number
  redis_hit_rate: number
  redis_keyspace_hits: number
  redis_keyspace_misses: number
  redis_memory_mb: number
  redis_max_memory_mb: number
  hot_queries: HotQuery[]
}

export interface HotQuery {
  query: string
  hits: number
  hit_rate: number
  avg_latency_ms: number
}

export interface PerformanceStats {
  gateway: {
    instances: number
    cpu_percent: number
    memory_mb: number
    goroutines: number
    connections: number
    redis_latency_ms: number
    db_latency_ms: number
    uptime_seconds: number
    version: string
  }
  python_engine: {
    pods: number
    cpu_percent: number
    memory_mb: number
    active_tasks: number
    avg_inference_ms: number
    redis_latency_ms: number
    uptime_seconds: number
    version: string
  }
  latency_distribution: Record<string, number>
  qps_trend: { time: string; qps: number }[]
}

export interface ApiKey {
  id: string
  provider: string
  key_preview: string
  status: 'active' | 'rate_limited' | 'circuit_open'
  weight: number
  failures: number
  last_used: string
  remark: string
}

/** 服务提供商目录分组（与 Go 网关 internal/api/llm_providers.go 的 category 一致） */
export type LlmProviderCategory = 'international' | 'china' | 'aggregator' | 'self_hosted'

/** 接入协议：openai = OpenAI 兼容，anthropic = Anthropic Messages */
export type LlmProviderKind = 'openai' | 'anthropic'

/**
 * 服务提供商目录项 + 运行时状态。
 * 目录为权威源的只读投影：base_url 是目录默认端点，
 * base_url_override 是管理端在 DB 里的覆盖值，effective_base_url 是最终生效值。
 */
export interface LlmProviderPreset {
  id: string
  label: string
  vendor: string
  category: LlmProviderCategory
  kind: LlmProviderKind
  base_url: string
  api_key_env: string
  api_key_prefix: string
  model_prefixes: string[]
  docs_url: string
  cost: number
  quality: number
  requires_key: boolean
  model_discovery: boolean
  configured: boolean
  key_count: number
  base_url_override: string
  effective_base_url: string
}

// ── Dashboard ──

export async function getMetrics(): Promise<AdminMetrics> {
  const { data } = await api.get('/v1/admin/metrics')
  return data.data
}

// ── Users ──

export async function listUsers(): Promise<AdminUser[]> {
  const { data } = await api.get('/v1/admin/users')
  return data.data?.users || []
}

export async function getUser(id: string): Promise<AdminUser> {
  const { data } = await api.get(`/v1/admin/users/${id}`)
  return data.data?.user
}

export async function updateUser(id: string, updates: Partial<AdminUser>): Promise<void> {
  await api.put(`/v1/admin/users/${id}`, updates)
}

export async function deleteUser(id: string): Promise<void> {
  await api.delete(`/v1/admin/users/${id}`)
}

// ── System ──

export async function getSystemInfo(): Promise<SystemInfo> {
  const { data } = await api.get('/v1/admin/system')
  return data.data
}

export async function triggerMaintenance(action: 'vacuum' | 'reindex' | 'analyze' | 'flush_cache'): Promise<void> {
  await api.post('/v1/admin/maintenance', { action })
}

// 注：`downloadBackup`（POST /v1/admin/backup）已随「容器内备份」整体移除 ——
// 企业部署用云数据库，备份由服务商承担（快照 / PITR）。

// ── Storage ──

export async function getStorage(): Promise<StorageConfig> {
  const { data } = await api.get('/v1/admin/storage')
  return data.data
}

export async function updateStorage(config: { backend: string; [key: string]: unknown }): Promise<void> {
  await api.put('/v1/admin/storage', config)
}

export async function testStorage(): Promise<{ success: boolean; message: string }> {
  const { data } = await api.post('/v1/admin/storage/test')
  return data.data
}

// ── Redis ──

export async function getRedis(): Promise<RedisInfo> {
  const { data } = await api.get('/v1/admin/redis')
  return data.data
}

export async function updateRedis(config: { mode: string; [key: string]: unknown }): Promise<void> {
  await api.put('/v1/admin/redis', config)
}

export async function testRedis(): Promise<{ success: boolean; message: string }> {
  const { data } = await api.post('/v1/admin/redis/test')
  return data.data
}

// ── Queue ──

export async function getQueueStats(): Promise<QueueStats> {
  const { data } = await api.get('/v1/admin/queue')
  return data.data
}

export async function flushQueue(): Promise<void> {
  await api.post('/v1/admin/queue/flush')
}

export async function pauseQueue(pause: boolean): Promise<void> {
  await api.post('/v1/admin/queue/pause', { pause })
}

// ── Cache ──

export async function getCacheStats(): Promise<CacheStats> {
  const { data } = await api.get('/v1/admin/cache/stats')
  return data.data
}

// ── Performance ──

export async function getPerformance(): Promise<PerformanceStats> {
  const { data } = await api.get('/v1/admin/performance')
  return data.data
}

// ── 运行时监控（性能 / 队列 / 缓存统一数据源）──

/**
 * 一次取全三个面板。`window` 决定请求统计的时间窗（5m/15m/60m），
 * 后端按同一窗口计算 samples / error_rate / P50/P95/P99。
 */
export async function getMonitorOverview(window: MonitorWindow = '15m'): Promise<MonitorOverview> {
  const { data } = await api.get('/v1/admin/monitor/overview', { params: { window } })
  return data.data
}

// ── 运行时监控（统一数据源）──
//
// 与 GET /v1/admin/monitor/overview 一一对应。所有"可能采不到"的数值都是
// `number | null`：null 表示**未知**（PG/Redis/引擎不可达或没有样本），
// 界面必须显示「—」，不能 `?? 0` —— 0 会被读成"确实是零"。

export type MonitorWindow = '5m' | '15m' | '60m'
export type MonitorSectionStatus = 'ok' | 'partial' | 'unavailable'

export interface MonitorComponentError {
  component: string
  message: string
}

export interface MonitorSectionMeta {
  generated_at: string
  status: MonitorSectionStatus
  sources: string[]
  errors: MonitorComponentError[]
}

export interface MonitorBucket {
  at: string
  count: number
  errors: number
  p95_ms: number
  avg_ms: number
}

export interface MonitorRequestStats {
  has_samples: boolean
  samples: number
  success: number
  client_errors: number
  server_errors: number
  error_rate_percent: number
  p50_ms: number | null
  p95_ms: number | null
  p99_ms: number | null
  max_ms: number | null
  note?: string
  buckets: MonitorBucket[]
}

export interface MonitorDBPool {
  total_conns: number
  idle_conns: number
  acquired_conns: number
  max_conns: number
  constructing_conns: number
  empty_acquire_count: number
  acquire_duration_ms: number
  utilization_percent: number
  source: string
}

export interface MonitorGoRuntime {
  goroutines: number
  heap_alloc_mb: number
  heap_sys_mb: number
  heap_inuse_mb: number
  gc_runs: number
  gomaxprocs: number
  cpu_percent: number
  uptime_seconds: number
  source: string
  cpu_note: string
}

export interface MonitorEngineProcess {
  configured: boolean
  reachable: boolean
  instances: number
  version: string
  uptime_seconds: number
  memory_mb: number
  cpu_percent: number
  active_tasks: number
  source: string
  cpu_note: string
}

export interface MonitorPerformance extends MonitorSectionMeta {
  window_seconds: number
  window: string
  requests: MonitorRequestStats
  db_pool: MonitorDBPool | null
  go_runtime: MonitorGoRuntime
  engine: MonitorEngineProcess | null
  redis_latency_ms: number | null
  db_latency_ms: number | null
}

export interface MonitorStreamStats {
  name: string
  key: string
  depth: number | null
  pending: number | null
  lag: number | null
  groups: number | null
  consumers: number | null
  dlq_depth: number | null
  oldest_wait_seconds: number | null
  oldest_task_at?: string
  source: string
}

export interface MonitorRunCounts {
  active: number | null
  awaiting_input: number | null
  failed_last_hour: number | null
  timeout_last_hour: number | null
  run_leases: number | null
  source: string
}

export interface MonitorQueue extends MonitorSectionMeta {
  streams: MonitorStreamStats[]
  run_state: MonitorRunCounts | null
}

export interface MonitorRedisCache {
  used_memory_mb: number | null
  max_memory_mb: number | null
  hit_rate_percent: number | null
  keyspace_hits: number | null
  keyspace_misses: number | null
  keys: number | null
  keys_with_ttl: number | null
  avg_ttl_ms: number | null
  evicted_keys: number | null
  expired_keys: number | null
  maxmemory_policy: string
  source: string
}

export interface MonitorLLMCache {
  l1_hits: number
  l2_hits: number
  l3_hits: number
  misses: number
  hit_rate_percent: number
  source: string
}

export interface MonitorCache extends MonitorSectionMeta {
  redis: MonitorRedisCache | null
  llm: MonitorLLMCache | null
}

export interface MonitorOverview {
  generated_at: string
  window_seconds: number
  window: MonitorWindow
  partial: boolean
  performance: MonitorPerformance
  queue: MonitorQueue
  cache: MonitorCache
}

// ── API Keys ──

export async function listApiKeys(): Promise<ApiKey[]> {
  const { data } = await api.get('/v1/admin/api-keys')
  return data.data?.keys || []
}

export async function addApiKey(key: { provider: string; key: string; remark?: string; base_url?: string }): Promise<void> {
  await api.post('/v1/admin/api-keys', key)
}

export async function updateApiKey(id: string, updates: Partial<ApiKey>): Promise<void> {
  await api.put(`/v1/admin/api-keys/${id}`, updates)
}

/**
 * M27：改一把 key 的**分配权重**（smooth WRR 的比例）。
 *
 * 与 status 分开是有意的：`weight = 0` 的语义是"不参与选择但保留状态"（灰度摘流量），
 * 而不是"停用这把 key"（那是 status 的事）。
 */
export async function updateApiKeyWeight(id: string, weight: number) {
  await api.put(`/v1/admin/api-keys/${id}/weight`, { weight })
}

export async function deleteApiKey(id: string): Promise<void> {
  await api.delete(`/v1/admin/api-keys/${id}`)
}

// ── 服务提供商目录 ──

/** 拉取服务提供商目录与各 provider 的配置状态（key 条数 / 生效端点）。 */
export async function listLlmProviders(): Promise<LlmProviderPreset[]> {
  const { data } = await api.get('/v1/admin/llm-providers')
  return data.data?.providers || []
}

/**
 * 保存 provider 的端点覆盖（写 system_settings python 分类的 {id}_base_url）。
 * baseUrl 传空串即删除覆盖、回落目录默认端点。
 */
export async function saveLlmProviderBaseURL(id: string, baseUrl: string): Promise<void> {
  await api.put(`/v1/admin/llm-providers/${encodeURIComponent(id)}`, { base_url: baseUrl })
}

// ── Settings ──

export async function saveSettings(category: string, config: Record<string, unknown>): Promise<void> {
  await api.put('/v1/admin/settings', { category, config })
}

/** 读取已持久化的某类系统设置（rate_limit/degradation/cache/api_key） */
export async function getSettings(category: string): Promise<Record<string, unknown>> {
  const { data } = await api.get(`/v1/admin/settings?category=${encodeURIComponent(category)}`)
  return data?.data?.config ?? {}
}

// ── Agent 运行配置（system_settings 的 agent 分类）──────────────────────
//
// 与 market 同一套理由（见上）：键名是**后端契约**，写错不报错、只是静默不生效。
// 权威消费方是引擎的 `app/agent/runtime_config.py`（订阅 Redis 广播 + 启动补拉），
// 网关只负责存与广播 —— 所以这里只做"读写这一类配置"的薄封装，不做任何校验。
//
// 默认值与引擎侧一致（`python-engine/app/config.py` 的同名字段）：页面在"后台还没有
// 保存过任何值"时必须显示**引擎实际在用的策略**，否则用户看到的是一个从未生效的值。

/** 系统设置分类名（与后端 settingsCategories 逐字一致） */
export const AGENT_SETTINGS_CATEGORY = 'agent'

/** 计划批准策略：never = 直接开工（默认）；always = 第一次写清单前先确认计划 */
export const AGENT_PLAN_APPROVALS = ['never', 'always'] as const
export type AgentPlanApproval = (typeof AGENT_PLAN_APPROVALS)[number]

/** 回合间叙述详略：off = 不注入（默认）；brief = 一句话；detailed = 说清做了什么与下一步 */
export const AGENT_NARRATIONS = ['off', 'brief', 'detailed'] as const
export type AgentNarration = (typeof AGENT_NARRATIONS)[number]

export interface AgentSettingsConfig {
  /** 计划做好后要不要先给用户看 */
  plan_approval: AgentPlanApproval
  /** 等一次人工答复（审批 / 提问）的上限，秒 */
  interaction_wait_seconds: number
  /** 每做完一步要不要说一句在干什么 */
  narration: AgentNarration
  /** 长时间无进展时是否提醒用户（而不只是告诉模型） */
  no_progress_notice: boolean
  /** 判为「无进展」所需的连续相同结果次数；0 = 关闭该规则 */
  no_progress_threshold: number
  /** 自上次有进展以来的累计输入达到模型窗口的这个倍数 ⇒ 暂停并提醒 */
  token_window_ratio: number
}

/** 默认值（与引擎 `agent_*` 字段一致）；后端无记录时页面显示这一份 */
export const AGENT_SETTINGS_DEFAULTS: AgentSettingsConfig = {
  plan_approval: 'never',
  interaction_wait_seconds: 240,
  narration: 'off',
  no_progress_notice: true,
  no_progress_threshold: 3,
  token_window_ratio: 8,
}

function pickChoice<T extends string>(raw: unknown, allowed: readonly T[], fallback: T): T {
  const v = String(raw ?? '').trim().toLowerCase()
  return (allowed as readonly string[]).includes(v) ? (v as T) : fallback
}

function pickNumber(raw: unknown, fallback: number): number {
  const n = Number(raw)
  return Number.isFinite(n) ? n : fallback
}

/**
 * 把后端返回的原始配置归一成完整对象。
 *
 * 后端对未配置项返回 `undefined`/`null`，直接绑到表单上会让 InputNumber 显示空、
 * Switch 变成未勾选 —— 看起来像"引擎关了这项"，而实际是"后台从没保存过"。
 */
export function normalizeAgentSettings(raw: Record<string, unknown>): AgentSettingsConfig {
  const d = AGENT_SETTINGS_DEFAULTS
  return {
    plan_approval: pickChoice(raw.plan_approval, AGENT_PLAN_APPROVALS, d.plan_approval),
    interaction_wait_seconds: pickNumber(raw.interaction_wait_seconds, d.interaction_wait_seconds),
    narration: pickChoice(raw.narration, AGENT_NARRATIONS, d.narration),
    no_progress_notice:
      raw.no_progress_notice === undefined || raw.no_progress_notice === null
        ? d.no_progress_notice
        : Boolean(raw.no_progress_notice),
    no_progress_threshold: pickNumber(raw.no_progress_threshold, d.no_progress_threshold),
    token_window_ratio: pickNumber(raw.token_window_ratio, d.token_window_ratio),
  }
}

export async function getAgentSettings(): Promise<AgentSettingsConfig> {
  return normalizeAgentSettings(await getSettings(AGENT_SETTINGS_CATEGORY))
}

export async function saveAgentSettings(cfg: AgentSettingsConfig): Promise<void> {
  await saveSettings(AGENT_SETTINGS_CATEGORY, { ...cfg })
}

// ── 资源市场「外部源」配置（system_settings 的 market 分类）──────────────
//
// 为什么要这层封装而不是各处直接 getSettings('market')：
//   1) 键名是**后端契约**（remote_enabled / catalog_sources / allow_user_sources /
//      allow_remote_mcp / max_sources）。散落在组件里迟早写错一个字符 —— 而配置键写错
//      **不报错**，只是静默不生效，现象是"配了没反应"，最难排。
//   2) **20 是产品硬上限**，只在这里定义一次：UI 不给可编辑输入框，保存时也恒提交 20，
//      避免任何一处放宽口径。
//   3) 后端对未配置项可能返回 null / 空串 / 非数组，读取侧统一归一，组件里不必到处判空。
//
// HTTP 逻辑复用上面通用的 getSettings / saveSettings（同一端点、同一鉴权），不重复造请求。

/** 系统设置分类名（与后端 settingsCategories 逐字一致） */
export const MARKET_SETTINGS_CATEGORY = 'market'

/** 外部源条数硬上限 —— 产品决策，不接受配置调大 */
export const MAX_MARKET_SOURCES = 20

export interface MarketSettingsConfig {
  /** 外部源总开关；**初装默认关闭** */
  remote_enabled: boolean
  /** 外部目录源地址列表（字符串数组，不是逗号分隔串） */
  catalog_sources: string[]
  /** 允许用户自建源；默认关闭（开启等于把 SSRF 面变成自助入口） */
  allow_user_sources: boolean
  /** 允许外部源提供的 MCP；默认关闭 */
  allow_remote_mcp: boolean
  /** 条数上限，恒为 MAX_MARKET_SOURCES */
  max_sources: number
}

/** 初装/空态：四个开关全关、无源 —— 与产品决策「初次安装完的状态是关闭的」一致 */
export function emptyMarketSettings(): MarketSettingsConfig {
  return {
    remote_enabled: false,
    catalog_sources: [],
    allow_user_sources: false,
    allow_remote_mcp: false,
    max_sources: MAX_MARKET_SOURCES,
  }
}

/**
 * 宽松取布尔：后端可能给 true / "true" / 1。
 * 认不出来一律 false —— 安全开关的未知态必须是"关"，不能是"开"。
 */
function asBool(value: unknown): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') {
    const v = value.trim().toLowerCase()
    return v === 'true' || v === '1' || v === 'yes'
  }
  return false
}

/**
 * 归一 catalog_sources：接受数组，或后端可能以 JSON 字符串形式落库的形态。
 * 元素只保留非空字符串并去重（重复 URL 会在列表里产生重复行，按值删除时会误删）。
 */
export function parseCatalogSources(value: unknown): string[] {
  let raw: unknown = value
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw)
    } catch {
      return []
    }
  }
  if (!Array.isArray(raw)) return []
  const out: string[] = []
  for (const item of raw) {
    if (typeof item !== 'string') continue
    const url = item.trim()
    if (url && !out.includes(url)) out.push(url)
  }
  return out
}

/**
 * 把后端返回的原始 config 归一成面板状态。
 *
 * 刻意**不截断**超过 20 条的存量：静默丢用户数据比显示"超限、需删减"更糟
 * （界面显示「已配置 23 / 20」并禁用「添加」，由用户自己删到 20 以内）。
 */
export function normalizeMarketSettings(raw: Record<string, unknown> | null | undefined): MarketSettingsConfig {
  if (!raw) return emptyMarketSettings()
  return {
    remote_enabled: asBool(raw.remote_enabled),
    catalog_sources: parseCatalogSources(raw.catalog_sources),
    allow_user_sources: asBool(raw.allow_user_sources),
    allow_remote_mcp: asBool(raw.allow_remote_mcp),
    // 上限恒为产品值：忽略后端可能回传的其它数字
    max_sources: MAX_MARKET_SOURCES,
  }
}

/** 读取 market 分类配置（无记录时返回全关的空态） */
export async function getMarketSettings(): Promise<MarketSettingsConfig> {
  return normalizeMarketSettings(await getSettings(MARKET_SETTINGS_CATEGORY))
}

/**
 * 保存 market 分类配置。
 *
 * payload 逐键显式赋值（不用展开）：键名即契约，读代码时一眼能核对，
 * 也避免意外把 UI 内部状态带进请求。max_sources 恒提交 MAX_MARKET_SOURCES。
 */
export async function saveMarketSettings(config: MarketSettingsConfig): Promise<void> {
  await saveSettings(MARKET_SETTINGS_CATEGORY, {
    remote_enabled: config.remote_enabled,
    catalog_sources: [...config.catalog_sources],
    allow_user_sources: config.allow_user_sources,
    allow_remote_mcp: config.allow_remote_mcp,
    max_sources: MAX_MARKET_SOURCES,
  })
}

// ── D1：自诊断（"配了但不生效"的可自答清单）──────────────────────────────

export type DiagnosticStatus = 'ok' | 'warn' | 'fail' | 'skipped'

export interface DiagnosticCheck {
  id: string
  title: string
  /** config / dependency / capability —— 前端据此分组 */
  category: string
  status: DiagnosticStatus
  /** host 观测到的**事实**（不是建议） */
  detail: string
  /** 该怎么办；ok 时为空 */
  remediation?: string
}

export interface DiagnosticsReport {
  generated_at: number
  status: DiagnosticStatus
  counts: Record<string, number>
  checks: DiagnosticCheck[]
}

/** 读取自诊断报告。 */
export async function getDiagnostics(): Promise<DiagnosticsReport> {
  const { data } = await api.get('/v1/admin/diagnostics')
  return data?.data ?? { generated_at: 0, status: 'skipped', counts: {}, checks: [] }
}

// ── P0-1：自诊断聚合清单（schema / 必需表 / 知识库 / 检查点 / 依赖）──────────

/**
 * 聚合清单的状态取值。与 `/diagnostics` 的四值**不是同一套**：
 * 那边用 ok/warn/fail/skipped（体检项），这边用 ok/warn/error/unknown（结论项）。
 * `unknown` 必须单独存在 —— "看不见"与"有问题"的处置正好相反。
 */
export type DiagSummaryStatus = 'ok' | 'warn' | 'error' | 'unknown'

export interface DiagSummaryCheck {
  /** schema / tables / knowledge / checkpoints / deps —— 决定标题与图标 */
  key: string
  /** 后端给的中文标题（缺少 i18n 键时的兜底） */
  title: string
  status: DiagSummaryStatus
  /** 观测到的事实（不是建议） */
  detail: string
  /** 该怎么办；ok 时为空 */
  fix: string
}

export interface DiagSummaryResponse {
  generated_at: number
  status: DiagSummaryStatus
  counts: Record<string, number>
  checks: DiagSummaryCheck[]
}

/**
 * 读取聚合清单。
 *
 * 后端在 DB/引擎不可达时**仍回 200**（各项 status=unknown），所以这里的失败只代表
 * "页面拿不到这份清单"（网络/鉴权），与"清单里有 unknown 项"是两件事。
 */
export async function getDiagnosticsSummary(): Promise<DiagSummaryResponse> {
  const { data } = await api.get('/v1/admin/diagnostics/summary')
  return data?.data ?? { generated_at: 0, status: 'unknown', counts: {}, checks: [] }
}

// ── LLM 熔断：读数与手动恢复 ─────────────────────────────────────────────

/** 单个 provider 的熔断读数。空串表示引擎没报该表，与 'closed' 不同义。 */
export interface GatewayBreaker {
  provider: string
  /** chat 熔断状态：closed / half_open / open */
  chat: string
  /** embedding 熔断状态，与 chat 是独立的两张表 */
  embed: string
  avg_latency_ms: number
}

/**
 * 读取各 provider 的熔断状态。
 *
 * 引擎不可用时后端回 502（不是空列表）—— 空列表会被读成"没有 provider 熔断"，
 * 而实际是"读不到"，两种情况的处置正好相反。
 */
export async function getGatewayBreakers(): Promise<GatewayBreaker[]> {
  const { data } = await api.get('/v1/admin/gateway/breakers')
  return data?.data?.breakers ?? []
}

/** 手动解除某 provider 的熔断（chat 与 embed 两张表一起清）。 */
export async function resetGatewayBreaker(provider: string): Promise<void> {
  await api.post('/v1/admin/gateway/reset-breaker', { provider })
}

// ── D3：用量目录（"花在哪"的构成，区别于配额用量的"还剩多少"）─────────────

export interface UsageBucket {
  bucket: string
  input_tokens: number
  output_tokens: number
  cost_cents: number
  records: number
}

export interface UsageCatalog {
  group_by: string
  from: string
  to: string
  buckets: UsageBucket[]
  total: UsageBucket
  bucket_cnt: number
  note: string
}

/** 按维度（day / model / user）读取用量构成。 */
export async function getUsageCatalog(params: {
  groupBy: 'day' | 'model' | 'user'
  from?: string
  to?: string
}): Promise<UsageCatalog> {
  const { data } = await api.get('/v1/admin/usage/catalog', {
    params: { group_by: params.groupBy, from: params.from, to: params.to },
  })
  return data?.data ?? {
    group_by: params.groupBy, from: '', to: '',
    buckets: [], total: { bucket: '', input_tokens: 0, output_tokens: 0, cost_cents: 0, records: 0 },
    bucket_cnt: 0, note: '',
  }
}

// ── 新用户引导漏斗（P0-3 验收「完成率可查」） ──

export interface OnboardingFunnelStep {
  step: string
  completed: number
  skipped: number
  pending: number
  /** 0–1 的比例，分母是 entered（进入过引导的人数） */
  completion_rate: number
  skip_rate: number
}

export interface OnboardingFunnel {
  /** 进入过引导的人数 —— 唯一正确的分母（不是全站用户数） */
  entered: number
  finished: number
  finished_rate: number
  steps: OnboardingFunnelStep[]
  steps_order: string[]
}

export async function getOnboardingFunnel(): Promise<OnboardingFunnel> {
  const { data } = await api.get('/v1/admin/onboarding/funnel')
  return data?.data ?? { entered: 0, finished: 0, finished_rate: 0, steps: [], steps_order: [] }
}

// ── 分享管理（租户内跨用户） ──

export interface AdminShareItem {
  share_id: string
  session_id: string
  user_id: string
  user_email: string
  title: string
  message_count: number
  created_at: string
  expires_at: string
  /** 服务端产出的绝对分享地址（桌面端 file:// 下 origin 拼出来的是坏链） */
  url?: string
}

/** 列出本租户内全部**有效**分享（已撤销 / 已过期的不会返回） */
export async function listAdminShares(): Promise<AdminShareItem[]> {
  const { data } = await api.get('/v1/admin/shares')
  return data?.data?.items ?? []
}

/** 按分享 id 直接撤销（不必知道会话 id）；公开链接随即 410 */
export async function revokeAdminShare(shareId: string): Promise<void> {
  await api.delete(`/v1/admin/shares/${encodeURIComponent(shareId)}`)
}

// ── 插件命令白名单（后台可编辑） ──

export interface PluginAllowlist {
  /** 生效中的全部命令（env ∪ 后台文件） */
  commands: string[]
  /** 后台文件里的那部分（编辑框回填它） */
  file_commands: string[]
  /** 来自部署环境变量，页面只读 */
  env_commands: string[]
  /** 是否配置了任何来源 */
  configured: boolean
}

export async function getPluginAllowlist(): Promise<PluginAllowlist> {
  const { data } = await api.get('/v1/admin/plugin-allowlist')
  return data?.data ?? { commands: [], file_commands: [], env_commands: [], configured: false }
}

/** 整体替换后台白名单文件（改动即时生效，网关与引擎共用同一份） */
export async function updatePluginAllowlist(commands: string[]): Promise<PluginAllowlist> {
  const { data } = await api.put('/v1/admin/plugin-allowlist', { commands })
  return data?.data ?? { commands: [], file_commands: [], env_commands: [], configured: false }
}

// ── 「服务」页（局域网/广域网访问 + 对外域名）──────────────────────────
//
// 后端：GET/PUT /v1/admin/service（internal/api/admin_service.go）。
// 关键点：**两项都要重启网关才真正生效** —— 监听地址绑在套接字上，对外地址在
// 启动时读入并据此推导 Cookie 的 Secure。因此响应里同时给「生效值」与「已保存值」，
// 页面据此显示"需要重启"，而不是让用户以为改完就生效了。

export interface AdminServiceInfo {
  /** 实际绑定的 host:port */
  listen_addr: string
  port: string
  /** 生效中的绑定地址（0.0.0.0 = 全部网卡） */
  bind_host: string
  /** 生效中的对外地址（空 = 未配置） */
  public_base_url: string
  /** Cookie 是否带 Secure（由对外地址协议推导） */
  cookie_secure: boolean
  /** 已保存的绑定地址与生效值不同 ⇒ 必须重启网关 */
  restart_required: boolean
  saved_bind_host: string
  saved_public_base_url: string
  /** 本机非回环 IPv4 地址：告诉同事"用这个访问"时的来源 */
  local_addresses: string[]
  /** 这组配置的后果提示（空 = 无） */
  warning: string
}

export async function getServiceInfo(): Promise<AdminServiceInfo> {
  const { data } = await api.get('/v1/admin/service')
  return data?.data
}

/** 保存服务配置；返回保存后的事实（含 restart_required / warning）。 */
export async function saveServiceInfo(payload: {
  bind_host?: string
  public_base_url?: string
}): Promise<AdminServiceInfo> {
  const { data } = await api.put('/v1/admin/service', payload)
  return data?.data
}

// ── 「知识库」页（本地模式 ↔ 外部 pageindex 服务）────────────────────────
//
// 后端：internal/api/admin_knowledge.go。
// 关键点：**这一页保存即生效**（引擎订阅广播后热切地址），但"保存成功"不等于
// "引擎已采纳" —— 广播可能丢失、引擎可能没起。所以响应里同时给 saved 与
// runtime 两组事实，`pending_restart` 表示两者已经分叉，页面据此提示，而不是
// 假装已经生效。

export interface AdminKnowledgeInfo {
  /** 库里已持久化的配置（下次启动 / 广播会用它） */
  saved: { mode?: string; service_url?: string } & Record<string, unknown>
  /** 引擎当前生效的运行时事实（未启用 / 不可达时为 null） */
  runtime: {
    mode?: string
    service_url?: string
    bootstrap_url?: string
    using_bootstrap?: boolean
    probe?: { ok: boolean; status_code: number; detail: string }
  } | null
  /** 引擎是否给出了运行时事实（false 不代表配置错，只是读不到） */
  engine_available: boolean
  /** 保存值与引擎生效值已分叉（引擎还没采纳） */
  pending_restart: boolean
}

export async function getKnowledgeInfo(): Promise<AdminKnowledgeInfo> {
  const { data } = await api.get('/v1/admin/knowledge')
  return data?.data
}

export async function saveKnowledgeInfo(payload: {
  mode?: 'local' | 'remote'
  service_url?: string
}): Promise<AdminKnowledgeInfo> {
  const { data } = await api.put('/v1/admin/knowledge', payload)
  return data?.data
}

/** 让**引擎**去探一个候选地址（空串 = 探当前生效的）。响应恒 200，连不上是业务结果。 */
export async function testKnowledgeService(serviceUrl: string): Promise<{
  ok: boolean
  engine_available: boolean
  detail: string
  status_code?: number
  target?: string
}> {
  const { data } = await api.post('/v1/admin/knowledge/test', { service_url: serviceUrl })
  return data?.data
}
