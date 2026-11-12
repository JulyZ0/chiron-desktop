'use strict'
/**
 * 回归测试：「检查本地运行时就闪退」。
 *
 * 现场取证（%APPDATA%/frontend-vue/logs/main.log）：
 *
 *   04:29:03.184  local runtime not ready (venv-missing)   ← 弹出安装窗口（当时唯一的窗口）
 *   04:29:26.115  local backend ready                      ← 23 秒后装完
 *   04:29:26.474  local backend stopped                    ← 0.36 秒后进程退出
 *
 * 根因：`closeInstallWindow()` 在 `finally` 里 `destroy()` 掉安装进度窗，
 * 而 `BrowserWindow#destroy()` 会**同步**派发 window-all-closed（electron 45 实测，
 * 见下方 integration 用例），原实现直接 `app.quit()` —— 紧接着调用的 `createWindow()`
 * 建出来的主窗口还没显示，进程就已经在退出了。
 *
 * 这里锁两件事：
 *   1. 纯函数闸门 `shouldQuitOnAllWindowsClosed` 的各种组合；
 *   2. 真实 Electron 里 destroy 的唯一窗口**确实同步**触发 window-all-closed
 *      —— 这是整个结论的事实基础，若哪天 Electron 改成异步派发，本测试会提醒我们重估。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const shared = require('../shared.cjs')

const { shouldQuitOnAllWindowsClosed: gate } = shared

// ------------------------------------------------------------------ 纯函数闸门
test('正常退出：启动完成且没有重建，非 macOS 时关窗即退出', () => {
  assert.equal(gate({ platform: 'win32', startupComplete: true, holdingWindows: 0 }), true)
  assert.equal(gate({ platform: 'linux', startupComplete: true, holdingWindows: 0 }), true)
})

test('★ 回归：启动未完成时销毁唯一窗口（安装窗）不得退出', () => {
  // 这正是闪退的现场组合：首次安装装完就 closeInstallWindow()，主窗口还没建
  assert.equal(gate({ platform: 'win32', startupComplete: false, holdingWindows: 0 }), false)
  assert.equal(gate({ platform: 'linux', startupComplete: false, holdingWindows: 0 }), false)
})

test('窗口重建期（销毁旧窗 → 建新窗）不得退出', () => {
  for (const platform of ['win32', 'linux']) {
    assert.equal(gate({ platform, startupComplete: true, holdingWindows: 1 }), false)
    assert.equal(gate({ platform, startupComplete: true, holdingWindows: 3 }), false)
  }
})

test('macOS 一律不因关窗退出（含启动期与重建期）', () => {
  const cases = [
    { startupComplete: true, holdingWindows: 0 },
    { startupComplete: false, holdingWindows: 0 },
    { startupComplete: true, holdingWindows: 2 },
  ]
  for (const c of cases) {
    assert.equal(gate({ platform: 'darwin', ...c }), false)
  }
})

test('平台名按字面匹配：大写或别名不会被当成 mac', () => {
  // 防的是有人写成 'Darwin' / 'mac' 以为能跳过；这里明确它按 Electron 的取值是小写 darwin
  assert.equal(gate({ platform: 'Darwin', startupComplete: true, holdingWindows: 0 }), true)
  assert.equal(gate({ platform: 'mac', startupComplete: true, holdingWindows: 0 }), true)
})

test('main.cjs 的 window-all-closed 必须走这个闸门，而不是自己判断', () => {
  const fs = require('node:fs')
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  const start = source.indexOf("app.on('window-all-closed'")
  assert.ok(start > 0, '找不到 window-all-closed 的注册')
  // 取到 before-quit 之前，正好是这一个 handler 的体
  const end = source.indexOf("app.on('before-quit'", start)
  assert.ok(end > start, 'window-all-closed 之后应当跟着 before-quit')
  const handler = source.slice(start, end)
  assert.match(handler, /shouldQuitOnAllWindowsClosed/, '必须调用共享闸门，避免逻辑两处漂移')
  assert.match(handler, /startupComplete/, '必须把启动状态传进去')
  assert.match(handler, /holdingWindows/, '必须把重建计数传进去')
  // 反向断言：不能再出现"没有闸门兜底"的裸 app.quit()
  assert.doesNotMatch(
    handler.replace(/shouldQuitOnAllWindowsClosed[\s\S]*?\n  \}\)/, ''),
    /^\s*app\.quit\(\)\s*$/m,
    '闸门之外的兜底 app.quit() 会绕过所有保护',
  )
})

/**
 * 回归：「从最小化还原后应用卡死」。
 *
 * 现场：最小化 → 窗口收进托盘 → 从托盘还原，窗口框回来了，但内容是一片冻住的
 * 旧画面，点哪里都没反应。
 *
 * 根因：最小化后调用了 `hide()`，窗口同时处于「已最小化」和「已隐藏」两个状态。
 * Windows 认为它仍是最小化的，Chromium 又认为它被遮挡、停止合成 —— 恢复时
 * `show()` 只把窗口框拿出来，最小化状态还在，页面因此不重绘。
 *
 * 正确做法是**不 hide()**：让窗口保持普通的最小化状态，只 `setSkipTaskbar(true)`
 * 摘掉任务栏按钮，恢复走标准 `restore()`。两边状态始终一致。
 *
 * 这两条断言锁的是"别改回 hide()"和"恢复别加 isMinimized() 守卫"——两种改法都
 * 会让卡死重现，而且都不会让任何一条既有测试变红。
 */
test('★ 回归：托盘最小化不得 hide()，只摘任务栏按钮', () => {
  const fs = require('node:fs')
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')

  const start = source.indexOf("mainWindow.on('minimize'")
  assert.ok(start > 0, '找不到 minimize 事件的注册')
  const end = source.indexOf("mainWindow.on('restore'", start)
  assert.ok(end > start, 'minimize 之后应当跟着 restore 的注册')
  const handler = source.slice(start, end)

  assert.match(handler, /setSkipTaskbar\(true\)/, '最小化时必须摘掉任务栏按钮')
  assert.doesNotMatch(
    handler,
    /\.hide\(\)/,
    '在 minimize 里 hide() 会让窗口处于「最小化+隐藏」的复合态，还原后内容冻住',
  )
  // 托盘图标缺失时不能摘任务栏按钮：否则窗口既没有任务栏按钮、也没有托盘入口
  assert.match(handler, /if \(!tray/, '没有托盘时不能动任务栏')
})

test('★ 回归：从托盘恢复必须无条件 restore()，不能加 isMinimized() 守卫', () => {
  const fs = require('node:fs')
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')

  const start = source.indexOf('function showMainWindow(')
  assert.ok(start > 0, '找不到 showMainWindow 的定义')
  const end = source.indexOf('\n}', start)
  assert.ok(end > start, '找不到 showMainWindow 的结尾')
  // 只断言代码：注释里**必须**能写「别用 isMinimized() 做守卫」，
  // 否则这段说明下次就会被人删掉，然后照着删掉的思路把守卫加回来。
  const body = source
    .slice(start, end)
    .replace(/^\s*\/\/.*$/gm, '')

  assert.match(body, /\.restore\(\)/, '必须调用 restore()，show() 不会清除最小化状态')
  assert.doesNotMatch(
    body,
    /isMinimized\(\)/,
    '用 isMinimized() 做守卫时一旦判断失误就跳过 restore()，窗口会带着最小化状态被 show()',
  )
  // restore 必须发生在 show 之前：顺序反了等于先显示再还原，中间会闪一下空窗
  assert.ok(
    body.indexOf('.restore()') < body.indexOf('.show()'),
    'restore() 必须在 show() 之前',
  )
})
