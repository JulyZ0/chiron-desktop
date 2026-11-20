/**
 * 知识库引用可定位（P1-1）的用例。
 *
 * 钉住三件事：
 * 1. 链接形状必须是 `/knowledge/:kbId?doc=:docId`（KnowledgeDetailView 只认这个）；
 * 2. 缺 `kb_id` 时**不**生成链接（不能凭空猜一个库）；
 * 3. 脏数据不得让渲染层炸掉（老会话的 metadata 形状与现在不同）。
 */
import { describe, expect, it } from 'vitest'
import {
  buildKbCitationLink,
  kbCitationLabel,
  normalizeKbCitations,
} from '../kbCitation'

describe('buildKbCitationLink', () => {
  it('kb + doc → 带 ?doc= 的详情路由', () => {
    expect(buildKbCitationLink({ kb_id: 'kb-1', doc_id: 'doc-9' })).toBe(
      '/knowledge/kb-1?doc=doc-9',
    )
  })

  it('只有 kb → 无 ?doc（仍可回到该库）', () => {
    expect(buildKbCitationLink({ kb_id: 'kb-1' })).toBe('/knowledge/kb-1')
  })

  it('缺 kb_id → 空串（调用方降级为纯文本）', () => {
    expect(buildKbCitationLink({ doc_id: 'doc-9' })).toBe('')
    expect(buildKbCitationLink(null)).toBe('')
    expect(buildKbCitationLink(undefined)).toBe('')
  })

  it('对 id 做 URL 编码（uuid 之外的脏 id 不撑破路由）', () => {
    expect(buildKbCitationLink({ kb_id: 'a b', doc_id: 'c/d' })).toBe(
      '/knowledge/a%20b?doc=c%2Fd',
    )
  })
})

describe('normalizeKbCitations', () => {
  it('保留完整字段并去重', () => {
    const out = normalizeKbCitations([
      { kb_id: 'kb-1', doc_id: 'd1', doc_name: 'a.pdf', page: 3, block_id: 'b1', snippet: 's' },
      { kb_id: 'kb-1', doc_id: 'd1', doc_name: 'a.pdf', page: 3, block_id: 'b1', snippet: 's' },
    ])
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ kb_id: 'kb-1', doc_id: 'd1', page: 3 })
  })

  it('容忍非法页码与缺字段（老会话 metadata）', () => {
    const out = normalizeKbCitations([
      { doc_name: 'a.pdf', page: 'x' },
      { kb_id: 'kb-1', page: -2 },
      { nothing: true },
      null,
      'oops',
    ])
    // 第 3 条既无 kb/doc 也无 doc_name ⇒ 丢弃；其余保留（不可点但可读）
    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ doc_name: 'a.pdf', page: null, kb_id: '' })
    expect(out[1]).toMatchObject({ kb_id: 'kb-1', page: null })
  })

  it('非数组输入返回空表', () => {
    expect(normalizeKbCitations(undefined)).toEqual([])
    expect(normalizeKbCitations(null)).toEqual([])
    expect(normalizeKbCitations({ kb_id: 'x' })).toEqual([])
  })
})

describe('kbCitationLabel', () => {
  it('文档名 + 页码；缺文档名时用通用文案', () => {
    expect(kbCitationLabel({ doc_name: 'a.pdf', page: 3 })).toContain('a.pdf')
    expect(kbCitationLabel({ doc_name: 'a.pdf', page: 3 })).toContain('3')
    expect(kbCitationLabel({ page: 3 })).not.toContain('undefined')
  })

  it('没有页码时只给文档名', () => {
    expect(kbCitationLabel({ doc_name: 'a.pdf', page: null })).toBe('a.pdf')
  })
})
