/**
 * 知识库**引用可定位**（P1-1）的纯逻辑：RAG 命中块 → 可点击目标。
 *
 * 背景：对话里此前只有一句"引用了知识库（×N）"的汇总标签，用户点不进去，
 * 也没法核对"这句话出自哪篇文档的第几页"。引擎现在在 assistant metadata 里给出
 * `kb_citations`（`kb_id` / `doc_id` / `doc_name` / `page`），这里负责把它变成
 * 一个**稳定的路由目标**：
 *
 *     /knowledge/:kbId?doc=:docId
 *
 * `KnowledgeDetailView` 读 `?doc=` 定位并高亮该文档行。抽成纯函数（而非写在视图里）
 * 是为了能被 vitest 直接钉住 —— 链接拼错的表现是"点了跳到知识库列表/404"，
 * 靠人工点一遍非常容易漏。
 */

import { t } from '../i18n'

/** 引擎写入 metadata.kb_citations 的条目形状（字段可能缺失，见 normalize）。 */
export interface KbCitation {
  kb_id: string
  doc_id: string
  doc_name: string
  page: number | null
  block_id: string
  snippet: string
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function asPage(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.floor(value) : null
}

/**
 * 归一化任意来源的引用数组。
 *
 * 用宽松解析而不是直接断言类型：`metadata` 来自后端 JSON（历史会话里可能是旧形状，
 * Go 网关链路则完全没有），渲染层不该因为一条脏数据整块炸掉。
 * 既没有 `kb_id` 也没有 `doc_id` 的条目**保留**（仍可展示文档名与页码），
 * 只是不可点击 —— 丢掉它们等于把答案的来源整段藏起来。
 */
export function normalizeKbCitations(raw: unknown): KbCitation[] {
  if (!Array.isArray(raw)) return []
  const out: KbCitation[] = []
  const seen = new Set<string>()
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const citation: KbCitation = {
      kb_id: asString(record.kb_id),
      doc_id: asString(record.doc_id),
      doc_name: asString(record.doc_name) || asString(record.name),
      page: asPage(record.page),
      block_id: asString(record.block_id),
      snippet: asString(record.snippet),
    }
    if (!citation.kb_id && !citation.doc_id && !citation.doc_name) continue
    // 同一份文档的同一块在多库检索里可能重复出现（引擎已去重，这里兜底）
    const key = `${citation.kb_id}|${citation.doc_id}|${citation.doc_name}|${citation.page ?? ''}|${citation.block_id}|${citation.snippet.slice(0, 80)}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(citation)
  }
  return out
}

/**
 * 引用 → 知识库详情路由（带 `?doc=` 定位参数）。
 *
 * - 有 `kb_id`：`/knowledge/<kbId>`，有 `doc_id` 时再带 `?doc=<docId>`；
 * - **没有** `kb_id`：返回空串（调用方渲染成纯文本）。此时拼一个只有 `?doc=` 的链接
 *   没有任何意义 —— 不知道去哪个库找那篇文档，点进去必然落空。
 */
export function buildKbCitationLink(citation: Partial<KbCitation> | null | undefined): string {
  const kbId = asString(citation?.kb_id)
  if (!kbId) return ''
  const base = `/knowledge/${encodeURIComponent(kbId)}`
  const docId = asString(citation?.doc_id)
  if (!docId) return base
  const params = new URLSearchParams({ doc: docId })
  // 页码不是文档身份的一部分，但它是"回到原文位置"的唯一坐标；带上它，
  // 详情页可以提示"第 N 页"（没有阅读器时至少不会丢掉这个信息）。
  const page = asPage(citation?.page)
  if (page) params.set('page', String(page))
  return `${base}?${params.toString()}`

}

/**
 * 引用的展示文案：`文档名 · 第 3 页`（缺文档名时用"文档"，缺页码时省略）。
 *
 * 文案与引擎注入提示词里的标签口径一致（`[库名 · 文件名 第 N 页]`），
 * 这样模型在回答里说的"第几页"和界面上能看到的是同一个数字。
 */
export function kbCitationLabel(citation: Partial<KbCitation> | null | undefined): string {
  const name = asString(citation?.doc_name) || ''
  const page = asPage(citation?.page)
  const base = name || t('knowledge.document')
  return page ? t('knowledge.citation_doc_page', { doc: base, page }) : base
}

/** 渲染层用的引用条目：`to` 为空 = 不可点（调用方渲染成纯文本）。 */
export interface KbCitationView {
  key: string
  label: string
  to: string
  snippet: string
}

/**
 * 引用数组 → 渲染条目（`MessageItem` 与分享页共用，P1-8）。
 *
 * `linkable: false` 用于**分享页**：公开链接没有库权限，跳 `/knowledge/:kbId`
 * 只会把访客送进一个 404/登录墙。此时 `to` 恒为空，调用方按纯文本渲染 ——
 * 信息（文档名 · 页码）保留，点击能力去掉。这条规则必须**共用同一个函数**，
 * 否则两条渲染路径迟早分叉成"对话里能点、分享页也能点"。
 */
export function kbCitationViews(
  raw: unknown,
  options: { linkable?: boolean } = {},
): KbCitationView[] {
  const linkable = options.linkable !== false
  return normalizeKbCitations(raw).map((citation, index) => ({
    key: `${citation.kb_id}-${citation.doc_id}-${index}`,
    label: kbCitationLabel(citation),
    to: linkable ? buildKbCitationLink(citation) : '',
    snippet: citation.snippet,
  }))
}

/**
 * 站内知识库路由前缀。分享页要把这类链接降级为纯文本（见 `kbCitationViews`）。
 *
 * 只匹配**站内相对路径**：绝对外链（`https://…`）与 `#` 锚点不受影响，
 * 否则分享页的正常外链也会被降级。
 */
const INTERNAL_KB_LINK_PREFIXES = ['/knowledge', '/chat', '/agents', '/kb/']

export function isInternalAppLink(href: string): boolean {
  const value = asString(href)
  if (!value || !value.startsWith('/')) return false
  return INTERNAL_KB_LINK_PREFIXES.some(prefix => value === prefix || value.startsWith(`${prefix}/`) || value.startsWith(`${prefix}?`))
}
