import { describe, expect, it } from 'vitest'
import { findMatches, matchExcerpt, searchableText } from '../transcriptSearch'
import type { ChatItem } from '../chat-types'

const user = (id: string, content: string): ChatItem => ({ kind: 'text', role: 'user', content, id })
const assistant = (id: string, content: string): ChatItem => ({ kind: 'text', role: 'assistant', content, id })
const reasoning = (id: string, content: string): ChatItem => ({ kind: 'reasoning', content, id })
const call = (id: string, name: string, args: string): ChatItem =>
  ({ kind: 'tool_call', id, name, arguments: args, status: 'done' })

const items: ChatItem[] = [
  user('u1', '怎么配置 Redis'),
  assistant('a1', '在 .env 里配置 REDIS_URL'),
  reasoning('r1', '先确认 redis 版本'),
  call('c1', 'grep_files', '{"pattern":"redis"}'),
  user('u2', '还有一个问题'),
]

describe('transcriptSearch（会话内检索）', () => {
  it('命中正文与思考，按出现顺序返回下标', () => {
    expect(findMatches(items, 'redis')).toEqual([0, 1, 2])
  })

  it('默认不区分大小写，可显式改为敏感', () => {
    expect(findMatches(items, 'REDIS')).toEqual([0, 1, 2])
    expect(findMatches(items, 'REDIS', { caseSensitive: true })).toEqual([1])
  })

  it('不搜工具调用的参数（机器输出不进检索结果）', () => {
    expect(searchableText(items[3])).toBe('')
    expect(findMatches(items, 'pattern')).toEqual([])
  })

  it('空白查询返回空结果', () => {
    expect(findMatches(items, '')).toEqual([])
    expect(findMatches(items, '   ')).toEqual([])
  })

  it('无命中返回空数组', () => {
    expect(findMatches(items, '不存在的词')).toEqual([])
  })

  it('matchExcerpt 截取命中片段并补省略号', () => {
    const text = `${'A'.repeat(60)}needle${'B'.repeat(60)}`
    const excerpt = matchExcerpt(text, 'needle', 10)
    expect(excerpt.startsWith('…')).toBe(true)
    expect(excerpt.endsWith('…')).toBe(true)
    expect(excerpt).toContain('needle')
  })

  it('matchExcerpt 在多行正文里压平空白', () => {
    // radius=4 时尾部正好落在正文末尾，因此只补前导省略号
    expect(matchExcerpt('第一行\n第二行  关键词\n第三行', '关键词', 4)).toBe('…二行 关键词 第三行')
  })

  it('matchExcerpt 未命中时给出开头片段而不是空串', () => {
    expect(matchExcerpt('abcdefghij', 'zzz', 3)).toBe('abcdef')
  })
})
