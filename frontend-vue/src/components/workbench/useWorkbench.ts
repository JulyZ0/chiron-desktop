/**
 * 工作台（项目文件面板）的开关状态。
 *
 * 为什么是模块级单例而不是 ChatView 里的一个 `ref`：面板的开关是**用户的工作环境**，
 * 与具体会话无关 —— 切会话、刷新页面都不该把它关掉。放组件里就会被 `v-if` 的重建
 * 顺手清空，而"每次回来都要重新点开"正是这类面板最后没人用的原因。
 *
 * 与既有分屏（`SessionPreviewPane`）的分工：
 * - 分屏比的是**另一个会话**（只读参考）；
 * - 工作台看的是**项目文件**（当前会话所属项目的磁盘内容）。
 * 两者可以同时开，所以宽度上限各取一半的容器宽度，避免一方把另一方挤成零宽。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'chiron:workbench:open'

/** 宽度键单独导出：面板自己读写，这里只管开关（宽度是拖拽中的高频状态，不该穿过 composable） */
export const WORKBENCH_WIDTH_KEY = 'chiron:workbench-width:v1'

/**
 * 首次进入的默认值：宽屏展开，窄屏收起。
 *
 * 断点取 1280 而不是侧面板的 1025：工作台是**在会话之外再开一栏**，还要和分屏
 * 参考栏共存（两者各占一半宽度）。1024 宽的窗口上再切一栏，会话正文会被压到
 * 无法阅读 —— 那时"默认展开"反而是帮倒忙。
 */
function readStored(): boolean {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch {
    // 隐私模式拿不到 localStorage：退回按屏幕宽度判断
  }
  // `matchMedia` 不是所有环境都有（jsdom、SSR、老 webview）。
  // 这个函数在**模块顶层**执行，直接调用会让"没有 matchMedia 的环境"连导入都失败 ——
  // 一个可选的面板开关不该有这种辐射面，所以缺失时按"收起"处理。
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(min-width: 1280px)').matches
  } catch {
    return false
  }
}

export const workbenchOpen = ref<boolean>(readStored())

/**
 * 「请工作台打开这个文件」的请求。
 *
 * 为什么是带 `seq` 的对象而不是一个路径字符串：用户可能**连点两次同一个路径**
 * （比如关掉预览后想再打开）。只比较路径的话第二次的值与第一次相同，`watch`
 * 不会触发，点了没反应 —— 那正是幽灵行为。序号让每次请求都是新值。
 */
export interface RevealRequest {
  /** 相对项目根的 POSIX 路径；无法映射到这个项目时为绝对路径 */
  path: string
  seq: number
}

export const revealRequest = ref<RevealRequest | null>(null)
let revealSeq = 0

/**
 * 打开工作台并定位到某个文件。
 *
 * 归一化在这里只做**无歧义**的部分（反斜杠转正斜杠、去掉 `./`）。绝对路径不在这里
 * 映射成相对路径 —— 那需要知道项目根，而根只有面板拿到 listing 之后才知道（见面板内
 * `revealPath`）。
 */
export function revealInWorkbench(path: string): void {
  const rel = String(path || '').trim().replace(/\\/g, '/').replace(/^\.\//, '')
  if (!rel) return
  revealSeq += 1
  revealRequest.value = { path: rel, seq: revealSeq }
  setWorkbenchOpen(true)
}

export function setWorkbenchOpen(next: boolean): void {
  workbenchOpen.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next ? '1' : '0')
  } catch {
    /* 存不下就只在本次运行内生效 */
  }
}

export function toggleWorkbench(): void {
  setWorkbenchOpen(!workbenchOpen.value)
}
