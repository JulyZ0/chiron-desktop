/**
 * 统计"Agent 的某个绑定列"被哪些 Agent 使用（反向引用）。
 *
 * 目前两处用得上：
 * - `agents.kb_id`（迁移 3b8e5f1a7c94）→ 知识库"被哪些 Agent 当默认库"
 * - `agents.plugins`（迁移 7d21b9c4e8f3）→ 插件"被哪些 Agent 装配"
 *
 * 与 `skillUsage` 同族：后端没有"谁在用我"的查询，而 Agent 列表前端本来就会拉，
 * 自己算一遍比新增一次后端往返划算。拉失败只让这一项为空。
 */

export interface BindingUsage {
  agents: string[]
}

/** 取绑定列里规范化后的标识集合（容忍单值字符串与字符串数组） */
function valuesOf(raw: unknown): string[] {
  const out: string[] = []
  const push = (value: unknown) => {
    if (typeof value !== 'string') return
    const trimmed = value.trim()
    if (trimmed && !out.includes(trimmed)) out.push(trimmed)
  }
  if (Array.isArray(raw)) raw.forEach(push)
  else push(raw)
  return out
}

/**
 * 聚合出 绑定值 → { agents }。
 * `field` 是 Agent 对象上的列名（`kb_id` / `plugins` …）；同一 Agent 只记一次，
 * 缺 name 的条目跳过（否则会产出一个空名字的 owner）。
 */
export function collectBindingUsage(
  agents: readonly Record<string, unknown>[],
  field: string,
): Map<string, BindingUsage> {
  const usage = new Map<string, BindingUsage>()
  for (const agent of agents) {
    const name = typeof agent?.name === 'string' ? agent.name.trim() : ''
    if (!name) continue
    for (const value of valuesOf(agent[field])) {
      const entry = usage.get(value) ?? { agents: [] }
      if (!entry.agents.includes(name)) entry.agents.push(name)
      usage.set(value, entry)
    }
  }
  return usage
}

/** 查一个绑定值的使用情况；没有记录时返回空壳，调用方不用再判空 */
export function bindingUsageOf(usage: Map<string, BindingUsage>, value: string): BindingUsage {
  return usage.get(value) ?? { agents: [] }
}

// ── 知识库的既有入口（KnowledgeView 与其单测在用）──

export type KbUsage = BindingUsage

export function collectKbUsage(
  agents: readonly { name: string; kb_id?: unknown }[],
): Map<string, KbUsage> {
  return collectBindingUsage(agents as readonly Record<string, unknown>[], 'kb_id')
}

export function kbUsageOf(usage: Map<string, KbUsage>, kbId: string): KbUsage {
  return bindingUsageOf(usage, kbId)
}
