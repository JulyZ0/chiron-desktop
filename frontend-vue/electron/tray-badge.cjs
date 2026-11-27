'use strict'
/**
 * 托盘计数的纯逻辑（F10 后半的「动态计数」）。
 *
 * 计数对象定为**未读站内通知**：它已经有服务端 COUNT(*)（`/v1/notifications/unread`）
 * 与真实产生方（子 Agent 失败、分享被撤销），不需要另造一套"运行中会话"的跨页面
 * 状态 —— 后者在导航离开聊天页时本就没有可靠来源，容易显示一个错的数字。
 *
 * 单独成模块的原因与 update-check 相同：main.cjs 一 require 就起 Electron，
 * 而"999+ 怎么显示、0 显示什么"这类判据值得被单测钉住。
 */

/** 未读数收敛到 [0, 999] 的整数：负值/NaN/小数都落到 0 或向下取整。 */
function normalizeUnread(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return 0
  return Math.min(999, Math.floor(n))
}

/** 托盘 tooltip：0 条时保持原来的 "Chiron"，避免悬停时多一行噪音。 */
function trayTooltip(count, base = 'Chiron') {
  const n = normalizeUnread(count)
  return n > 0 ? `${base} · ${n} 条未读通知` : base
}

/** 主窗口菜单项文案：把未读数放在**能长期看见**的位置（tooltip 要悬停才出现）。 */
function showWindowLabel(count) {
  const n = normalizeUnread(count)
  return n > 0 ? `显示主窗口（${n} 条未读）` : '显示主窗口'
}

module.exports = { normalizeUnread, trayTooltip, showWindowLabel }
