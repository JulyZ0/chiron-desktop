/**
 * 「快速提问」信号的跨页面桥（F10）。
 *
 * 桌面主进程收到全局快捷键后只负责把窗口拉到前台并发 IPC；页面要做的第一件事
 * 是**切到聊天页**，等 ChatView 挂载后再处理。但 IPC 到达时 ChatView 可能
 * 还没挂载（用户正在设置/知识库页），直接派发 DOM 事件会丢。
 *
 * 所以这里保留一个"待消费"标记：App 收到信号先置位并导航，ChatView 挂载时消费；
 * 已经在聊天页时则由事件监听立即处理。
 *
 * 两条信号别混：
 * - `requestQuickAsk()` 只要求**聚焦输入框**（旧快捷键路径 / 降级路径）；
 * - `submitQuickAskText()` 带着一段文本，要求**直接发送**（迷你输入框，F10 后半）。
 */
let pending = false
let pendingText = ''

const EVENT = 'chiron:quick-ask'
const TEXT_EVENT = 'chiron:quick-ask-text'

/** App 层收到 IPC 后调用：置位并通知已挂载的聊天页。 */
export function requestQuickAsk(): void {
  pending = true
  window.dispatchEvent(new Event(EVENT))
}

/** ChatView 挂载时消费一次（导航过来时信号早于挂载）。 */
export function consumeQuickAsk(): boolean {
  if (!pending) return false
  pending = false
  return true
}

/** 聊天页已挂载时监听后续信号。返回取消订阅函数。 */
export function onQuickAsk(cb: () => void): () => void {
  const handler = (): void => {
    pending = false
    cb()
  }
  window.addEventListener(EVENT, handler)
  return () => window.removeEventListener(EVENT, handler)
}

/** App 层收到迷你输入框的文本后调用：置位并通知已挂载的聊天页。 */
export function submitQuickAskText(text: string): void {
  const value = String(text || '').trim()
  if (!value) return
  pendingText = value
  window.dispatchEvent(new Event(TEXT_EVENT))
}

/** ChatView 挂载时消费一次待发送文本（空串 = 没有）。 */
export function consumeQuickAskText(): string {
  const value = pendingText
  pendingText = ''
  return value
}

/** 聊天页已挂载时监听迷你输入框提交。返回取消订阅函数。 */
export function onQuickAskText(cb: (text: string) => void): () => void {
  const handler = (): void => {
    const value = consumeQuickAskText()
    if (value) cb(value)
  }
  window.addEventListener(TEXT_EVENT, handler)
  return () => window.removeEventListener(TEXT_EVENT, handler)
}
