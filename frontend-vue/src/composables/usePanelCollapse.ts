/**
 * 侧面板（轨迹/历史等）的展开折叠状态。
 *
 * 三条行为约定（此前散落在各视图里各写一遍，于是"关掉后刷新又自己回来"）：
 *
 * 1. **记住用户的选择**：宽屏下用户手动关掉，刷新后保持关闭 —— 存储的是用户的
 *    选择，而不是"按屏幕宽度算出来的默认值"；
 * 2. **窄屏自动收起，但不覆盖宽屏偏好**：≤1024px 时面板让位给正文，此时不写存储；
 *    回到宽屏后恢复用户之前的选择（没选过则用 `defaultOpen()`）；
 * 3. **窄屏下用户仍可手动打开**：抽屉形态，同样不写宽屏偏好。
 *
 * 返回 `open` 是可直接绑定的 ref；`setOpen/toggle` 是用户动作入口（写存储）。
 */
import { onBeforeUnmount, ref } from 'vue'

export interface PanelCollapseOptions {
  /** localStorage 键（每个面板独立：同时打开多个面板时互不干扰） */
  storageKey: string
  /** 宽屏且从未选择过时的默认值 */
  defaultOpen: () => boolean
  /** 窄屏判定（默认 <=1024px） */
  narrowQuery?: string
}

function readStored(key: string): boolean | null {
  try {
    const raw = globalThis.localStorage?.getItem(key)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch {
    // 隐私模式 / 存储被禁用：当作"没选过"
  }
  return null
}

function writeStored(key: string, value: boolean): void {
  try {
    globalThis.localStorage?.setItem(key, value ? '1' : '0')
  } catch {
    // 写不进去不影响本次会话的状态
  }
}

export function usePanelCollapse(options: PanelCollapseOptions) {
  const query = options.narrowQuery ?? '(max-width: 1024px)'
  const stored = readStored(options.storageKey)

  let media: MediaQueryList | null = null
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    media = window.matchMedia(query)
  }
  const isNarrow = ref(media?.matches ?? false)
  // 窄屏期间的"宽屏偏好"快照：进入窄屏时保存，离开时恢复
  let wideOpen = stored ?? options.defaultOpen()
  const open = ref(isNarrow.value ? false : wideOpen)

  function onMediaChange(event: MediaQueryListEvent): void {
    isNarrow.value = event.matches
    if (event.matches) {
      // 进入窄屏：收起，但不改 wideOpen（用户的选择留给宽屏恢复）
      open.value = false
    } else {
      open.value = wideOpen
    }
  }

  if (media) {
    if (typeof media.addEventListener === 'function') {
      media.addEventListener('change', onMediaChange)
    } else {
      // 老 Safari
      ;(media as MediaQueryList & { addListener?: (cb: (e: MediaQueryListEvent) => void) => void })
        .addListener?.(onMediaChange)
    }
  }

  /** 用户动作：写存储（窄屏下也写 —— 它表达的是"我这次要打开"，宽屏恢复时沿用） */
  function setOpen(value: boolean): void {
    open.value = value
    wideOpen = value
    writeStored(options.storageKey, value)
  }

  function toggle(): void {
    setOpen(!open.value)
  }

  onBeforeUnmount(() => {
    if (!media) return
    if (typeof media.removeEventListener === 'function') {
      media.removeEventListener('change', onMediaChange)
    } else {
      ;(media as MediaQueryList & { removeListener?: (cb: (e: MediaQueryListEvent) => void) => void })
        .removeListener?.(onMediaChange)
    }
  })

  return { open, isNarrow, setOpen, toggle }
}
