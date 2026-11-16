/**
 * 换肤强调色的可选值。
 *
 * 放在 .ts 而非组件里有两个原因：
 * 1. 这些是**用户可选的颜色数据**，不是样式声明 —— 它们要被写进 users.settings
 *    并原样回显，无法表达为 `var(--*)`。
 * 2. 主题 token 契约只扫 `.vue`/`.css`，颜色字面量写在组件里会被判为硬编码色值。
 */
export const ACCENT_PRESETS = [
  '#4176e6', // DeepSeek 蓝
  '#10b981', // 翡翠绿
  '#8b5cf6', // 紫罗兰
  '#ef4444', // 朱红
  '#f59e0b', // 琥珀
]

/** 「自定义颜色」取色器未选中任何强调色时的兜底显示色 */
export const DEFAULT_ACCENT = '#0a0a0a'
