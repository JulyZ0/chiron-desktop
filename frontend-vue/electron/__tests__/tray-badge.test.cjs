'use strict'
/**
 * F10 后半：托盘未读计数（纯文案 + 接线守卫）。
 *
 * 计数对象是**未读站内通知**：服务端已有 `GET /v1/notifications/unread` 的
 * COUNT(*) 与真实产生方；这里只钉住"0 显示什么、999+ 怎么收、脏值怎么兜底"，
 * 以及通道两侧（preload 发 / main 收）与能力表是否一致。
 */
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const badge = require('../tray-badge.cjs')

const read = name => fs.readFileSync(path.resolve(__dirname, '..', name), 'utf8')

test('normalizeUnread：负值/NaN/小数/超界都收敛到安全整数', () => {
  for (const value of [-3, NaN, undefined, null, 'x']) {
    assert.equal(badge.normalizeUnread(value), 0, `value=${value}`)
  }
  assert.equal(badge.normalizeUnread(2.9), 2)
  assert.equal(badge.normalizeUnread('7'), 7)
  assert.equal(badge.normalizeUnread(1000), 999)
})

test('0 条时 tooltip 与菜单保持原样（不制造噪音）', () => {
  assert.equal(badge.trayTooltip(0), 'Chiron')
  assert.equal(badge.showWindowLabel(0), '显示主窗口')
})

test('有未读时同时体现在 tooltip 与菜单文案里', () => {
  assert.equal(badge.trayTooltip(3), 'Chiron · 3 条未读通知')
  assert.equal(badge.showWindowLabel(3), '显示主窗口（3 条未读）')
  assert.equal(badge.trayTooltip(5000), 'Chiron · 999 条未读通知')
})

test('preload 推送通道与主进程接收通道逐字一致', () => {
  const preload = read('preload.cjs')
  assert.ok(preload.includes('setTrayUnread'), 'preload 缺少 setTrayUnread')
  assert.ok(preload.includes("ipcRenderer.send('desktop:tray-unread'"))
  const main = read('main.cjs')
  assert.ok(main.includes("ipcMain.on('desktop:tray-unread'"))
  assert.ok(main.includes('normalizeUnread(payload?.count)'))
  // 收到后必须同时更新 tooltip 与菜单（只改 tooltip 的话，不悬停就看不见数字）
  assert.ok(main.includes('tray.setToolTip(trayTooltip(trayUnreadCount))'))
  assert.ok(main.includes('tray.setContextMenu(buildTrayMenu())'))
})

test('能力表只允许主界面推送托盘计数', () => {
  const guard = require('../ipc-guard.cjs')
  assert.equal(guard.canCall('main', 'desktop:tray-unread'), true)
  for (const kind of ['setup', 'error', 'quickAsk', '']) {
    assert.equal(guard.canCall(kind, 'desktop:tray-unread'), false, kind)
  }
})

test('系统通知：通道两侧一致，且只有主界面能弹', () => {
  const preload = read('preload.cjs')
  assert.ok(preload.includes('notifyNative'))
  assert.ok(preload.includes("ipcRenderer.send('desktop:notify-native'"))
  const main = read('main.cjs')
  assert.ok(main.includes("ipcMain.on('desktop:notify-native'"))
  assert.ok(main.includes('Notification.isSupported()'))
  // 点击通知要把主窗口带回来，否则用户点完毫无反应
  assert.ok(main.includes("notice.on('click', () => showMainWindow())"))

  const guard = require('../ipc-guard.cjs')
  assert.equal(guard.canCall('main', 'desktop:notify-native'), true)
  for (const kind of ['setup', 'error', 'quickAsk', '']) {
    assert.equal(guard.canCall(kind, 'desktop:notify-native'), false, kind)
  }
})
