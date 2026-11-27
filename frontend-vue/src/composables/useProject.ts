/**
 * 当前**项目**（记忆隔离维度，C3）。
 *
 * 语义：L2 记忆按 `tenant + user + project` 隔离，**空串 = 全局** —— 该条目对所有项目
 * 可见并会被自动带上；具名项目之间仍互不可见（缺省值即全局，与既有行为向后兼容）。
 *
 * 两处消费点：
 * - **记忆页**用它决定"看/改哪个项目的记忆"；
 * - **对话工作台**把它放进 `workbench_context.project`，于是 agent 的
 *   `remember` / `recall` / `forget` 也落在同一个项目里（引擎侧从 tool context 取）。
 *
 * 用 `ref` + localStorage 而不是裸读 localStorage：刷新后"从项目里掉出来"是记忆串味的
 * 经典起点，而记忆页需要**响应式**地跟着切换重新加载。
 *
 * 作为模块级单例导出：全应用共享同一份状态（多个页面各持一份必然漂移）。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'chiron.memory.project'

function readStored(): string {
  try {
    return (localStorage.getItem(STORAGE_KEY) || '').trim()
  } catch {
    // 隐私模式 / SSR 等拿不到 localStorage：退回"全局"，不影响功能
    return ''
  }
}

/** 当前项目（空串 = 全局视图）。 */
export const currentProject = ref<string>(readStored())

/** 切换项目并持久化（空串 = 回到全局视图）。 */
export function setProject(name: string): void {
  const next = (name || '').trim()
  currentProject.value = next
  try {
    if (next) localStorage.setItem(STORAGE_KEY, next)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    // 落盘失败也要在当前会话内生效（内存里已经是 next）
  }
}

/**
 * 给 HTTP 请求用的 query 片段。
 *
 * 空项目 → **不带参数**（服务端把"没有 project"与"project=''"都当全局范围）——
 * 这样未使用项目的用户，请求与 C3 之前**逐字相同**。
 */
export function projectParams(): Record<string, string> {
  return currentProject.value ? { project: currentProject.value } : {}
}
