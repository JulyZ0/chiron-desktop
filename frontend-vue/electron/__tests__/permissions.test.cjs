'use strict'
/**
 * 回归测试：「桌面端所有复制按钮一起失效」。
 *
 * 现象：网页版一切正常，装成桌面端之后每个复制按钮都点了没反应 —— 消息正文、
 * 代码块、文件路径、分享链接、新建 API Key，全都不进剪贴板，也不报错。
 *
 * 根因：桌面端两个权限 handler 用一份白名单放行，白名单里只有 `clipboard-read`。
 * 页面侧调的是 `navigator.clipboard.writeText()`，Chromium 为此申请的权限名是
 * `clipboard-sanitized-write`（**不是** `clipboard-write`），于是被
 * `setPermissionCheckHandler` 一律判 false。网页版走安全上下文，不经这条检查，
 * 所以只有桌面端复现。调用点普遍写成 `.then()` 且没有 catch，失败因此是静默的。
 *
 * 白名单放在 `shared.cjs` 而不是 `main.cjs`，是因为 main.cjs 一 require 就起
 * Electron，单测跑不起来；而这里要钉的恰恰是几个字符串字面量。
 */
const test = require('node:test')
const assert = require('node:assert/strict')

const shared = require('../shared.cjs')

const { ALLOWED_PERMISSIONS, isAllowedPermission } = shared

test('★ 回归：clipboard-sanitized-write 必须放行，否则桌面端全部复制失效', () => {
  assert.equal(isAllowedPermission('clipboard-sanitized-write'), true)
})

test('读剪贴板仍然放行（粘贴链接、读取分享码要用）', () => {
  assert.equal(isAllowedPermission('clipboard-read'), true)
})

test('麦克风与全屏放行', () => {
  assert.equal(isAllowedPermission('media'), true)
  assert.equal(isAllowedPermission('fullscreen'), true)
})

test('★ 白名单之外一律拒绝：不能为了修复制就把权限面整体放开', () => {
  for (const permission of [
    'geolocation',
    'notifications',
    'midi',
    'midiSysex',
    'pointerLock',
    'idle-detection',
    'openExternal',
    'unknown',
    '',
  ]) {
    assert.equal(isAllowedPermission(permission), false, `${permission} 不该被放行`)
  }
})

test('白名单写成数组且无重复项（判定函数与导出的常量必须同源）', () => {
  assert.ok(Array.isArray(ALLOWED_PERMISSIONS))
  assert.equal(new Set(ALLOWED_PERMISSIONS).size, ALLOWED_PERMISSIONS.length)
  for (const permission of ALLOWED_PERMISSIONS) {
    assert.equal(isAllowedPermission(permission), true)
  }
})
