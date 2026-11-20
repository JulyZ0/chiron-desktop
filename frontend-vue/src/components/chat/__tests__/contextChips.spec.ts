import { describe, expect, it } from 'vitest'
import {CONTEXT_QUERY_KEYS, DEFAULT_KB_WEIGHT, KB_WEIGHT_MAX, KB_WEIGHT_MIN, MAX_CONTEXT_KBS, buildWorkbenchContext, chipsFromWorkbenchContext, clampKbWeight, parseContextQuery, type ContextChip} from '../contextChips'

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
    // kb_ids 是 `[{id, weight}]`（P1-8）：缺省权重 1.0，老库行为不变
    expect(ctx).toEqual({
      kb_id: 'kb-1',
      kb_ids: [{ id: 'kb-1', weight: 1 }, { id: 'kb-2', weight: 1 }],
    })
  })

  it('各类上下文各自成字段，技能本来就是数组', () => {
    const ctx = buildWorkbenchContext(chips(
      ['kb', 'kb-1'], ['agent', 'ag-1'], ['plugin', 'fs'], ['skill', 'pdf'], ['skill', 'web'],
    ))
    expect(ctx).toEqual({
      kb_id: 'kb-1',
      kb_ids: [{ id: 'kb-1', weight: 1 }],
      agent_id: 'ag-1',
      agent_ids: ['ag-1'],
      plugin_names: ['fs'],
      skill_names: ['pdf', 'web'],
    })
  })

  it('同类重复值去重（URL 里手写重复不该变成两次检索）', () => {
    const ctx = buildWorkbenchContext(chips(['kb', 'kb-1'], ['kb', 'kb-1']))
    expect(ctx).toEqual({ kb_id: 'kb-1', kb_ids: [{ id: 'kb-1', weight: 1 }] })
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
    expect(ctx.kb_ids).toEqual([{ id: 'kb-0', weight: 1 }, { id: 'kb-1', weight: 1 }])
    expect(ctx).not.toHaveProperty('knowledge_base_ids')
    expect(ctx).not.toHaveProperty('kbIds')
  })

  it('多选提交：kb_ids 数组带上全部已选库（单值 kb_id 仍取首个）', () => {
    const ctx = buildWorkbenchContext(manyKb(3))
    expect(ctx).toEqual({
      kb_id: 'kb-0',
      kb_ids: [
        { id: 'kb-0', weight: 1 },
        { id: 'kb-1', weight: 1 },
        { id: 'kb-2', weight: 1 },
      ],
    })
  })

  it('超过上限时截断到 5 个（与引擎 MAX_CONTEXT_KBS 对齐）', () => {
    const ctx = buildWorkbenchContext(manyKb(8)) as { kb_ids: { id: string }[]; kb_id: string }
    expect(MAX_CONTEXT_KBS).toBe(5)
    expect(ctx.kb_ids.map(k => k.id)).toEqual(['kb-0', 'kb-1', 'kb-2', 'kb-3', 'kb-4'])
    expect(ctx.kb_id).toBe('kb-0')
  })

  it('URL 里手写 8 个 kb 参数也只在提交时带 5 个', () => {
    const chips = parseContextQuery({ kb: manyKb(8).map(c => c.value) })
    expect(chips).toHaveLength(8) // 解析全保留（用户可能想先看到自己传了什么）
    const ctx = buildWorkbenchContext(chips) as { kb_ids: unknown[] }
    expect(ctx.kb_ids).toHaveLength(MAX_CONTEXT_KBS)
  })

  it('上限只作用于知识库：agent/skill/plugin 不受影响', () => {
    const ctx = buildWorkbenchContext([
      ...manyKb(7),
      ...Array.from({ length: 6 }, (_, i) => ({ type: 'skill' as const, label: '', value: `s-${i}` })),
    ]) as { kb_ids: unknown[]; skill_names: string[] }
    expect(ctx.kb_ids).toHaveLength(5)
    expect(ctx.skill_names).toHaveLength(6)
  })
})

/**
 * 多库权重（P1-8）：选中多个库时可以调"这个库多重要"。
 *
 * 引擎侧（`workbench_context.context_weights`）按 `weight × 名次分` 排序后合并结果；
 * 前端这里只负责两件事：**发对形状**、**不把非法权重发出去**。
 */
describe('知识库权重（0.1–3.0，默认 1.0）', () => {
  it('★ 多库时 kb_ids 带上每个库的权重（默认 1.0）', () => {
    const ctx = buildWorkbenchContext([
      { type: 'kb', label: 'a', value: 'kb-a', weight: 2 },
      { type: 'kb', label: 'b', value: 'kb-b' },
    ]) as { kb_id: string; kb_ids: { id: string; weight: number }[] }

    expect(ctx.kb_id).toBe('kb-a')
    expect(ctx.kb_ids).toEqual([
      { id: 'kb-a', weight: 2 },
      { id: 'kb-b', weight: 1 },
    ])
  })

  it('★ 权重越界/非法一律收敛到 [0.1, 3.0]（引擎另有一道兜底，但别指望它）', () => {
    const ctx = buildWorkbenchContext([
      { type: 'kb', label: 'a', value: 'kb-a', weight: 99 },
      { type: 'kb', label: 'b', value: 'kb-b', weight: -3 },
      { type: 'kb', label: 'c', value: 'kb-c', weight: Number.NaN },
    ]) as { kb_ids: { id: string; weight: number }[] }

    expect(KB_WEIGHT_MAX).toBe(3)
    expect(KB_WEIGHT_MIN).toBe(0.1)
    expect(ctx.kb_ids).toEqual([
      { id: 'kb-a', weight: 3 },
      { id: 'kb-b', weight: 0.1 },
      { id: 'kb-c', weight: 1 },
    ])
  })

  it('权重保留一位小数（滑块 0.1 步进的输入不该出现 1.7000000000000002）', () => {
    expect(clampKbWeight(1.74)).toBe(1.7)
    expect(clampKbWeight('2.25')).toBe(2.3)
    expect(clampKbWeight(undefined)).toBe(1)
    expect(clampKbWeight('abc')).toBe(1)
  })

  it('★ 刷新后从 runtime.context 还原权重（否则用户调好的权重悄悄回到 1.0）', () => {
    const chips = chipsFromWorkbenchContext({
      kb_ids: [{ id: 'kb-a', weight: 2.5 }, 'kb-b'],
    })
    expect(chips.map(c => [c.value, c.weight])).toEqual([
      ['kb-a', 2.5],
      ['kb-b', undefined],
    ])
    // 往返一致：还原后再组装，权重不丢
    const ctx = buildWorkbenchContext(chips) as { kb_ids: { id: string; weight: number }[] }
    expect(ctx.kb_ids).toEqual([
      { id: 'kb-a', weight: 2.5 },
      { id: 'kb-b', weight: 1 },
    ])
  })

  it('梦游 URL（裸字符串 kb_ids 的老会话）仍能还原成 chip', () => {
    const chips = chipsFromWorkbenchContext({ kb_ids: ['kb-x', 'kb-y'] })
    expect(chips.map(c => c.value)).toEqual(['kb-x', 'kb-y'])
  })
})
