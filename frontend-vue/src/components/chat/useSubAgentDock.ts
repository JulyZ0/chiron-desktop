/**
 * 子 Agent「对照栏」的开关与定位请求。
 *
 * ## 为什么从浮卡改成对照栏
 *
 * 此前子 Agent 是**右下角浮卡**（`FloatingPanel`）：限高到输入区顶部、宽度固定 420px，
 * 里面塞了运行树 + 输出 + 用量 + 产物 + 事件流五个 tab。它的问题不是"不好看"，
 * 而是**位置错了** —— 子 Agent 的产出是"要和主对话并排比对的东西"（它改了哪些文件、
 * 做了什么、和我自己的结论哪里不同），而浮卡是"盖在正文上、看完即关"的形态。
 *
 * 现在它和「参考另一个会话」是同一类东西：布局里的**一栏**（对照/分屏），
 * 可以拖宽度、可以和主会话同时读、可以一直开着。
 *
 * ## 为什么是模块级单例
 *
 * 与 `useWorkbench` 同理：这是**用户的工作环境**，与具体会话无关。
 * 但对照栏显示的内容与会话强相关（子 Agent 运行属于会话），切会话时
 * 内容自己刷新（`SubAgentPanel` 已按 `sessionId` 重建），开关本身不该被清掉。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'chiron:subagent-dock:open'

/** 宽度键单独导出：栏自己读写（每栏一把，不与会话分屏共用） */
export const SUBAGENT_DOCK_WIDTH_KEY = 'chiron:subagent-dock-width:v1'

/**
 * 首次进入的默认值：`false`。
 *
 * 对照栏是**按需打开**的观测面，不是常驻装饰 —— 默认展开会让每次进对话都被
 * 挤掉一栏宽度。宽屏也不默认开：这里没有"大多数时候都在看子 Agent"的场景。
 */
function readStored(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false // 隐私模式：当作关闭
  }
}

export const subagentDockOpen = ref<boolean>(readStored())

export function setSubagentDockOpen(next: boolean): void {
  subagentDockOpen.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next ? '1' : '0')
  } catch {
    /* 存不下就只在本次运行内生效 */
  }
}

export function toggleSubagentDock(): void {
  setSubagentDockOpen(!subagentDockOpen.value)
}

/**
 * 「请对照栏定位到这个 run」的请求。
 *
 * 带 `seq` 而不是只用 run_id：用户可能**连点两次同一个 run**（关掉后再点回来），
 * 只比较 id 的话第二次与第一次相同，`watch` 不触发 —— 点了没反应。
 */
export interface SubAgentFocusRequest {
  runId: string
  seq: number
}

export const subagentFocusRequest = ref<SubAgentFocusRequest | null>(null)
let focusSeq = 0

/** 打开对照栏；给了 `runId` 就连带请求定位到它。 */
export function openSubagentDock(runId?: string): void {
  setSubagentDockOpen(true)
  const id = String(runId || '').trim()
  if (!id) return
  focusSeq += 1
  subagentFocusRequest.value = { runId: id, seq: focusSeq }
}
