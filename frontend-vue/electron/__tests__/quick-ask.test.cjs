'use strict'
/**
 * F10 后半：迷你输入框的**接线守卫**。
 *
 * 这一层没有可跑的 Electron 集成测试（需要真窗口），但三条最容易漂移的接线
 * 可以用源码断言钉住 —— 它们的失效方式都是"点了没反应"，不会报错：
 *   1. 主进程 ↔ 迷你 preload 的通道名必须逐字一致；
 *   2. 迷你窗口必须登记为 `quickAsk` 且只拿到两条通道（能力表在 ipc-guard 里单测）；
 *   3. 全局快捷键/托盘菜单必须走迷你窗口，且保留"建不出来退回旧行为"的降级路径。
 */
const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const read = name => fs.readFileSync(path.resolve(__dirname, '..', name), 'utf8')

test('主进程注册了迷你输入框的两个通道，并校验来源窗口', () => {
  const src = read('main.cjs')
  assert.ok(src.includes("ipcMain.handle('desktop:quick-ask-submit'"), '缺少提交通道')
  assert.ok(src.includes("ipcMain.on('desktop:quick-ask-cancel'"), '缺少关闭通道')
  assert.ok(
    src.includes("registerTrustedWebContents(quickAskWindow.webContents, 'quickAsk')"),
    '迷你窗口必须登记为 quickAsk 才进得了能力表',
  )
  assert.ok(
    src.includes('event.sender === quickAskWindow.webContents'),
    '提交必须核对来源是迷你窗口本身，而不只是“某个可信窗口”',
  )
  assert.ok(
    src.includes('shared.QUICK_ASK_TEXT_CHANNEL'),
    '提交后必须用共享常量把文本转给主界面（与 preload 同一口径）',
  )
})

test('preload 暴露 onQuickAskText，且通道取自 shared.cjs', () => {
  const src = read('preload.cjs')
  assert.ok(src.includes('onQuickAskText'), '主界面 preload 缺少文本订阅')
  assert.ok(src.includes('shared.QUICK_ASK_TEXT_CHANNEL'))
  const shared = read('shared.cjs')
  assert.ok(shared.includes("QUICK_ASK_TEXT_CHANNEL = 'desktop:quick-ask-text'"))
  assert.ok(shared.includes('QUICK_ASK_TEXT_CHANNEL,'), '共享常量必须导出')
})

test('迷你 preload 只有提交/关闭两项能力，不碰凭据与文件系统', () => {
  const src = read('quick-ask-preload.cjs')
  for (const channel of ['desktop:quick-ask-submit', 'desktop:quick-ask-cancel']) {
    assert.ok(src.includes(channel), `缺少 ${channel}`)
  }
  for (const forbidden of [
    'get-token',
    'save-token',
    'clear-token',
    'wipe-local-data',
    'open-path',
    'pick-directory',
  ]) {
    assert.ok(!src.includes(forbidden), `迷你 preload 不该出现 ${forbidden}`)
  }
})

test('快捷键与托盘都走迷你窗口，并保留旧路径降级', () => {
  const src = read('main.cjs')
  // 快捷键：先试迷你窗口，失败才拉主窗口并聚焦输入框
  assert.ok(src.includes('if (showQuickAskWindow()) return'))
  assert.ok(src.includes('shared.QUICK_ASK_CHANNEL'))
  // 托盘菜单入口
  assert.ok(src.includes("label: '快速提问'"))
  assert.ok(src.includes('if (!showQuickAskWindow()) showMainWindow()'))
})

test('迷你页面用外链 css/js（不依赖内联脚本，CSP 不需要放开 unsafe-inline）', () => {
  const html = read('quick-ask.html')
  assert.ok(html.includes('href="./quick-ask.css"'))
  assert.ok(html.includes('src="./quick-ask.js"'))
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?<\/script>/i.test(html), '不允许内联脚本')
})
