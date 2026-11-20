'use strict'
/**
 * 打包冒烟（静态断言）。
 *
 * 这一组用例守的是"**安装包看起来是好的、装完却少一块能力**"的静默失效：
 * electron-builder 的 extraResources 匹配不到只 warn、前端路由与网关回退
 * 各写一份也不会互相报错。全部断言都只读源码/配置，不启动 Electron、不打包，
 * 因此可以在任何 CI job（含纯 Node）里跑。
 *
 * 覆盖四簇：
 *   1. `dist/` 额外进 `resources/web` —— 网关（独立 Go 进程）打不开 asar，
 *      少了这一份就没有任何人能打开分享链接；
 *   2. 壳层算路径、`CHIRON_WEB_ROOT` 注入 spawn 环境 —— 两侧路径必须一致；
 *   3. 网关的 SPA 回退（`GET /` → index.html）—— `/s/:token` 这类客户端
 *      路由在网关托管下必须能直接打开，而不是 404；
 *   4. 前端路由里 `/s/:token`、`/knowledge`、`/admin/deps` 仍在 —— 分享页、
 *      知识库与依赖页可达。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const ELECTRON_DIR = path.resolve(__dirname, '..')
const FRONTEND_DIR = path.resolve(ELECTRON_DIR, '..')
const REPO_ROOT = path.resolve(FRONTEND_DIR, '..')

function read(relPath) {
  return fs.readFileSync(path.join(REPO_ROOT, relPath), 'utf8')
}

test('electron-builder.yml：extraResources 把 dist/ 额外复制到 resources/web', () => {
  const yml = read('frontend-vue/electron-builder.yml')
  const extraIdx = yml.indexOf('extraResources:')
  assert.ok(extraIdx >= 0, 'electron-builder.yml 必须有 extraResources 段')

  const extra = yml.slice(extraIdx)
  const entry = /-\s*from:\s*dist\s*\n\s*to:\s*web\b/.exec(extra)
  assert.ok(
    entry,
    'extraResources 必须含 `from: dist / to: web`：网关打不开 asar，缺这一份分享链接永远 404',
  )
  // 注意判据取自 extraResources 之后的文本：写进 files 只会进 asar，网关是独立进程读不到
})

test('main.cjs：webRoot 由 resolveWebRoot() 算出并交给本地栈', () => {
  const main = fs.readFileSync(path.join(ELECTRON_DIR, 'main.cjs'), 'utf8')
  assert.match(main, /function resolveWebRoot\s*\(/, 'main.cjs 必须有 resolveWebRoot()')
  assert.match(
    main,
    /webRoot:\s*resolveWebRoot\(\)/,
    'startLocalStack 必须收到 webRoot: resolveWebRoot()',
  )
  // 实现只有一份（local-stack.cjs 的 resolver）：壳层把 resourcesPath 与开发态 dist
  // 交给它算，不再自己拼 `resources/web` —— 两处各拼一份就会静默漂移。
  assert.match(
    main,
    /stack\.resolveWebRoot\(\{[\s\S]{0,120}process\.resourcesPath[\s\S]{0,120}devDist/,
    'main.cjs 必须委托 local-stack.resolveWebRoot（打包态 resourcesPath / 开发态 dist）',
  )
  assert.match(main, /return ''/, '拿不到 resolver 时必须返回空串而不是抛错')
})

test('local-stack.cjs：webRoot 解析只有一份实现（打包态 resources/web、开发态 dist）', () => {
  const stack = fs.readFileSync(path.join(ELECTRON_DIR, 'local-stack.cjs'), 'utf8')
  assert.match(stack, /function resolveWebRoot\s*\(/, '必须导出 resolveWebRoot 作为唯一实现')
  assert.match(stack, /path\.join\(resourcesDir,\s*'web'\)/, '打包态必须定位 <resources>/web')
  assert.match(stack, /index\.html/, '必须检查 index.html（目录存在但没产物等于没托管）')
  assert.match(stack, /resolveResourcesDir\(options\)/, '资产根解析也必须走同一入口')
})

test('local-stack.cjs：把 webRoot 注入网关 spawn 环境的 CHIRON_WEB_ROOT', () => {
  const stack = fs.readFileSync(path.join(ELECTRON_DIR, 'local-stack.cjs'), 'utf8')
  assert.match(
    stack,
    /env\.CHIRON_WEB_ROOT\s*=\s*webRoot/,
    'spawn 环境必须注入 CHIRON_WEB_ROOT（网关只认这个环境变量）',
  )
  assert.match(stack, /if\s*\(\s*webRoot\s*\)/, '空串代表不托管，不能注入空路径')
})

test('internal/api/web_static.go：存在 mountSPA 与 GET / 的 SPA 回退', () => {
  const goSrc = read('internal/api/web_static.go')
  assert.match(goSrc, /func mountSPA\s*\(/, '必须导出 mountSPA（装配处一行接上）')
  assert.match(
    goSrc,
    /mux\.Handle\("GET \/"/,
    '必须以 `GET /` 注册兜底：/s/<token>、/knowledge 等客户端路由才有人接',
  )
  assert.match(goSrc, /index\.html/, '回退必须返回 index.html')
  assert.match(goSrc, /SPA/, '文件里必须还写着 SPA 兜底语义（防止被改成纯静态）')
  // API 前缀不能被 SPA 吞掉：否则 /v1/* 未命中会返回 HTML 而不是 JSON 错误
  assert.match(goSrc, /isReservedAPIPath/, 'SPA 兜底必须排除 API 前缀')
})

test('前端路由：分享页 /s/:token、知识库 /knowledge、依赖页 /admin/deps 可达', () => {
  const router = read('frontend-vue/src/router/index.ts')
  assert.match(router, /path:\s*'\/s\/:token'/, '分享页路由 /s/:token 必须存在')
  assert.match(router, /path:\s*'\/knowledge'/, '知识库路由 /knowledge 必须存在')
  // admin 子路由 path: 'deps' 拼成 /admin/deps（父级 path 是 '/admin'）
  assert.match(router, /path:\s*'deps'/, "/admin/deps 的 admin 子路由 path: 'deps' 必须存在")
  assert.match(router, /name:\s*'AdminDeps'/, 'AdminDeps 命名路由必须存在')
  assert.match(router, /DepsView\.vue/, '依赖页组件必须是懒加载的 DepsView.vue')
})
