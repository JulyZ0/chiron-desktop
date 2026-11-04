import { describe, expect, it } from 'vitest'
import { collectSkillUsage, skillNamesOfGraph, usageOf } from '../skillUsage'

const graph = (name: string, nodes: unknown[]) => ({ name, graph_json: { name, nodes, edges: [] } })
const skillNode = (skill_name: string) => ({ id: 'n1', label: 'skill', node_type: 'skill', config: { skill_name } })

describe('skillNamesOfGraph（从 graph_json 提取技能名）', () => {
  it('取 node_type=skill 节点的 config.skill_name', () => {
    expect(skillNamesOfGraph({ nodes: [skillNode('weekly-report'), { node_type: 'llm', config: {} }] }))
      .toEqual(['weekly-report'])
  })

  it('接受字符串形态的 graph_json', () => {
    expect(skillNamesOfGraph(JSON.stringify({ nodes: [skillNode('summarize')] }))).toEqual(['summarize'])
  })

  it('结构不完整时不抛错，返回空', () => {
    expect(skillNamesOfGraph(null)).toEqual([])
    expect(skillNamesOfGraph(undefined)).toEqual([])
    expect(skillNamesOfGraph('不是 JSON')).toEqual([])
    expect(skillNamesOfGraph({})).toEqual([])
    expect(skillNamesOfGraph({ nodes: 'oops' })).toEqual([])
    expect(skillNamesOfGraph({ nodes: [null, 42, { node_type: 'skill' }] })).toEqual([])
    expect(skillNamesOfGraph({ nodes: [{ node_type: 'skill', config: { skill_name: 123 } }] })).toEqual([])
  })
})

describe('collectSkillUsage（反向引用聚合）', () => {
  it('汇总 Agent 侧的 skills 列', () => {
    const usage = collectSkillUsage({
      agents: [{ name: '归档助手', skills: ['weekly-report', 'summarize'] }],
      graphs: [],
    })
    expect(usageOf(usage, 'weekly-report').agents).toEqual(['归档助手'])
    expect(usageOf(usage, 'summarize').agents).toEqual(['归档助手'])
    expect(usageOf(usage, 'weekly-report').workflows).toEqual([])
  })

  it('汇总工作流侧的 skill 节点', () => {
    const usage = collectSkillUsage({
      agents: [],
      graphs: [graph('周报流', [skillNode('weekly-report')])],
    })
    expect(usageOf(usage, 'weekly-report').workflows).toEqual(['周报流'])
    expect(usageOf(usage, 'weekly-report').agents).toEqual([])
  })

  it('同一技能被两边引用时分别记录', () => {
    const usage = collectSkillUsage({
      agents: [{ name: '归档助手', skills: ['weekly-report'] }],
      graphs: [graph('周报流', [skillNode('weekly-report')])],
    })
    const entry = usageOf(usage, 'weekly-report')
    expect(entry.agents).toEqual(['归档助手'])
    expect(entry.workflows).toEqual(['周报流'])
  })

  it('同名 owner 去重（一个 Agent 重复列同一技能只算一次）', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A', skills: ['x', 'x'] }],
      graphs: [graph('G', [skillNode('x'), skillNode('x')])],
    })
    expect(usageOf(usage, 'x').agents).toEqual(['A'])
    expect(usageOf(usage, 'x').workflows).toEqual(['G'])
  })

  it('忽略空技能名与非字符串条目（脏数据不该产出无名条目）', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A', skills: ['  ', '', 42, null, 'ok'] }],
      graphs: [],
    })
    expect([...usage.keys()]).toEqual(['ok'])
  })

  it('skills 缺失或不是数组时跳过该 Agent', () => {
    const usage = collectSkillUsage({
      agents: [{ name: 'A' }, { name: 'B', skills: 'not-an-array' }, { name: 'C', skills: ['s'] }],
      graphs: [],
    })
    expect(usageOf(usage, 's').agents).toEqual(['C'])
    expect(usage.size).toBe(1)
  })

  it('查不到的技能返回空壳而不是 undefined', () => {
    const entry = usageOf(new Map(), 'nothing')
    expect(entry).toEqual({ agents: [], workflows: [] })
  })
})
