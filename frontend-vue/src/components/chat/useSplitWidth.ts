/**
 * 分屏栏的宽度：可拖拽 + 本地记忆 + 双击重置 + 键盘微调。
 *
 * 为什么抽出来：对照栏现在有两种（参考会话 `SessionPreviewPane`、子 Agent
 * `SubAgentPane`），它们**必须是同一种手感的同一条分割线** —— 两处各写一份，
 * 早晚会漂移成"一个能双击重置、另一个不能"。
 *
 * 传 `storageKey` 而不是共用一把键：两栏可以同时打开，宽度是各自的。
 *
 * 刻意**不设默认宽度**：只有用户真的拖过之后才应用固定 px 宽度；没拖过就保持
 * 「内容决定宽度」。这样老用户零观感变化，双击重置也有明确语义。
 */
import { computed, onUnmounted, ref } from 'vue'

/** 指针死区（CDS v1 §5.3.2 的 `qM = 4px`）：轻点手柄或手抖不该被当成拖拽 */
const DEAD_ZONE_PX = 4
const MIN_WIDTH = 260
/** 上限 = 容器宽度的这个比例：保证主区域永远留得下可用宽度 */
const MAX_RATIO = 0.6

export interface SplitWidthOptions {
  /** localStorage 键（每栏独立） */
  storageKey: string
  /** 手柄贴哪条边 —— 决定拖动方向（贴左手柄向左拖变宽） */
  handleSide: () => 'left' | 'right'
}

export function useSplitWidth(opts: SplitWidthOptions) {
  function readWidth(): number | null {
    try {
      const raw = localStorage.getItem(opts.storageKey)
      if (!raw) return null
      const n = Number.parseInt(raw, 10)
      return Number.isFinite(n) && n > 0 ? n : null
    } catch {
      return null // 隐私模式下 localStorage 不可用：退回「内容决定宽度」
    }
  }

  const paneRef = ref<HTMLElement | null>(null)
  const paneWidth = ref<number | null>(readWidth())
  const resizing = ref(false)

  function clampWidth(px: number): number {
    const container = paneRef.value?.parentElement?.clientWidth || 0
    const max = container > 0 ? Math.max(MIN_WIDTH, Math.round(container * MAX_RATIO)) : 900
    return Math.min(Math.max(Math.round(px), MIN_WIDTH), max)
  }

  function persistWidth() {
    if (paneWidth.value === null) return
    try {
      localStorage.setItem(opts.storageKey, String(paneWidth.value))
    } catch {
      /* 存不下就只在本次会话内生效，不影响使用 */
    }
  }

  let startX = 0
  let startWidth = 0
  /** 已按下指针、但位移还没越过死区：此阶段既不进入拖拽态，也不挂全局 Escape */
  let pressing = false
  /** 拖拽开始前的宽度基准：`null` 表示当时的「内容决定宽度」，撤销时要原样还回 */
  let widthBeforeDrag: number | null = null

  /**
   * 拖拽中途按 Escape：三步复位（对齐 desktop-agent-ui 的分栏取消闭环）
   *   1. 摘掉全局监听、清 `resizing` 标记；
   *   2. 手柄保持挂载 —— 手柄始终在栏内、不随宽度卸载，天然满足；
   *   3. 宽度还回拖拽前的基准，`null` 也照还（即恢复「内容决定宽度」）。
   * 刻意**不** persist：用户撤销了这次拖拽，不该写进本地记忆。
   */
  function onResizeEscape(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    e.preventDefault()
    e.stopPropagation()
    cancelResize()
  }

  function cancelResize() {
    resizing.value = false
    pressing = false
    window.removeEventListener('keydown', onResizeEscape, true)
    paneWidth.value = widthBeforeDrag
  }

  function onResizeStart(e: PointerEvent) {
    const el = paneRef.value
    if (!el) return
    // 先只记录起点：是否真的进入拖拽由 onResizeMove 越过死区后决定
    pressing = true
    resizing.value = false
    startX = e.clientX
    // 以**当前实际宽度**为起点：否则第一次拖动会突然跳到某个默认值
    startWidth = el.getBoundingClientRect().width
    widthBeforeDrag = paneWidth.value
    // 指针捕获：拖出窗口也不丢事件，且不必往 document 上挂/摘监听
    ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
    e.preventDefault()
  }

  function onResizeMove(e: PointerEvent) {
    if (!pressing && !resizing.value) return
    if (!resizing.value) {
      // 死区内只记录、不动作；越过阈值才真正开始拖拽（此刻才挂全局 Escape）
      if (Math.abs(e.clientX - startX) < DEAD_ZONE_PX) return
      resizing.value = true
      window.addEventListener('keydown', onResizeEscape, true)
    }
    // 手柄贴右边缘时向右拖 = 变宽；贴左边缘时向左拖 = 变宽
    const delta = opts.handleSide() === 'left' ? startX - e.clientX : e.clientX - startX
    paneWidth.value = clampWidth(startWidth + delta)
  }

  function onResizeEnd(e: PointerEvent) {
    pressing = false
    if (!resizing.value) {
      // 死区内的单击：当作没拖过 —— 不改宽度、也不写本地记忆
      ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
      return
    }
    resizing.value = false
    window.removeEventListener('keydown', onResizeEscape, true)
    ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
    persistWidth()
  }

  /** 键盘可达（role="separator"）：←/→ 微调 */
  function onResizeKey(e: KeyboardEvent) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const base = paneWidth.value ?? paneRef.value?.getBoundingClientRect().width ?? MIN_WIDTH
    const step = opts.handleSide() === 'left' ? -16 : 16
    const next = clampWidth(base + (e.key === 'ArrowLeft' ? -step : step))
    paneWidth.value = next
    persistWidth()
  }

  /** 双击手柄：回到「内容决定宽度」并清掉本地记忆 */
  function resetWidth() {
    paneWidth.value = null
    resizing.value = false
    window.removeEventListener('keydown', onResizeEscape, true)
    try {
      localStorage.removeItem(opts.storageKey)
    } catch {
      /* 忽略 */
    }
  }

  const paneStyle = computed(() =>
    paneWidth.value ? { width: `${paneWidth.value}px`, flex: '0 0 auto' } : {},
  )

  // 兜底：组件卸载时摘掉可能还挂着的拖拽 Escape
  onUnmounted(() => window.removeEventListener('keydown', onResizeEscape, true))

  return {
    paneRef,
    paneWidth,
    resizing,
    paneStyle,
    onResizeStart,
    onResizeMove,
    onResizeEnd,
    onResizeKey,
    resetWidth,
  }
}
