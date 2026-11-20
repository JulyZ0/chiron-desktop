import { describe, expect, it } from 'vitest'
import {CONTEXT_QUERY_KEYS, MAX_CONTEXT_KBS, buildWorkbenchContext, parseContextQuery, type ContextChip} from '../contextChips'

const chips = (...pairs: [ContextChip['type'], string][]): ContextChip[] =>
  pairs.map(([type, value]) => ({ type, label: value, value }))

describe('parseContextQuery（URL 约定）', () => {
  it('单值写法保持兼容（老链接不受影响）', () => {
    const parsed = parseContextQuery({ kb: 'kb-1', skill: 'pdf-tools' })
    expect(parsed.map(c => [c.type, c.value])).toEqual([['kb', 'kb-1'], ['skill', 'pdf-tools']])
  })

  it('同名参数可重复，逐个成为 chip（这是多选的关键）', () => {
    const parsed = parseContextQuery({ kb: ['kb-1', 'kb-2'], skill: ['a', 'b', 'c'] })
    expect(parsed.filter(c => c.type === 'kb').map(c => c.value)).toEqual(['kb-1', 'kb-2'])
    expect(parsed.filter(c => c.type === 'skill').map(c => c.value)).toEqual(['a', 'b', 'c'])
  })

  it('忽略空串、空白与非字符串（避免产出空 chip）', () => {
    expect(parseContextQuery({ kb: '', agent: '   ', workflow: 42 })).toEqual([])
    expect(parseContextQuery({ kb: ['', 'kb-1', '  '] }).map(c => c.value)).toEqual(['kb-1'])
  })

  it('没有 query 时不报错', () => {
    expect(parseContextQuery(null)).toEqual([])
    expect(parseContextQuery(undefined)).toEqual([])
    expect(parseContextQuery({})).toEqual([])
  })

  it('chip 带可读的占位标签（真实名称由调用方补全）', () => {
    const [kb] = parseContextQuery({ kb: 'abcdef1234567890' })
    expect(kb!.label).toContain('知识库')
    expect(kb!.label).toContain('abcdef12')
  })
})

describe('buildWorkbenchContext（发给引擎的形态）', () => {
  it('单值字段取首个，同时给出多值数组（新旧后端都能工作）', () => {
    const ctx = buildWorkbenchContext(chips(['kb', 'kb-1'], ['kb', 'kb-2']))
    expect(ctx).toEqual({ kb_id: 'kb-1', kb_ids: ['kb-1', 'kb-2'] })
  })

  it('各类上下文各自成字段，技能本来就是数组', () => {
    const ctx = buildWorkbenchContext(chips(
      ['kb', 'kb-1'], ['agent', 'ag-1'], ['plugin', 'fs'], ['skill', 'pdf'], ['skill', 'web'],
    ))
    expect(ctx).toEqual({
      kb_id: 'kb-1',
      kb_ids: ['kb-1'],
      agent_id: 'ag-1',
      agent_ids: ['ag-1'],
      plugin_names: ['fs'],
      skill_names: ['pdf', 'web'],
    })
  })

  it('同类重复值去重（URL 里手写重复不该变成两次检索）', () => {
    const ctx = buildWorkbenchContext(chips(['kb', 'kb-1'], ['kb', 'kb-1']))
    expect(ctx).toEqual({ kb_id: 'kb-1', kb_ids: ['kb-1'] })
  })

  it('没有上下文时返回 undefined（调用方据此不写 context 字段）', () => {
    expect(buildWorkbenchContext([])).toBeUndefined()
    expect(buildWorkbenchContext(chips(['kb', '']))).toBeUndefined()
  })

  it('memory chip：重复参数解析为多个分类，并组装成 memory_slots', () => {
    const chips = parseContextQuery({ memory: ['preference', 'fact'] })
    expect(chips.filter((c) => c.type === 'memory')).toHaveLength(2)
    expect(buildWorkbenchContext(chips)).toEqual({
      memory_slots: ['preference', 'fact'],
    })
  })

  it('memory chip：单值写法同上；all 原样透传（折叠由服务端负责）', () => {
    expect(buildWorkbenchContext(parseContextQuery({ memory: 'preference' }))).toEqual({
      memory_slots: ['preference'],
    })
    expect(buildWorkbenchContext(parseContextQuery({ memory: 'all' }))).toEqual({
      memory_slots: ['all'],
    })
  })

  it('memory 参数已纳入统一清空列表（新增 chip 类型不再漏清）', () => {
    expect(CONTEXT_QUERY_KEYS).toContain('memory')
  })
})

describe('知识库多选（P1-1：上限 5）', () => {
  const manyKb = (n: number): ContextChip[] =>
    Array.from({ length: n }, (_, i) => ({ type: 'kb', label: `kb-${i}`, value: `kb-${i}` }))

  it('提交字段名就是引擎读的 kb_ids / kb_id（不是 knowledge_base_ids）', () => {
    // 这条断言防的是"两端各用一个名字"的断点：引擎侧读的是
    // python-engine/app/agent/workbench_context.py::context_ids(ctx, "kb") →
    // `<name>_ids` / `<name>_id`。前端一旦改名（如 knowledge_base_ids），
    // 网关照旧透传、引擎照旧解析，但解析结果恒为空 —— 表现就是"选了库却没有任何注入"。
    const ctx = buildWorkbenchContext(manyKb(2)) as Record<string, unknown>
    expect(Object.keys(ctx).sort()).toEqual(['kb_id', 'kb_ids'])
    expect(ctx.kb_ids).toEqual(['kb-0', 'kb-1'])
    expect(ctx).not.toHaveProperty('knowledge_base_ids')
    expect(ctx).not.toHaveProperty('kbIds')
  })

  it('多选提交：kb_ids 数组带上全部已选库（单值 kb_id 仍取首个）', () => {
    const ctx = buildWorkbenchContext(manyKb(3))
    expect(ctx).toEqual({ kb_id: 'kb-0', kb_ids: ['kb-0', 'kb-1', 'kb-2'] })
  })

  it('超过上限时截断到 5 个（与引擎 MAX_CONTEXT_KBS 对齐）', () => {
    const ctx = buildWorkbenchContext(manyKb(8)) as { kb_ids: string[]; kb_id: string }
    expect(MAX_CONTEXT_KBS).toBe(5)
    expect(ctx.kb_ids).toEqual(['kb-0', 'kb-1', 'kb-2', 'kb-3', 'kb-4'])
    expect(ctx.kb_id).toBe('kb-0')
  })

  it('URL 里手写 8 个 kb 参数也只在提交时带 5 个', () => {
    const chips = parseContextQuery({ kb: manyKb(8).map(c => c.value) })
    expect(chips).toHaveLength(8) // 解析全保留（用户可能想先看到自己传了什么）
    const ctx = buildWorkbenchContext(chips) as { kb_ids: string[] }
    expect(ctx.kb_ids).toHaveLength(MAX_CONTEXT_KBS)
  })

  it('上限只作用于知识库：agent/skill/plugin 不受影响', () => {
    const ctx = buildWorkbenchContext([
      ...manyKb(7),
      ...Array.from({ length: 6 }, (_, i) => ({ type: 'skill' as const, label: '', value: `s-${i}` })),
    ]) as { kb_ids: string[]; skill_names: string[] }
    expect(ctx.kb_ids).toHaveLength(5)
    expect(ctx.skill_names).toHaveLength(6)
  })
})
