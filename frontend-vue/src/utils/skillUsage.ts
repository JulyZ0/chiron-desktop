/**
 * 统计每个技能被谁在用（反向引用）。
 *
 * 两个来源的字段位置不同，各取权威处，不做字段猜测：
 * - **Agent**：`agents.skills`（JSONB 技能名数组，迁移 3b8e5f1a7c94 新增），
 *   引擎派发时 `_get_skills_context` 就是用它筛技能的；
 * - **工作流**：graph_json 里 `node_type === 'skill'` 的节点，技能名在 `config.skill_name`
 *   —— python-engine/app/workflow/engine.py 的 `_skill_node` 读的正是这个键，
 *   缺它节点会直接返回 "skill_name is required"。
 */

export interface SkillUsage {
  agents: string[]
  workflows: string[]
}

export interface SkillUsageSources {
  agents: readonly { name: string; skills?: unknown }[]
  graphs: readonly { name: string; graph_json?: unknown }[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 从 graph_json 里挑出技能名：容忍字符串形态的 graph_json 与结构不完整的节点 */
export function skillNamesOfGraph(graphJson: unknown): string[] {
  let parsed: unknown = graphJson
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch {
      return []
    }
  }
  const nodes = isRecord(parsed) ? parsed.nodes : undefined
  if (!Array.isArray(nodes)) return []

  const names: string[] = []
  for (const node of nodes) {
    if (!isRecord(node) || node.node_type !== 'skill') continue
    const skillName = isRecord(node.config) ? node.config.skill_name : undefined
    if (typeof skillName === 'string') names.push(skillName)
  }
  return names
}

/**
 * 聚合出 技能名 → { agents, workflows }。同名 owner 去重，空技能名忽略。
 */
export function collectSkillUsage(sources: SkillUsageSources): Map<string, SkillUsage> {
  const usage = new Map<string, SkillUsage>()

  const bump = (skillName: string, kind: keyof SkillUsage, owner: string) => {
    const name = skillName.trim()
    if (!name) return
    const entry = usage.get(name) ?? { agents: [], workflows: [] }
    if (!entry[kind].includes(owner)) entry[kind].push(owner)
    usage.set(name, entry)
  }

  for (const agent of sources.agents) {
    if (!Array.isArray(agent.skills)) continue
    for (const skill of agent.skills) {
      if (typeof skill === 'string') bump(skill, 'agents', agent.name)
    }
  }

  for (const graph of sources.graphs) {
    for (const skillName of skillNamesOfGraph(graph.graph_json)) {
      bump(skillName, 'workflows', graph.name)
    }
  }

  return usage
}

/** 查一个技能的使用情况；没有记录时返回空壳，调用方不用再判空 */
export function usageOf(usage: Map<string, SkillUsage>, skillName: string): SkillUsage {
  return usage.get(skillName) ?? { agents: [], workflows: [] }
}
