/**
 * 资源市场 API 客户端（`/v1/market/*`）。
 *
 * 三条设计口径，都体现在类型与函数签名上：
 *
 * 1) **来源是第一等概念**。`(source, id)` 才是条目主键 —— 同名条目在不同源是不同东西，
 *    所以每个读写端点都要求显式带 `source`，没有任何"默认源"的隐式行为。
 * 2) **外部源的失败不是整体失败**。目录响应把 `sources`（含失败/停用的源）与 `warnings`
 *    （非致命问题）与 `items` 一起返回，页面照常渲染健康的部分；`warnings[].reason`
 *    是**稳定机器码**，前端本地化，绝不把英文异常串甩给用户。
 * 3) **错误要能照着 i18n 说人话**。403（部署不允许自建源）与 409（源数量上限 / 条目已装）
 *    各有专门文案，不用后端原始 message —— 见 `describeMarketError`。
 */
import { api } from './index'
import { t } from '../i18n'
import { errorStatus } from '../utils/apiError'

// ── 类型 ──

/** 条目类型。值与后端 `market.Kind` 一一对应（单数形式）。 */
export type MarketKind = 'agent' | 'skill' | 'knowledge' | 'mcp'

/** 合法类型的**顺序即界面 Tab 顺序**（与后端 `market.Kinds` 保持一致）。 */
export const MARKET_KINDS: readonly MarketKind[] = ['agent', 'skill', 'knowledge', 'mcp']

/** 内置源的固定 id：永远存在、不需要联网、排第一个。 */
export const BUILTIN_SOURCE_ID = 'builtin'

/** 源的信任级别：内置随二进制发布，运维由部署方配置，用户自行添加。 */
export type MarketSourceType = 'builtin' | 'operator' | 'user'

/** 安装前可读的前提。**只有键名，绝不含凭据值**（值由用户在表单里填、只存内存）。 */
export interface MarketRequires {
  /** 需要用户提供的环境变量**键名**（如 `GITHUB_TOKEN`）。 */
  env?: string[]
  /** 该条目需要可解析的模型才能运行。 */
  model?: boolean
  /** 装完仍需在插件页授权才能真正执行。 */
  approval?: boolean
}

export interface MarketItem {
  kind: MarketKind
  id: string
  name: string
  description?: string
  version?: string
  author?: string
  tags?: string[]
  homepage?: string
  requires?: MarketRequires
  /** 条目所属源 id（`builtin` 或外部源），由后端加载层填写。 */
  source: string
  /** 是否带发布方签名。`false` 只做提醒，不阻止安装。 */
  signed: boolean
  /** 条目来自上次成功快照（本次抓取失败）。 */
  stale?: boolean
  /** 外部条目的内容来源；内置条目为空（内容在 `payload` 里）。 */
  manifest_url?: string
  payload?: unknown
}

export interface MarketSource {
  id: string
  name: string
  url?: string
  type: MarketSourceType
  /** `false` 表示该源存在但当前不参与拉取。 */
  enabled: boolean
  last_ok_at?: string
  last_error?: string
  stale?: boolean
  /** 该源参与展示的条目数。 */
  count: number
}

/** 非致命告警。`reason` 是稳定机器码，前端用 `market.warn.<reason>` 本地化。 */
export interface MarketWarning {
  source: string
  reason: string
  /** 给运维看的原始信息（可能含 URL / 状态码），不直接展示给用户。 */
  detail?: string
}

export interface MarketCatalog {
  items: MarketItem[]
  total: number
  page: number
  page_size: number
  /** **全部**来源（含本轮失败/被停用的），页面据此渲染源 Tab 与告警。 */
  sources: MarketSource[]
  warnings: MarketWarning[]
}

/** 部署级能力开关（决定"能不能加源"，不是"有没有源"）。 */
export interface MarketLimits {
  remote_enabled: boolean
  allow_user_sources: boolean
  allow_remote_mcp: boolean
  max_sources: number
}

export const EMPTY_MARKET_LIMITS: MarketLimits = {
  remote_enabled: false,
  allow_user_sources: false,
  allow_remote_mcp: false,
  max_sources: 0,
}

export interface MarketSourcesResponse {
  sources: MarketSource[]
  limits: MarketLimits
}

export interface MarketItemDetail {
  item: MarketItem
  warnings: MarketWarning[]
}

export interface MarketCatalogParams {
  kind?: MarketKind
  q?: string
  tag?: string
  source?: string
  page?: number
  page_size?: number
}

export interface MarketInstallBody {
  source: string
  kind: MarketKind
  id: string
  /** 用户填写的环境变量。**只随本次请求提交**，前端不做任何持久化。 */
  env?: Record<string, string>
}

export interface MarketInstallResult {
  installed: boolean
  source: string
  kind: MarketKind
  id: string
  ref?: Record<string, unknown>
  /** 之前已装过：不是错误，但界面要说明"无需重复安装"。 */
  already_installed?: boolean
  /** 装完是否已启用。外部源条目为 `false` —— 必须在对应页面手动启用。 */
  enabled: boolean
  /** 是否仍需在插件页授权。 */
  requires_approval: boolean
}

export interface MarketInstalledItem {
  source: string
  kind: MarketKind
  id: string
  ref?: Record<string, unknown>
  installed_at?: string
  enabled: boolean
}

const DEFAULT_PAGE_SIZE = 20

// ── 目录 / 条目 ──

export async function listMarketCatalog(params: MarketCatalogParams = {}): Promise<MarketCatalog> {
  const { data } = await api.get('/v1/market/catalog', {
    params: {
      // 空值一律不传：后端把 `kind=""` 当"未指定"，但显式发空串会让 URL 里多出
      // `kind=`，排查问题时看着像前端搞错了类型。
      kind: params.kind || undefined,
      q: params.q || undefined,
      tag: params.tag || undefined,
      source: params.source || undefined,
      page: params.page,
      page_size: params.page_size,
    },
  })
  const body = (data?.data ?? {}) as Partial<MarketCatalog>
  return {
    items: body.items ?? [],
    total: body.total ?? 0,
    page: body.page ?? 1,
    page_size: body.page_size ?? DEFAULT_PAGE_SIZE,
    sources: body.sources ?? [],
    warnings: body.warnings ?? [],
  }
}

export async function getMarketItem(source: string, id: string): Promise<MarketItemDetail> {
  const { data } = await api.get(
    `/v1/market/items/${encodeURIComponent(source)}/${encodeURIComponent(id)}`,
  )
  const body = (data?.data ?? {}) as Partial<MarketItemDetail>
  if (!body.item) {
    // 200 却没有 item 只可能是契约漂移；宁可抛错让页面显示"加载失败"，
    // 也不要让抽屉渲染一个空壳。
    throw new Error('market item missing in response')
  }
  return { item: body.item, warnings: body.warnings ?? [] }
}

// ── 源管理 ──

export async function listMarketSources(): Promise<MarketSourcesResponse> {
  const { data } = await api.get('/v1/market/sources')
  const body = (data?.data ?? {}) as Partial<MarketSourcesResponse>
  return {
    sources: body.sources ?? [],
    limits: body.limits ?? EMPTY_MARKET_LIMITS,
  }
}

/** 新增外部源。403 = 部署不允许自建源；409 = 已达源数量上限。 */
export async function createMarketSource(body: { url: string; name?: string }): Promise<MarketSource | undefined> {
  const { data } = await api.post('/v1/market/sources', body)
  return (data?.data?.source ?? data?.data) as MarketSource | undefined
}

export async function deleteMarketSource(id: string): Promise<void> {
  await api.delete(`/v1/market/sources/${encodeURIComponent(id)}`)
}

// ── 安装 / 卸载 ──

export async function installMarketItem(body: MarketInstallBody): Promise<MarketInstallResult> {
  // 只提交**非空**的 env：空字符串会把"用户没填"写成"用户填了空值"，
  // 后端校验时报错反而更难懂。
  const env: Record<string, string> = {}
  for (const [k, v] of Object.entries(body.env ?? {})) {
    const trimmed = (v ?? '').trim()
    if (trimmed) env[k] = trimmed
  }
  const { data } = await api.post('/v1/market/install', {
    source: body.source,
    kind: body.kind,
    id: body.id,
    ...(Object.keys(env).length ? { env } : {}),
  })
  const body2 = (data?.data ?? {}) as Partial<MarketInstallResult>
  return {
    installed: body2.installed ?? true,
    source: body2.source ?? body.source,
    kind: body2.kind ?? body.kind,
    id: body2.id ?? body.id,
    ref: body2.ref,
    already_installed: body2.already_installed,
    // 字段缺失时按"已启用"处理：内置源装完即可用，只有明确 `false` 才提示去启用。
    enabled: body2.enabled ?? true,
    requires_approval: body2.requires_approval ?? false,
  }
}

export async function uninstallMarketItem(
  source: string,
  kind: MarketKind,
  id: string,
): Promise<void> {
  await api.delete(
    `/v1/market/install/${encodeURIComponent(source)}/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
  )
}

export async function listInstalledMarketItems(kind?: MarketKind): Promise<MarketInstalledItem[]> {
  const { data } = await api.get('/v1/market/install', {
    params: { kind: kind || undefined },
  })
  return ((data?.data?.items ?? []) as MarketInstalledItem[])
}

// ── 辅助 ──

/** 条目/安装记录的稳定键：`source/kind/id`（同名不同源必须区分）。 */
export function marketItemKey(v: { source: string; kind: MarketKind; id: string }): string {
  return `${v.source}/${v.kind}/${v.id}`
}

/**
 * 源 Tab 的排序：**内置源恒排第一**，其余按后端顺序（即运维配置顺序）。
 *
 * 排序放在前端而不是依赖后端返回顺序：`builtin` 排第一是产品口径（它永远可用、
 * 不需要联网），任何后端调整都不该让用户先看到一堆坏掉的外部源。
 */
export function sortSourcesWithBuiltinFirst(sources: MarketSource[]): MarketSource[] {
  const builtin = sources.filter(s => s.id === BUILTIN_SOURCE_ID)
  const rest = sources.filter(s => s.id !== BUILTIN_SOURCE_ID)
  return [...builtin, ...rest]
}

/** 告警条的去重键（同一源同一原因只显示一条）。 */
export function marketWarningKey(w: MarketWarning): string {
  return `${w.source}|${w.reason}`
}

export type MarketErrorOp = 'catalog' | 'install' | 'uninstall' | 'source_add' | 'source_remove'

/**
 * 把市场请求失败翻译成**用户能看懂**的本地化文案。
 *
 * 为什么不直接用 `describeApiError`：市场有几个与业务强绑定的状态码语义 ——
 * `403` 是"这个部署不允许自建源"（不是权限不足），`409` 在**加源**时是"数量已达上限"
 * 而在**安装**时是"已经装过了"。通用映射会把这些都说成"请求失败"，用户无从处置。
 */
export function describeMarketError(error: unknown, op: MarketErrorOp = 'catalog'): string {
  const status = errorStatus(error)
  if (status === 403 && (op === 'source_add' || op === 'source_remove')) {
    return t('market.error.user_sources_forbidden')
  }
  if (status === 409) {
    return op === 'source_add'
      ? t('market.error.source_limit_reached')
      : t('market.error.conflict')
  }
  return t('market.error.request_failed')
}
