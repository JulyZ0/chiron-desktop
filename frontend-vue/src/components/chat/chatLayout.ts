/**
 * Chat 工作区的宽度预算。
 *
 * 背景：工作台、分屏参考、子 Agent 栏、侧栏都可以同时打开，但此前每栏各自按
 * 父容器的 50%–60% 计算上限。三个面板一起打开时，主会话会被压到接近 0 ——
 * 每个面板都"合法"，合起来不可用。
 *
 * 这里把约束收成两条：
 * 1. 主区永远保留 `CHAT_MAIN_MIN_WIDTH`；
 * 2. 辅助栏总宽不超过容器 `CHAT_AUX_MAX_RATIO`。
 *
 * `computeDockLayout` 是纯函数（给未来 DockHost 与单测），`clampDockWidth` 是
 * 当前面板拖拽时的即时约束 —— 两处共用同一组常量，避免再次漂移。
 */

/** 主会话最小可用宽度：低于它，消息正文、代码块和输入区都开始不可读。 */
export const CHAT_MAIN_MIN_WIDTH = 560

/** 辅助栏总宽上限（相对容器）。留 45% 给主区与侧栏。 */
export const CHAT_AUX_MAX_RATIO = 0.55

/** 单个辅助栏的最小宽度。 */
export const CHAT_DOCK_MIN_WIDTH = 260

/** 单个辅助栏最大占容器比例（仍需满足总预算与主区下限）。 */
export const CHAT_DOCK_MAX_RATIO = 0.6

export interface DockPaneRequest {
  id: string
  side: 'left' | 'right'
  minWidth?: number
  preferredWidth?: number
  /** 数字越小越优先保留；空间不足时先折叠大数字。 */
  priority?: number
}

export interface DockLayout {
  /** 主区可用宽度 */
  main: number
  /** 每个面板的最终宽度（未折叠的面板） */
  widths: Record<string, number>
  /** 因空间不足被折叠的面板 id */
  collapsed: string[]
}

/**
 * 计算一次停靠布局。
 *
 * 规则：
 * 1. 先满足高优先级面板（`priority` 升序），每块宽度落在 `[min, preferred]`；
 * 2. 任何分配都不能让主区低于 `mainMin`，也不能让辅助栏总宽超过 `maxAuxRatio`；
 * 3. 放不下的面板进入 `collapsed`，由 UI 提示用户手动关闭或换 tab。
 */
export function computeDockLayout(
  containerWidth: number,
  panes: readonly DockPaneRequest[],
  options: {
    mainMin?: number
    maxAuxRatio?: number
    maxDockRatio?: number
  } = {},
): DockLayout {
  const mainMin = options.mainMin ?? CHAT_MAIN_MIN_WIDTH
  const maxAuxRatio = options.maxAuxRatio ?? CHAT_AUX_MAX_RATIO
  const maxDockRatio = options.maxDockRatio ?? CHAT_DOCK_MAX_RATIO
  const container = Math.max(0, Math.floor(containerWidth))
  const auxBudget = Math.max(0, Math.min(container * maxAuxRatio, container - mainMin))
  const perDockBudget = Math.max(0, Math.floor(container * maxDockRatio))

  const ordered = [...panes].sort(
    (a, b) => (a.priority ?? 100) - (b.priority ?? 100) || a.id.localeCompare(b.id),
  )

  const widths: Record<string, number> = {}
  const collapsed: string[] = []
  let used = 0

  for (const pane of ordered) {
    const min = Math.max(1, pane.minWidth ?? CHAT_DOCK_MIN_WIDTH)
    const preferred = Math.max(min, pane.preferredWidth ?? min)
    const available = Math.floor(auxBudget - used)
    if (available < min) {
      collapsed.push(pane.id)
      continue
    }
    const width = Math.min(preferred, perDockBudget, available)
    widths[pane.id] = width
    used += width
  }

  return {
    main: Math.max(0, container - used),
    widths,
    collapsed,
  }
}

/**
 * 拖拽中的即时上限：除了单栏比例，还要保证主区不被挤到最小宽度以下。
 *
 * 与其他面板的联动通过主区实际宽度反推：
 * `其他栏总宽 = 容器 - 主区宽 - 本栏当前宽`，因此本栏最多还能占
 * `容器 - 其他栏 - mainMin - 本栏当前宽`。
 */
export function clampDockWidth(input: {
  next: number
  pane: HTMLElement | null
  minWidth?: number
  maxRatio?: number
  mainMin?: number
}): number {
  const min = input.minWidth ?? CHAT_DOCK_MIN_WIDTH
  const pane = input.pane
  // 面板可能被 DockHost 包了一层：预算必须相对整个 `.chat-layout` 计算，
  // 而不是直接父元素（否则 host 里查不到 `.chat-main`，上限会退化成 0）。
  const layout = pane?.closest('.chat-layout') as HTMLElement | null
  const container = layout?.clientWidth ?? pane?.parentElement?.clientWidth ?? 0
  if (!pane || container <= 0) return Math.max(min, Math.round(input.next))

  const main = layout?.querySelector('.chat-main') as HTMLElement | null
  const mainWidth = main?.getBoundingClientRect().width ?? 0
  const paneWidth = pane.getBoundingClientRect().width
  const others = Math.max(0, container - mainWidth - paneWidth)
  const maxByMain = Math.floor(container - others - (input.mainMin ?? CHAT_MAIN_MIN_WIDTH))
  const maxByRatio = Math.floor(container * (input.maxRatio ?? CHAT_DOCK_MAX_RATIO))
  const max = Math.max(min, Math.min(maxByRatio, maxByMain))
  return Math.min(max, Math.max(min, Math.round(input.next)))
}
