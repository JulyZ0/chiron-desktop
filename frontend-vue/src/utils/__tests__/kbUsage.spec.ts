import { describe, expect, it } from 'vitest'
import { bindingUsageOf, collectBindingUsage, collectKbUsage, kbUsageOf } from '../kbUsage'

describe('collectBindingUsage（Agent 绑定列的通用反向引用）', () => {
  it('数组列：一个 Agent 装配多个插件时每个都记上', () => {
    const usage = collectBindingUsage(
      [{ name: '归档助手', plugins: ['fs', 'gh'] }],
      'plugins',
    )
    expect(bindingUsageOf(usage, 'fs').agents).toEqual(['归档助手'])
    expect(bindingUsageOf(usage, 'gh').agents).toEqual(['归档助手'])
  })

  it('多个 Agent 装配同一插件时都记上', () => {
    const usage = collectBindingUsage(
      [{ name: 'A', plugins: ['fs'] }, { name: 'B', plugins: ['fs'] }],
      'plugins',
    )
    expect(bindingUsageOf(usage, 'fs').agents).toEqual(['A', 'B'])
  })

  it('数组列里的重复项与空白项被忽略', () => {
    const usage = collectBindingUsage(
      [{ name: 'A', plugins: ['fs', ' fs ', '', '  '] }],
      'plugins',
    )
    expect([...usage.keys()]).toEqual(['fs'])
    expect(bindingUsageOf(usage, 'fs').agents).toEqual(['A'])
  })

  it('数组列里的非字符串项被忽略（脏数据不该产出垃圾键）', () => {
    const usage = collectBindingUsage(
      [{ name: 'A', plugins: ['fs', 42, null, { name: 'gh' }] }],
      'plugins',
    )
    expect([...usage.keys()]).toEqual(['fs'])
  })

  it('未装配 / 缺字段 / 空数组的 Agent 被跳过', () => {
    const usage = collectBindingUsage(
      [{ name: 'A' }, { name: 'B', plugins: [] }, { name: 'C', plugins: null }],
      'plugins',
    )
    expect(usage.size).toBe(0)
  })

  it('缺 name 的 Agent 跳过（否则会产出空名字的 owner）', () => {
    const usage = collectBindingUsage([{ plugins: ['fs'] }, { name: '', plugins: ['fs'] }], 'plugins')
    expect(usage.size).toBe(0)
  })
})

describe('collectKbUsage（知识库 → 被哪些 Agent 用）', () => {
  it('按 kb_id 聚合出使用的 Agent 名', () => {
    const usage = collectKbUsage([{ name: '归档助手', kb_id: 'kb-1' }])
    expect(kbUsageOf(usage, 'kb-1').agents).toEqual(['归档助手'])
  })

  it('多个 Agent 绑定同一知识库时都记上', () => {
    const usage = collectKbUsage([
      { name: 'A', kb_id: 'kb-1' },
      { name: 'B', kb_id: 'kb-1' },
    ])
    expect(kbUsageOf(usage, 'kb-1').agents).toEqual(['A', 'B'])
  })

  it('未绑定知识库的 Agent 被跳过（不应产出一个空 id 条目）', () => {
    const usage = collectKbUsage([{ name: 'A' }, { name: 'B', kb_id: '' }, { name: 'C', kb_id: '   ' }])
    expect(usage.size).toBe(0)
  })

  it('非字符串 kb_id 忽略（脏数据不该产出垃圾键）', () => {
    const usage = collectKbUsage([
      { name: 'A', kb_id: 42 },
      { name: 'B', kb_id: null },
      { name: 'C', kb_id: { id: 'kb-1' } },
      { name: 'D', kb_id: 'kb-1' },
    ])
    expect([...usage.keys()]).toEqual(['kb-1'])
  })

  it('同名 Agent 去重', () => {
    const usage = collectKbUsage([
      { name: 'A', kb_id: 'kb-1' },
      { name: 'A', kb_id: 'kb-1' },
    ])
    expect(kbUsageOf(usage, 'kb-1').agents).toEqual(['A'])
  })

  it('不同知识库互不串台', () => {
    const usage = collectKbUsage([
      { name: 'A', kb_id: 'kb-1' },
      { name: 'B', kb_id: 'kb-2' },
    ])
    expect(kbUsageOf(usage, 'kb-1').agents).toEqual(['A'])
    expect(kbUsageOf(usage, 'kb-2').agents).toEqual(['B'])
  })

  it('查不到的知识库返回空壳而不是 undefined', () => {
    expect(kbUsageOf(new Map(), 'nothing')).toEqual({ agents: [] })
  })

  it('空输入不报错', () => {
    expect(collectKbUsage([]).size).toBe(0)
  })
})
