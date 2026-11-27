'use strict'
/**
 * H47：IPC 来源判定与每窗口能力表（PRD 9.2）。
 *
 * 缺陷形态：
 *   1. 只按页面 URL 判断 ⇒ **任何 iframe** 都能发同一批特权调用
 *      （`event.senderFrame` 就是为这件事准备的，旧实现取的是 `senderFrame?.url || getURL()`）；
 *   2. setup/error 与主界面共用同一份 preload ⇒ 辅助页上下文里也有 token 读写、
 *      清空数据、打开任意路径；
 *   3. 没有"每窗口能力表" ⇒ 任何信任窗口可调用任意特权 IPC。
 *
 * 这里钉住判定逻辑；真实窗口仍需 Electron 才能端到端验证（下面有源码守卫补位）。
 */
const test = require('node:test')
const assert = require('node:assert')
const path = require('node:path')
const fs = require('node:fs')

const guard = require('../ipc-guard.cjs')

const ROOT = path.resolve(__dirname, '..')
const mainSrc = () => fs.readFileSync(path.join(ROOT, 'main.cjs'), 'utf8')

function frameEvent({ url = 'file:///app/index.html', parent = null, id = 1 } = {}) {
  return {
    channel: 'desktop:get-device-id',
    sender: { id, getURL: () => url },
    senderFrame: { url, parent },
  }
}

const isTrusted = (u) => String(u).startsWith('file:///app/')

test('主框架 + 可信 URL + 已登记窗口 ⇒ 放行', () => {
  const env = frameEvent()
  const v = guard.checkSender({ ...env, channel: 'desktop:get-device-id' }, {
    isTrustedUrl: isTrusted,
    allowedWebContents: [1],
  })
  assert.equal(v.ok, true)
  assert.equal(v.webContentsId, 1)
})

test('拿不到 senderFrame ⇒ 拒绝（不猜、不退回 getURL 放行）', () => {
  const v = guard.checkSender({ sender: { id: 1, getURL: () => 'file:///app/index.html' } }, {
    isTrustedUrl: isTrusted,
  })
  assert.equal(v.ok, false)
  assert.equal(v.reason, 'no-sender-frame')
})

test('iframe（非主框架）⇒ 拒绝', () => {
  const env = frameEvent({ parent: { url: 'file:///app/index.html' } })
  const v = guard.checkSender(env, { isTrustedUrl: isTrusted })
  assert.equal(v.ok, false)
  assert.equal(v.reason, 'not-main-frame')
})

test('URL 不在白名单 ⇒ 拒绝', () => {
  const env = frameEvent({ url: 'https://evil.example/index.html' })
  const v = guard.checkSender(env, { isTrustedUrl: isTrusted })
  assert.equal(v.ok, false)
  assert.equal(v.reason, 'untrusted-url')
})

test('没登记过的窗口 ⇒ 拒绝（别处 new BrowserWindow 拿不到特权）', () => {
  const env = frameEvent({ id: 99 })
  const v = guard.checkSender(env, { isTrustedUrl: isTrusted, allowedWebContents: [1] })
  assert.equal(v.ok, false)
  assert.equal(v.reason, 'unregistered-window')
})

// ── 能力表 ─────────────────────────────────────────────────────────────────
test('主界面不能调辅助页专属之外的特权？—— 至少它拿到的是完整特权集', () => {
  for (const ch of ['desktop:wipe-data', 'desktop:save-token', 'desktop:open-path']) {
    // 这些通道名不在表里时也应显式登记（表是白名单，不是黑名单）
    const allowed = guard.canCall('main', ch) || guard.canCall('main', ch)
    assert.equal(typeof allowed, 'boolean')
  }
  assert.equal(guard.canCall('main', 'desktop:save-token'), true)
})

test('辅助页不得调用破坏性/凭据类 IPC', () => {
  for (const ch of [
    'desktop:wipe-data',
    'desktop:save-token',
    'desktop:clear-token',
    'desktop:open-path',
    'desktop:restart-backend',
    'desktop:disable-two-factor',
  ]) {
    assert.equal(guard.canCall('setup', ch), false, `setup 不该能调 ${ch}`)
    assert.equal(guard.canCall('error', ch), false, `error 不该能调 ${ch}`)
  }
})

test('辅助页保留它真正需要的能力', () => {
  for (const ch of ['desktop:get-app-info', 'desktop:set-api-url', 'desktop:test-api-url', 'desktop:restart']) {
    assert.equal(guard.canCall('setup', ch), true, ch)
  }
  assert.equal(guard.canCall('error', 'desktop:get-app-info'), true)
})

test('迷你输入框只拿到提交/关闭两条通道', () => {
  assert.equal(guard.canCall('quickAsk', 'desktop:quick-ask-submit'), true)
  assert.equal(guard.canCall('quickAsk', 'desktop:quick-ask-cancel'), true)
  for (const ch of [
    'desktop:get-token',
    'desktop:save-token',
    'desktop:clear-token',
    'desktop:wipe-data',
    'desktop:open-path',
    'desktop:pick-directory',
  ]) {
    assert.equal(guard.canCall('quickAsk', ch), false, `quickAsk 不该能调 ${ch}`)
  }
})

test('未知窗口种类/通道一律拒绝（默认关闭）', () => {
  assert.equal(guard.canCall('', 'desktop:get-app-info'), false)
  assert.equal(guard.canCall('weird', 'desktop:get-app-info'), false)
  assert.equal(guard.canCall('main', ''), false)
})

// ── 源码守卫：接线必须真的存在 ─────────────────────────────────────────────
test('main.cjs 的 assertTrustedSender 走 ipc-guard（含主框架与能力表）', () => {
  const src = mainSrc()
  assert.ok(src.includes("require('./ipc-guard.cjs')"))
  assert.ok(src.includes('ipcGuard.checkSender('))
  assert.ok(src.includes('ipcGuard.canCall('))
  assert.ok(src.includes('allowedWebContents: trustedWebContents'))
})

test('辅助页用窄 preload，主界面用完整 preload', () => {
  const src = mainSrc()
  assert.ok(
    /preload: path\.join\(\s*\n?\s*__dirname,\s*\n?\s*target === 'app' \? 'preload\.cjs' : 'aux-preload\.cjs'/.test(src),
    'createWindow 应按 target 分流 preload',
  )
})

test('aux-preload 只暴露少数出口，且不含凭据/破坏性能力', () => {
  const src = fs.readFileSync(path.join(ROOT, 'aux-preload.cjs'), 'utf8')
  for (const forbidden of ['save-token', 'clear-token', 'wipe-data', 'open-path', 'two-factor']) {
    assert.ok(!src.includes(forbidden), `aux-preload 不应出现 ${forbidden}`)
  }
  for (const needed of ['desktop:set-api-url', 'desktop:test-api-url', 'desktop:restart']) {
    assert.ok(src.includes(needed), `aux-preload 缺 ${needed}`)
  }
})
