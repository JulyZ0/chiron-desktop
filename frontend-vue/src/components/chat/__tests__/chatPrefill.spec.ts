import { afterEach, describe, expect, it } from 'vitest'
import {
  CHAT_PREFILL_KEY,
  buildPrefillText,
  setChatPrefill,
  takeChatPrefill,
} from '../chatPrefill'

describe('buildPrefillText（插入输入框的文本）', () => {
  it('带标题时说明结果来源', () => {
    const text = buildPrefillText({ title: '运行结果 · 研究助手', text: '结论如下' })
    expect(text).toContain('运行结果 · 研究助手')
    expect(text).toContain('结论如下')
  })

  it('没有标题也能给出可读的提示语', () => {
    const text = buildPrefillText({ title: '', text: '结论如下' })
    expect(text).toContain('请基于它继续')
    expect(text).toContain('结论如下')
  })

  it('超长内容按上限截断并标注（不把整篇结果倒进输入框）', () => {
    const text = buildPrefillText({ title: 't', text: 'x'.repeat(50) }, 10)
    expect(text).toContain('xxxxxxxxxx')
    expect(text).not.toContain('x'.repeat(11))
    expect(text).toContain('已截断')
  })

  it('内容为空时只留提示语（不产生残缺文本）', () => {
    const text = buildPrefillText({ title: 't', text: '   ' })
    expect(text).toContain('请基于它继续')
    expect(text.endsWith('\n')).toBe(false)
  })
})

describe('投递与消费（sessionStorage）', () => {
  afterEach(() => {
    sessionStorage.clear()
  })

  it('取一次即删，避免每次进入对话都重复插入', () => {
    setChatPrefill({ title: 't', text: 'hello', source: 'agent' })
    expect(takeChatPrefill()).toEqual({ title: 't', text: 'hello', source: 'agent', kind: 'result' })
    expect(takeChatPrefill()).toBeNull()
    expect(sessionStorage.getItem(CHAT_PREFILL_KEY)).toBeNull()
  })

  it('没有投递时返回 null（进对话不会莫名插入内容）', () => {
    expect(takeChatPrefill()).toBeNull()
  })

  it('损坏的投递数据不抛错', () => {
    sessionStorage.setItem(CHAT_PREFILL_KEY, '{ not json')
    expect(takeChatPrefill()).toBeNull()
  })

  it('缺少 text 字段的数据视为无效', () => {
    sessionStorage.setItem(CHAT_PREFILL_KEY, JSON.stringify({ title: 't' }))
    expect(takeChatPrefill()).toBeNull()
  })

  it('能力投递：开场白是「我想用…来做」，而不是「基于结果继续」', () => {
    const text = buildPrefillText({
      title: 'Execute Python Code',
      text: '在沙箱中执行代码',
      kind: 'capability',
    })
    expect(text).toContain('我想用「Execute Python Code」来做')
    expect(text).not.toContain('请基于它继续')
    expect(text).toContain('在沙箱中执行代码')
  })

  it('缺省 kind 为 result；capability 投递可完整往返', () => {
    setChatPrefill({ title: 'x', text: 'y' })
    expect(takeChatPrefill()?.kind).toBe('result')

    setChatPrefill({ title: 'x', text: 'y', kind: 'capability' })
    expect(takeChatPrefill()?.kind).toBe('capability')
  })
})
