/**
 * 知识库 API 客户端（PageIndex 版）。
 *
 * 与旧实现的三点差异都体现在类型上：
 * 1. `provider`（selfhosted | cloud）取代了 `type`（wiki | rag）；
 * 2. 文档带 `pageindex_doc_id` / `page_count`，不再有 `chunk_count`；
 * 3. 查询返回**答案 + 引用**，不是 top-k 片段。
 *
 * 所有知识库调用集中在这里：视图里不再出现裸 `api.get('/v1/kb/...')`，
 * 契约变化时只有一个地方要改。
 */
import { api } from './index'

export type ProviderName = 'selfhosted' | 'cloud'
export type KbStatus = 'disabled' | 'indexing' | 'ready' | 'error'
export type KbVisibility = 'public' | 'private' | 'tenant'

/**
 * 索引状态的判据集合 —— 逐项镜像引擎侧 `python-engine/app/knowledge/status.py`。
 *
 * 这里**必须**是唯一一份前端判据：此前详情页把"构建完成"写成 `status === 'active'`、
 * 把"正在构建"写成 `'building'`，而这两个取值**从未被任何写入方写过**（worker 只写
 * `indexing` / `ready` / `error`）。后果是索引真跑完、库早已 `ready`，界面仍轮询到超时
 * 后默默放弃；"构建中"的按钮分支也永远不生效。
 */
export const KB_BUILDING_STATUSES: readonly KbStatus[] = ['indexing']
export const KB_TERMINAL_STATUSES: readonly KbStatus[] = ['ready', 'error']

/** 是否正在索引（读的是**库上的 status**，不是本地 loading 标志）。 */
export function isKbBuilding(status?: string | null): boolean {
  return KB_BUILDING_STATUSES.includes(status as KbStatus)
}

/** 是否已到终态（`error` 也算终态：继续轮询一个失败的库只会让用户干等）。 */
export function isKbTerminal(status?: string | null): boolean {
  return KB_TERMINAL_STATUSES.includes(status as KbStatus)
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  provider: ProviderName
  visibility: KbVisibility
  status: KbStatus
  /**
   * 用户开关：这个库是否参与检索与索引。
   *
   * 与 `status` **正交**（见 `migrations/versions/0006_kb_enabled.py`）：
   * `status` 说索引跑到哪一步，`enabled` 说用户让不让用。停用不销毁索引，
   * 重新启用不需要重建。
   */
  enabled: boolean
  document_count: number
  total_size_bytes: number
  credits_consumed: number
  error_message: string
  /**
   * 逐库模型覆盖（P1-6）：空串 = 沿用网关「模型服务」里的默认模型。
   *
   * 存的是 `knowledge_bases.config` 里的显式值，**不含默认值副本** ——
   * 所以管理员在 /models 改了默认模型，所有没覆盖过的库立刻跟着变。
   */
  index_model?: string
  chat_model?: string
  created_at: string
  updated_at: string
}

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  name: string
  file_type: string
  file_size_bytes: number
  page_count: number
  status: 'pending' | 'indexing' | 'ready' | 'error'
  pageindex_doc_id: string
  error_message: string
  created_at: string
  updated_at: string
}

export interface Citation {
  doc_name: string
  page: number | null
  block_id: string
  snippet: string
  /**
   * 可定位坐标（P1-1）：文档 id 由引擎从文件名前缀反解，缺省时为空串
   * （PageIndex 改写过文件名的情况），此时引用只展示文档名与页码。
   */
  doc_id?: string
  /** 该引用所属知识库；多库对话时用于把引用点回正确的库。 */
  kb_id?: string
}

export interface KnowledgeAnswer {
  provider: string
  answer: string
  citations: Citation[]
}

export interface ProviderOption {
  name: ProviderName
  label: string
  available: boolean
  status: string
  detail: string
}

export interface KnowledgeSettings {
  provider: ProviderName | ''
  /** 是否**显式保存过** provider（false 且 provider 非空 = 走内置服务的默认值） */
  provider_selected?: boolean
  default_provider?: ProviderName | ''
  status: string
  error_message: string
  cloud_key_configured: boolean
  /** 内置检索服务（pageindex-service）的实际地址，未部署时为空 */
  service_url?: string
  /** 内置服务的配置是否齐备（地址 + 内部令牌） */
  service_ready?: boolean
  providers: ProviderOption[]
}

export interface VerifyResult {
  ok: boolean
  status: string
  detail: string
}

/** 网关会把引擎响应包一层 `{data: ...}`；两种形状都兼容。 */
function unwrap<T>(resp: { data?: unknown }): T {
  const outer = resp?.data as { data?: unknown } | undefined
  return (outer && typeof outer === 'object' && 'data' in outer ? outer.data : outer) as T
}

// ── 知识库 ──

export async function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  const resp = await api.get('/v1/kb')
  const body = unwrap<{ knowledge_bases?: KnowledgeBase[] }>(resp)
  return body?.knowledge_bases ?? []
}

export async function createKnowledgeBase(body: {
  name: string
  description?: string
  provider?: ProviderName
  visibility?: KbVisibility
}): Promise<KnowledgeBase> {
  const resp = await api.post('/v1/kb', body)
  return unwrap<KnowledgeBase>(resp)
}

export async function getKnowledgeBase(id: string): Promise<KnowledgeBase> {
  const resp = await api.get(`/v1/kb/${id}`)
  return unwrap<KnowledgeBase>(resp)
}

export async function updateKnowledgeBase(
  id: string,
  body: {
    name?: string
    description?: string
    visibility?: KbVisibility
    /** 逐库模型覆盖（P1-6）：空串 = 清除覆盖、回落到默认（**不是**"保存空模型"） */
    index_model?: string
    chat_model?: string
  },
): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}`, body)
  return unwrap<unknown>(resp)
}

// ── 检索观测（P1-6） ──

/** 一个时间窗口内的检索计数（引擎按小时内桶聚合，见 app/knowledge/metrics.py）。 */
export interface KbStatsWindow {
  queries: number
  /** 命中数（有答案或有引用的库数） */
  hits: number
  /** 检索过但 0 命中的次数 */
  zero_hit: number
  errors: number
  avg_latency_ms: number
  /** 零命中率：分母是**成功**的检索数（失败另计，不混进去） */
  zero_hit_rate: number
}

export interface KbStatsFailure {
  /** 稳定分类（disabled / not_found / provider_unavailable / service_error / timeout / unknown） */
  category: string
  /** 截断后的失败消息（不会包含用户问题原文） */
  message: string
  count: number
}

export interface KbStats {
  kb_id: string
  window_hours_24: KbStatsWindow
  window_days_7: KbStatsWindow
  recent_failures: KbStatsFailure[]
  /** 引擎算好的可读结论（别在前端再解释一遍数字） */
  diagnosis: string[]
  /** false = Redis 不可读，数字全 0 **不代表**"没检索过" */
  available: boolean
  ttl_days?: number
  mine?: { window_hours_24: KbStatsWindow; window_days_7: KbStatsWindow }
}

/**
 * 检索观测摘要：回答"为什么没检索到"。
 *
 * 网关同时转 `/v1/kb/{id}/stats`（既有前缀）与 `/v1/knowledge/{id}/stats`（对外文档名），
 * 这里用前者，与其余 kb 接口保持一致。
 */
export async function getKnowledgeStats(kbId: string): Promise<KbStats> {
  const resp = await api.get(`/v1/kb/${kbId}/stats`)
  return unwrap<KbStats>(resp)
}

export async function deleteKnowledgeBase(id: string): Promise<unknown> {
  const resp = await api.delete(`/v1/kb/${id}`)
  return unwrap<unknown>(resp)
}

export async function setKnowledgeBaseVisibility(
  id: string,
  visibility: KbVisibility,
): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}/visibility`, { visibility })
  return unwrap<unknown>(resp)
}

/**
 * 启用 / 停用知识库（`enabled`，与索引状态 `status` 无关）。
 *
 * 停用**不删索引**：重新启用就能直接检索，不重复花索引费用。停用后该库不再参与
 * 检索，也不能投递构建任务（后端会回 409）。
 */
export async function setKnowledgeBaseEnabled(
  id: string,
  enabled: boolean,
): Promise<unknown> {
  const resp = await api.put(`/v1/kb/${id}/status`, { enabled })
  return unwrap<unknown>(resp)
}

// ── 文档 ──

export async function listDocuments(kbId: string): Promise<KnowledgeDocument[]> {
  const resp = await api.get(`/v1/kb/${kbId}/documents`)
  const body = unwrap<{ documents?: KnowledgeDocument[] }>(resp)
  return body?.documents ?? []
}

export async function uploadDocument(
  kbId: string,
  body: { name: string; file_type?: string; file_size_bytes?: number; content: string },
): Promise<KnowledgeDocument> {
  const resp = await api.post(`/v1/kb/${kbId}/documents`, body)
  return unwrap<KnowledgeDocument>(resp)
}

export async function deleteDocument(kbId: string, docId: string): Promise<unknown> {
  const resp = await api.delete(`/v1/kb/${kbId}/documents`, { params: { doc_id: docId } })
  return unwrap<unknown>(resp)
}

export async function previewDocument(
  kbId: string,
  docId: string,
): Promise<{ data?: KnowledgeDocument & { content?: string } }> {
  const resp = await api.get(`/v1/kb/${kbId}/documents/${docId}/preview`)
  return unwrap<{ data?: KnowledgeDocument & { content?: string } }>(resp) ?? {}
}

// ── 索引与问答 ──

export async function buildKnowledgeBase(
  kbId: string,
): Promise<{ status: string; task_id: string; provider: string }> {
  const resp = await api.post(`/v1/kb/${kbId}/build`, {})
  return unwrap<{ status: string; task_id: string; provider: string }>(resp)
}

export async function queryKnowledgeBase(kbId: string, query: string): Promise<KnowledgeAnswer> {
  const resp = await api.post(`/v1/kb/${kbId}/query`, { query })
  return unwrap<KnowledgeAnswer>(resp)
}

// ── 索引进度与重建（P1-1） ──

/** 单篇文档的索引进度条目（GET /v1/kb/{id}/status 的 documents[]）。 */
export interface KbIndexDocument {
  id: string
  name: string
  status: 'pending' | 'indexing' | 'ready' | 'error'
  page_count: number
  error_message: string
  file_size_bytes: number
}

/**
 * 库级索引进度。
 *
 * 由引擎按**文档状态聚合**（pageindex-service 没有 per-job 端点可透传，见
 * `python-engine/app/knowledge/progress.py`）。因此 `percent` / `active` 都是服务端
 * 算好的权威值 —— 前端**不要**再自己数文档，否则两边口径一漂就出现"进度条 80%、
 * 列表 6/10"。
 */
export interface KbIndexStatus {
  kb_id: string
  status: KbStatus
  /** 完成百分比：`ready + error` / 总数（失败也算处理完，否则进度永远停在半路）。 */
  percent: number
  /** 是否仍在索引 —— 前端据此决定要不要继续轮询。 */
  active: boolean
  total: number
  indexed: number
  failed: number
  pending: number
  indexing: number
  error_message: string
  documents: KbIndexDocument[]
  failed_documents: KbIndexDocument[]
}

/** 重建范围：只重试失败文档 / 全库重建。 */
export type KbReindexScope = 'failed' | 'all'

export interface KbReindexResult {
  status: string
  enqueued: boolean
  scope: KbReindexScope
  /** 已在索引中 / 没有失败文档时为 false 并给出 reason（幂等，不算失败）。 */
  reason?: string
  count: number
  task_id: string
}

/** 索引进度（只读）。 */
export async function getKnowledgeIndexStatus(kbId: string): Promise<KbIndexStatus> {
  const resp = await api.get(`/v1/kb/${kbId}/status`)
  return unwrap<KbIndexStatus>(resp)
}

/**
 * 重新提交索引：`failed`（默认）只重试失败文档，`all` 全库重建。
 *
 * 幂等：库正在索引时后端直接回当前状态（`enqueued: false`），不会重复花 LLM 费用。
 */
export async function reindexKnowledgeBase(
  kbId: string,
  scope: KbReindexScope = 'failed',
): Promise<KbReindexResult> {
  const resp = await api.post(`/v1/kb/${kbId}/reindex`, { scope })
  return unwrap<KbReindexResult>(resp)
}

// ── 用户级设置 ──

export async function getKnowledgeSettings(): Promise<KnowledgeSettings> {
  const resp = await api.get('/v1/kb/settings')
  return unwrap<KnowledgeSettings>(resp)
}

export async function putKnowledgeSettings(body: {
  provider: ProviderName
  cloud_api_key?: string
}): Promise<{ provider: string; status: string; cloud_key_configured: boolean }> {
  const resp = await api.put('/v1/kb/settings', body)
  return unwrap<{ provider: string; status: string; cloud_key_configured: boolean }>(resp)
}

export async function verifyKnowledgeSettings(): Promise<VerifyResult> {
  const resp = await api.post('/v1/kb/settings/verify', {})
  return unwrap<VerifyResult>(resp)
}
