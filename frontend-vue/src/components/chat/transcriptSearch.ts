/**
 * 会话内文本检索（纯逻辑，无 DOM）。
 *
 * 只搜正文与思考：工具参数/结果是机器输出，体量大且形态各异，把它们掺进结果会让
 * 「我刚说过什么」被噪声淹没 —— 需要查工具输出时用全局搜索（/v1/search）。
 */

import type { ChatItem } from './chat-types'

export interface SearchOptions {
  /** 默认不区分大小写 */
  caseSensitive?: boolean
}

/** 返回命中的 item 下标，按出现顺序 */
export function findMatches(
  items: readonly ChatItem[],
  query: string,
  options: SearchOptions = {},
): number[] {
  const raw = query.trim()
  if (!raw) return []
  const needle = options.caseSensitive ? raw : raw.toLowerCase()
  const hits: number[] = []
  for (let i = 0; i < items.length; i++) {
    const text = searchableText(items[i])
    if (!text) continue
    const haystack = options.caseSensitive ? text : text.toLowerCase()
    if (haystack.includes(needle)) hits.push(i)
  }
  return hits
}

/** 可检索文本：正文与思考 */
export function searchableText(item: ChatItem | undefined): string {
  if (!item) return ''
  if (item.kind === 'text' || item.kind === 'reasoning') return item.content || ''
  return ''
}

/** 命中片段（前后各留一点上下文），供结果列表显示 */
export function matchExcerpt(text: string, query: string, radius = 24): string {
  const raw = query.trim()
  if (!raw) return ''
  const at = text.toLowerCase().indexOf(raw.toLowerCase())
  if (at < 0) return collapse(text.slice(0, radius * 2))
  const start = Math.max(0, at - radius)
  const end = Math.min(text.length, at + raw.length + radius)
  const lead = start > 0 ? '…' : ''
  const trail = end < text.length ? '…' : ''
  return `${lead}${collapse(text.slice(start, end))}${trail}`
}

function collapse(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}
