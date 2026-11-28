import type { Ref } from 'vue'

/**
 * 标题栏聊天插槽：AppLayout 提供两个落点，ChatView 把自己的导航控件 Teleport 进去。
 *
 * 为什么不把控件直接写进 AppLayout：这些控件的状态（项目标签、侧栏、检索、分屏、
 * 工作台…）全部长在 ChatView 的响应式图里，搬出去等于把半个聊天页的状态外提。
 * Teleport 只换 DOM 落点，组件实例、事件、键盘导航、`aria-controls` 引用都不变。
 *
 * 用 provide/inject 而不是 `document.getElementById`：单测直接挂载 ChatView 时
 * 没有布局层，注入为空会自动回退成原位渲染（`<Teleport :disabled>`），不会告警。
 */
export const TOPBAR_SLOTS = Symbol('chiron:topbar-slots')

export interface TopbarSlots {
  /** 停靠坞右侧：项目标签 + 当前会话名 */
  left: Ref<HTMLElement | null>
  /** 标题栏最右（窗口三键之前）：会话 / 轨迹 / 地图 / 搜索 / 更多 */
  right: Ref<HTMLElement | null>
}
