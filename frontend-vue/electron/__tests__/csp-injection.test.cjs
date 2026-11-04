'use strict'
/**
 * 回归测试：「改用本地模式」按钮点了没有任何效果。
 *
 * 现场症状：连接配置页（electron/setup.html）上三个按钮全都不响应，
 * 连「测试连接」都不报错 —— 因为**事件监听根本没绑上**。
 *
 * 根因：主进程给所有 file:// 主文档无条件注入响应头 CSP（buildCsp），
 * 而 setup / install / error 三张辅助页各自带 meta CSP（default-src 'none'）
 * 并用**内联 <script>** 挂事件。多条 CSP 取交集，buildCsp 的 script-src
 * 只有 'self' file: 'wasm-unsafe-eval'、没有 'unsafe-inline'，
 * 于是内联脚本被整段拒绝执行。
 *
 * 这里锁两件事：
 *   1. 纯函数闸门 shouldInjectHeaderCsp 的各种组合（快速、无 Electron 依赖）；
 *   2. 真实 Electron 里这两种注入结果**确实不同** —— 这是整个结论的事实基础，
 *      若哪天 Electron 改成"响应头 CSP 覆盖/合并"语义，本测试会提醒我们重估。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs')
const { spawnSync } = require('node:child_process')

const shared = require('../shared.cjs')

// __dirname 是 frontend-vue/electron/__tests__，上两级才是 frontend-vue
const FRONTEND_ROOT = path.join(__dirname, '..', '..')
const ELECTRON = path.join(FRONTEND_ROOT, 'node_modules', 'electron', 'dist', 'electron.exe')
const ELECTRON_BIN = path.join(FRONTEND_ROOT, 'node_modules', '.bin', 'electron')

/** dist/index.html（SPA 首页）的 file:// 形态 */
function spaUrl() {
  return 'file:///' + path.join(FRONTEND_ROOT, 'dist', 'index.html').split(path.sep).join('/')
}
const AUX_PAGES = ['setup.html', 'install.html', 'error.html']

function fileUrl(...segs) {
  // 主进程里 details.url 就是 file:///D:/.../setup.html 这种形态
  // 用 path.sep 分段拼接，避免在源码里写反斜杠字面量
  return 'file:///' + path.join(__dirname, '..', ...segs).split(path.sep).join('/')
}

test.describe('响应头 CSP 注入判据（shared.shouldInjectHeaderCsp）', () => {
  test.it('★ 三张辅助页一律不注入：它们自带 meta CSP 且依赖内联脚本', () => {
    for (const page of AUX_PAGES) {
      assert.equal(
        shared.shouldInjectHeaderCsp({ url: fileUrl(page), resourceType: 'mainFrame' }),
        false,
        `${page} 不应被注入响应头 CSP，否则内联脚本会被拒`
      )
    }
  })

  test.it('★ 辅助页带 query / hash 时仍不注入', () => {
    // error.html 实际就是这么加载的：loadFile(..., { query: { code, desc, url } })
    assert.equal(
      shared.shouldInjectHeaderCsp({
        url: fileUrl('error.html') + '?code=1&desc=x',
        resourceType: 'mainFrame',
      }),
      false
    )
    assert.equal(
      shared.shouldInjectHeaderCsp({ url: fileUrl('setup.html') + '#top', resourceType: 'mainFrame' }),
      false
    )
  })

  test.it('★ SPA 首页仍注入：文件页的 XSS 外传防线不因此变松', () => {
    assert.match(spaUrl(), /\/dist\/index\.html$/)
    assert.equal(shared.shouldInjectHeaderCsp({ url: spaUrl(), resourceType: 'mainFrame' }), true)
  })

  test.it('非 file:// 文档不注入：dev 服务器/远程页面的 CSP 不该由这里代管', () => {
    assert.equal(
      shared.shouldInjectHeaderCsp({ url: 'http://localhost:5173/index.html', resourceType: 'mainFrame' }),
      false
    )
    assert.equal(shared.shouldInjectHeaderCsp({ url: '', resourceType: 'mainFrame' }), false)
    assert.equal(shared.shouldInjectHeaderCsp({}), false)
  })

  test.it('子资源即使落在辅助页目录也不注入（判据只认主文档）', () => {
    assert.equal(
      shared.shouldInjectHeaderCsp({ url: fileUrl('setup.html'), resourceType: 'subFrame' }),
      false
    )
  })
})

test.describe('真实 Electron 复现：辅助页的内联脚本到底跑没跑', () => {
  const probeDir = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'chiron-csp-'))
  const probeFile = path.join(probeDir, 'probe.cjs')

  // 探针只做一件事：按 shouldInjectHeaderCsp 决定是否注入，然后打开 setup.html，
  // 用"输入合法地址后 save 是否解锁"判断 sync() 跑过没有。
  fs.writeFileSync(
    probeFile,
    `
    const { app, BrowserWindow, session } = require('electron')
    const shared = require(${JSON.stringify(path.join(__dirname, '..', 'shared.cjs'))})
    const SETUP = ${JSON.stringify(path.join(__dirname, '..', 'setup.html'))}
    app.whenReady().then(async () => {
      const ses = session.fromPartition('csp-probe')
      ses.webRequest.onHeadersReceived((d, cb) => {
        const rh = d.responseHeaders || {}
        if (shared.shouldInjectHeaderCsp(d)) {
          rh['Content-Security-Policy'] = ["default-src 'self' file:; script-src 'self' file: 'wasm-unsafe-eval'"]
        }
        cb({ responseHeaders: rh })
      })
      const win = new BrowserWindow({ show: false, webPreferences: { partition: 'csp-probe', contextIsolation: true } })
      await win.loadFile(SETUP)
      await new Promise(r => setTimeout(r, 800))
      const ok = await win.webContents.executeJavaScript(\`(() => {
        const u = document.getElementById('url')
        const s = document.getElementById('save')
        if (!u || !s) return null
        u.value = 'http://127.0.0.1:8080'
        u.dispatchEvent(new Event('input'))
        return s.disabled === false
      })()\`)
      console.log('PROBE_RESULT=' + ok)
      app.exit(0)
    })
    `,
    'utf8'
  )

  test.it('★ setup.html 的内联脚本确实在执行（否则按钮点了没反应）', { timeout: 60000 }, () => {
    const exe = fs.existsSync(ELECTRON) ? ELECTRON : ELECTRON_BIN
    if (!fs.existsSync(exe)) return // 没装 electron 的机器跳过，不把环境缺失当成失败
    const r = spawnSync(exe, [probeFile, '--no-sandbox'], {
      encoding: 'utf8',
      timeout: 50000,
      env: { ...process.env, ELECTRON_ENABLE_LOGGING: '0' },
    })
    const out = String(r.stdout || '') + String(r.stderr || '')
    const m = out.match(/PROBE_RESULT=(true|false|null)/)
    assert.ok(m, `探针没有输出结果：${out.slice(-800)}`)
    assert.notEqual(m[1], 'null', 'setup.html 缺少 #url / #save，页面结构变了')
    assert.equal(m[1], 'true', '内联脚本未执行：响应头 CSP 把 setup.html 的按钮全废了')
  })
})
