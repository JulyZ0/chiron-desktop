/**
 * Chiron Desktop — Electron 主进程
 *
 * 形态：双模式桌面壳。渲染层始终是已构建的前端（dist/），后端按模式分流：
 *   local —— 本机后端（embedded-postgres + 本地 API，由 components.cjs 负责拉起）
 *   cloud —— 已废除（当前版本与云端服务端不兼容），不再说明
 * 契约见 docs/desktop-local/contracts.md：config.json 是模式真相，两个模式各用一个
 * Electron session partition，切换模式时清掉离开模式的会话数据并重启。
 *
 * 为什么选 Electron 而不是 Tauri/Wails：本仓样式对现代 CSS 是硬依赖
 * （`color-mix` 86 处、`@container` 4 处、`backdrop-filter` 25 处），而
 * Tauri/Wails 在 Linux 上借用系统 WebKitGTK —— Ubuntu 22.04 的 2.36 不支持
 * color-mix，主题阴影会静默失效。Electron 自带 Chromium，开发环境所见即用户所见。
 *
 * 启动分流：
 *   local 且本地后端拉起成功 → 主界面
 *   local 且本地后端未安装/启动失败 → 给重试 / 切回云端 / 退出三个选择，仍打不开则落到 error.html
 *   老配置（有 apiUrl 但没有 mode） → 先弹一次迁移提示，默认本地，apiUrl 保留
 *
 * 安全基线：contextIsolation 开、nodeIntegration 关、preload 只暴露明确的几个动作，
 * 外部链接一律交给系统浏览器。
 */
const { app, BrowserWindow, shell, Menu, ipcMain, session, dialog } = require('electron')
const fs = require('node:fs')
const crypto = require('node:crypto')
const path = require('node:path')
const { fileURLToPath } = require('node:url')
const {
  resolveApiUrl,
  resolveMode,
  readDeviceSettings,
  saveApiUrl,
  saveDeviceSettings,
  saveMode,
  markApiUrlPending,
  consumeApiUrlPending,
  probe,
} = require('./config.cjs')
const shared = require('./shared.cjs')
const dataDirAcl = require('./data-dir-acl.cjs')
const windowsHello = require('./windows-hello.cjs')
const {
  API_URL_ARG_PREFIX,
  MODE_ARG_PREFIX,
  DEFAULT_DEV_URL,
  partitionFor,
  shouldQuitOnAllWindowsClosed,
  resolveStartupWindow,
} = shared
/**
 * 本地设备 token 的注入前缀（`--chiron-device-token=<token>`）。
 *
 * 首选 shared.cjs 里的同名常量（契约 §3.1 要求共享常量集中在那里），本 worktree 没有
 * 改 shared.cjs 的授权，所以缺失时用这里的兜底字面量 —— preload.cjs 用同一套写法，
 * 两边只要有一处把常量搬到 shared.cjs 就会同时生效。
 */
const DEVICE_TOKEN_ARG_PREFIX = shared.DEVICE_TOKEN_ARG_PREFIX || '--chiron-device-token='
const { readToken, writeToken, clearToken } = require('./token-store.cjs')
const { switchMode } = require('./mode-switch.cjs')

/** 开发模式：`electron . --dev` 时连 Vite dev server，否则加载 dist/ */
const isDev = process.argv.includes('--dev')
const DEV_URL = process.env.CHIRON_DEV_URL || DEFAULT_DEV_URL

/** 窗口底色取 --bg-page 的浅色值：主进程拿不到 CSS 变量，只能写字面值。 */
const WINDOW_BACKGROUND = '#f9f9f7'

let mainWindow = null

/** 当前窗口使用的 API 地址；CORS 响应头只补给它，不碰其它站点 */
let currentApiUrl = ''

/** 「连接设置…」菜单强制进配置页（即使已有地址），见 relaunchIntoSetup */
let forceSetup = false

/** 本次运行的运行模式（whenReady 时定一次；切换模式一律走重启） */
let currentMode = 'local'

/**
 * 本地后端状态：`{ ok, baseUrl, token, dataDir, via, reason, message }`，由 startLocalBackend 填充。
 * 本地模式下设备 token 由 Go 网关注入，这里留空（见 tryStartGoStack 的返回值）。
 *
 * `via` 是**进程归属**：本地模式恒为 `'go-stack'` —— 网关/引擎/引导器（PG、Redis）
 * 都归 local-stack.cjs。停止时必须按它找 owner，停错那套等于一个进程都没回收
 * （见 stopLocalBackend）。
 */
let localBackend = blankLocalBackend()

/** 空态：停过之后不能把死 baseUrl 和废 token 留给后面重建的窗口 */
function blankLocalBackend() {
  return { ok: false, baseUrl: '', token: '', dataDir: '', via: '', reason: 'not-started', message: '' }
}

/** 已经注册过 webRequest / 权限 / 下载监听的 session（重建窗口时不能重复注册） */
const configuredSessions = new WeakSet()

/** 本地后端只停一次：before-quit 与模式切换都会触发停止 */
let localBackendStopped = false

/**
 * 启动流程（whenReady 里那串 await）是否已经走到建主窗口这一步。
 *
 * 存在的唯一原因：首次启动时安装进度窗是**当时唯一的窗口**，而 `destroy()`
 * 会**同步**派发 window-all-closed（electron 45 实测确认，不是"下个 tick 才发"）。
 * 不在启动期间拦一下，closeInstallWindow() 会立刻把 app.quit() 排进队列，
 * 紧接着建好的主窗口还没显示，进程就退了 —— 用户看到的就是
 * "检查本地运行时就闪退"。
 */
let startupComplete = false

/**
 * 正在做的"先销毁旧窗口、再建新窗口"的同步重建次数。
 *
 * relaunchIntoApp / relaunchIntoSetup 都是 `destroy()` 紧接着 `createWindow()`，
 * 和启动期同一个坑：destroy 同步触发 window-all-closed。计数 >0 期间不允许因此退出。
 */
let holdingWindows = 0

/** 把"销毁 + 重建"包成原子操作：中间那一下空窗口不触发退出 */
function holdingWindowQuota(fn) {
  holdingWindows += 1
  try {
    return fn()
  } finally {
    holdingWindows -= 1
  }
}

/**
 * 首次启动安装进度窗口。安装发生在 createWindow 之前，没有窗口就没有任何东西
 * 能显示进度 —— 用户只会看到"点了图标没反应"。装完立刻关掉，主界面照旧走 createWindow。
 */
let installWindow = null

/** 安装进度推送通道名，必须与 preload.cjs 的订阅名一致 */
const RUNTIME_STAGE_CHANNEL = 'desktop:runtime-stage'

const APP_ID = 'com.chiron.agent'
const APP_ROOT = path.resolve(__dirname, '..')
const DEV_ORIGIN = (() => {
  try {
    return new URL(DEV_URL).origin
  } catch {
    return ''
  }
})()

/** 本地诊断日志：不依赖网络，崩溃/加载失败时至少能给用户一个文件 */
function logMain(level, message) {
  const line = `${new Date().toISOString()} [${level}] ${message}\n`
  try {
    const file = path.join(app.getPath('userData'), 'logs', 'main.log')
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.appendFileSync(file, line)
  } catch {
    /* 日志失败不能影响主流程 */
  }
  if (level === 'error') console.error(message)
}

/**
 * 允许的页面地址：本地 app 根目录下的 file://，或开发模式的 Vite origin。
 * 导航限制与 IPC sender 校验共用这一份判断，避免两处规则漂移。
 */
function isTrustedUrl(rawUrl) {
  try {
    const url = new URL(rawUrl)
    if (url.protocol === 'file:') {
      const filePath = path.resolve(fileURLToPath(url))
      return filePath === APP_ROOT || filePath.startsWith(APP_ROOT + path.sep)
    }
    return isDev && url.origin === DEV_ORIGIN
  } catch {
    return false
  }
}

/**
 * file:// 页面没有 CSP 时 Electron 会直接报安全警告；XSS 也能任意外连。
 * 只放行本地资源与用户自己配置的 API/WS origin，其余 connect 一律拒绝。
 */
function buildCsp() {
  const local = "'self' file:"
  const connect = ["'self'", 'file:']
  if (currentApiUrl) {
    try {
      const api = new URL(currentApiUrl)
      connect.push(api.origin)
      connect.push(`${api.protocol === 'https:' ? 'wss' : 'ws'}://${api.host}`)
    } catch {
      /* 地址已在 config.cjs 校验；这里失败就不额外放行 */
    }
  }
  return [
    `default-src ${local}`,
    `script-src ${local} 'wasm-unsafe-eval'`,
    `style-src ${local} 'unsafe-inline'`,
    `img-src ${local} data: blob:`,
    `media-src ${local} data: blob:`,
    `font-src ${local} data:`,
    `connect-src ${connect.join(' ')}`,
    `worker-src ${local} blob:`,
    `child-src ${local} blob:`,
    `frame-src ${local}`,
    "object-src 'none'",
    "base-uri 'none'",
    `form-action ${local}`,
  ].join('; ')
}

/** 主文档加载失败时切到本地错误页，而不是留一个空白窗口 */
function showLoadError(code, description, failedUrl) {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (code === -3) return // ERR_ABORTED：用户主动取消导航
  if (failedUrl.includes('/error.html')) return
  logMain('error', `load failed (${code}) ${description} ${failedUrl}`)
  mainWindow
    .loadFile(path.join(__dirname, 'error.html'), {
      query: {
        code: String(code),
        desc: description || '',
        url: failedUrl || '',
        retry: failedUrl || '',
      },
    })
    .catch(err => logMain('error', `error page load failed: ${err.message}`))
}

process.on('uncaughtException', err => logMain('error', `uncaughtException: ${err?.stack || err}`))
process.on('unhandledRejection', reason => logMain('error', `unhandledRejection: ${reason}`))

/** IPC 只接受来自应用自身页面（或开发服务器）的调用 */
function assertTrustedSender(event) {
  const frameUrl = event.senderFrame?.url || event.sender.getURL()
  if (!isTrustedUrl(frameUrl)) {
    logMain('warn', `blocked IPC from ${frameUrl}`)
    throw new Error('forbidden')
  }
}

/**
 * 可选模块接线：components.cjs / secrets.cjs / inference-proxy.cjs 由另一个
 * worktree 实现并按需发布。本文件只 require + try/catch 接线，绝不在这里写它们的实现，
 * 模块不存在时安静降级（未安装本地组件是用户的正常状态）。
 */
const optionalModules = new Map()
function loadOptionalModule(name) {
  if (optionalModules.has(name)) return optionalModules.get(name)
  let mod = null
  try {
    mod = require(name)
  } catch (err) {
    logMain('info', `optional module ${name} unavailable: ${err?.message || err}`)
  }
  optionalModules.set(name, mod)
  return mod
}

/**
 * 启动本地后端（mode=local）。
 *
 * 启动回调由 W4 的 components.cjs 提供；这里全部走可选探测 —— 模块缺失、接口缺失、
 * 启动抛错都要变成一条可读状态再显示本地错误页，而不是让主进程崩掉。
 *
 * **返回值的两条语义必须分清**（本轮改的就是这条）：
 *   - `null`  = 这条链路**不适用**（壳里没有 local-stack / components，或这台机器
 *     根本没有网关二进制，或引导器运行时还没装）→ 调用方才允许走 desktop-local core；
 *   - **抛错** = 真的试过并且失败了 → 绝不回退。回退会拉起第二套 PG/Redis，
 *     还会把"引擎没起来"这种必须让用户知道的细节全部丢掉。
 * 过去这里把抛错也吞成 null，于是打包态每次引擎失败都变成"界面能开但半数是坏的"。
 */
async function tryStartGoStack(options = {}) {
  const stack = loadOptionalModule('./local-stack.cjs')
  const components = loadOptionalModule('./components.cjs')
  if (!stack || !components) return null

  const gatewayExe = resolveGatewayExe()
  if (!gatewayExe) {
    logMain('info', '本地网关不可用（未找到 chiron.exe），退回 desktop-local core')
    return null
  }

  let launch
  try {
    // 用 prepareCoreLaunch 而不是裸 resolveCoreLaunch：全新安装时 venv 还不存在，
    // 后者必然抛错，那时若直接回退到老路径，用户第一次启动拿到的是只有 desktop-local
    // 的**半套**界面（网关那侧的 skills/graphs/kb/workflows 全缺），而且引擎依赖
    // 也永远轮不到装。这里先按需装好 core，装完仍然拿不到启动信息才算这条链路不适用。
    launch = await components.prepareCoreLaunch(options)
  } catch (err) {
    // 运行时装不起来 / 安装包残缺：这是首启的正常状态，交给旧路径去触发安装流程
    logMain('info', `引导器运行时不可用：${err?.message || err}`)
    return null
  }

  const dataDir = options.dataDir
    ? String(options.dataDir)
    : path.join(app.getPath('userData'), 'local')

  // 引擎的依赖是**按需**装的（用到才装，见 components.ensureEngineRuntime），
  // 进度沿用同一条推送通道：首启弹了安装窗的话，这一段就显示在同一个窗里。
  // 装失败会抛 —— 由 startLocalBackend 转成可读状态，这里不吞。
  const engineLaunch = await prepareEngine(components, options)

  try {
    const port =
      Number(options.gatewayPort) > 0 ? Number(options.gatewayPort) : await components.pickFreePort()

    const started = await stack.startLocalStack({
      launcher: { command: launch.command, cwd: launch.cwd },
      dataDir,
      exe: gatewayExe,
      port,
      appSecret: ensureAppSecret(dataDir),
      engine: engineLaunch || undefined,
      onEngineExit: notifyEngineExit,
    })

    logMain(
      'info',
      `本地网关就绪 ${started.baseUrl}（pg=${started.launcher?.pgBackend} redis=${started.launcher?.redisBackend}）`,
    )
    // PG、Redis、引擎都是强依赖，能走到这里说明三者都起来了（缺一个 startLocalStack 就会抛）
    logMain('info', `python-engine 就绪 ${started.engine?.address || '（接管既有实例）'}`)
    // 新一轮运行重新计一次提示额度：上一轮崩过不该让这一轮的崩溃从此无声
    engineExitNotified = false
    return {
      ok: true,
      baseUrl: started.baseUrl,
      token: '',
      dataDir,
      // 进程归属标记：PG/Redis/引擎/网关由 local-stack.cjs 拉起，停止也必须走它
      //（见 stopLocalBackend 的注释 —— 停错那套等于什么都没停）。
      via: 'go-stack',
      reason: '',
      message: '',
    }
  } catch (err) {
    const message = err?.message || String(err)
    logMain('error', `本地网关启动失败：${message}`)
    // 带 reason 往上抛：startLocalBackend 会把它放进返回状态，恢复对话框与错误页
    // 才有的可显示；只留一句"启动失败"等于让用户对着黑箱猜。
    const error = err instanceof Error ? err : new Error(message)
    error.reason = error.reason || 'gateway-stack-failed'
    throw error
  }
}

/**
 * 找 Go 网关可执行文件。
 *
 * 打包态在 `resources/chiron.exe`（见 electron-builder.yml 的 extraResources），
 * 开发态在仓库根。找不到就返回空串，由调用方退回旧路径。
 */
function resolveGatewayExe() {
  const exeName = process.platform === 'win32' ? 'chiron.exe' : 'chiron'
  const candidates = []
  if (process.resourcesPath) candidates.push(path.join(process.resourcesPath, exeName))
  candidates.push(path.join(__dirname, '..', '..', exeName))
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  return ''
}

/**
 * 引擎就位：**用到才装**，然后给出启动信息。
 *
 * 解释器选择与依赖安装都在 `components.cjs`（`ensureEngineRuntime` /
 * `resolveEngineLaunch`）：那里才有"随包 vs 开发态"的唯一判据，写在两处必然漂移。
 * 壳这一层只做两件事 —— 起栈之前把依赖装上（进度照样推给进度窗），
 * 以及把装不出来时的原因**如实上抛**（不吞掉、不回退）。
 *
 * 返回 null 只有一种含义：这台机器上没有可用的 python-engine（既没随包源码也没有
 * 开发态源码）。网关随后会因"引擎未启动"整体失败，而不是悄悄少一块功能。
 */
async function prepareEngine(components, options) {
  await components.ensureEngineRuntime(options)
  return components.resolveEngineLaunch({ userDataPath: app.getPath('userData') })
}

/**
 * 取（或首次生成）网关的 APP_SECRET。
 *
 * 必须**跨重启稳定**：JWT_SECRET 与 INTERNAL_TOKEN 都由它派生，每次启动换新密钥
 * 会让所有已登录会话失效、用户被反复踢下线。
 * 长度 32 是网关的硬要求（`config/config.go` 强制），生成的 hex 有 64 字符。
 */
function ensureAppSecret(dataDir) {
  const file = path.join(dataDir, 'auth', 'app-secret')
  try {
    const existing = fs.readFileSync(file, 'utf8').trim()
    if (existing.length >= 32) return existing
  } catch {
    /* 不存在则生成 */
  }
  const secret = crypto.randomBytes(32).toString('hex')
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, secret, { mode: 0o600 })
  } catch (err) {
    logMain('warn', `无法持久化 APP_SECRET：${err?.message || err}`)
  }
  return secret
}

async function startLocalBackend(options = {}) {
  // 显式守卫：云端模式下**一律不启动**本地 core（以及它拉起的 PG / redis）。
  // 调用点目前都在 `currentMode === 'local'` 分支里，但这条不变量不能只靠调用方自律 ——
  // 以后新增一处调用就可能让云端用户白起一套数据库。放在这里，误用会立刻变成可读状态。
  if (currentMode !== 'local') {
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: 'not-local',
      message: `当前是${currentMode}模式，不启动本地后端`,
    }
  }
  const components = loadOptionalModule('./components.cjs')
  if (!components) {
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: 'components-missing',
      message: '本地组件未随此版本安装',
    }
  }
  // 本地模式**只有一条**启动链：引导器 → python-engine → Go 网关。
  //
  // 这里刻意没有回退分支。过去 `tryStartGoStack` 返回 null（找不到 chiron.exe /
  // 运行时没装）就会落到 desktop-local core 那条「自带 HTTP 服务」的老路径上，
  // 而那条路径已经随业务层一起删除（见 desktop-local/app/launcher.py 的模块文档：
  // 引导器只起 PG/Redis，不再提供任何 HTTP 端点）。留着回退只会让"网关二进制缺失"
  // 这个真实故障变成"起来了但所有业务端点 404"，用户和日志都无从判断。
  //
  // 因此：能起就起，起不来就把可读原因交回去（恢复对话框与错误页据此显示）。
  try {
    const goStack = await tryStartGoStack(options)
    if (goStack && goStack.ok) return goStack
  } catch (err) {
    const message = err?.message || String(err)
    logMain('error', `本地网关链路失败：${message}`)
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: err?.reason || 'stack-failed',
      message,
    }
  }

  // tryStartGoStack 返回 null 只有两种可能（见其注释）：缺网关二进制、或缺运行时。
  // 两种情况都必须让用户看到具体缺什么，而不是静默降级。
  return {
    ok: false,
    baseUrl: '',
    token: '',
    dataDir: '',
    reason: 'gateway-unavailable',
    message:
      '本地网关不可用：未找到 chiron 二进制或本地运行时尚未就绪。' +
      '请确认安装包完整（resources/chiron），或先执行一次运行时安装后重启应用。',
  }
}

/**
 * 建安装进度窗口。
 *
 * 为什么单独建一个 520×620 的窗口而不是复用主窗口：安装发生在 createWindow 之前，
 * 那时主窗口还不存在；而安装一旦开始就是几分钟的网络下载，没有反馈会被当成卡死。
 * 装完立刻销毁，主界面完全不感知它的存在。
 */
function openInstallWindow() {
  if (installWindow && !installWindow.isDestroyed()) return installWindow
  installWindow = new BrowserWindow({
    width: 520,
    height: 620,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    backgroundColor: WINDOW_BACKGROUND,
    show: false,
    title: 'Chiron — 正在准备本地环境',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      partition: partitionFor('local'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 进度页也用 preload 的订阅能力；模式按 local 注入，与它实际服务的东西一致
      additionalArguments: [`${MODE_ARG_PREFIX}local`],
    },
  })
  installWindow.once('ready-to-show', () => {
    if (installWindow && !installWindow.isDestroyed()) installWindow.show()
  })
  installWindow.on('closed', () => {
    installWindow = null
  })
  installWindow.loadFile(path.join(__dirname, 'install.html')).catch(err => {
    logMain('warn', `install page load failed: ${err?.message || err}`)
  })
  return installWindow
}

/** 关掉进度窗口。安装结束（成功或失败）必须调，否则它会一直盖在屏幕上 */
function closeInstallWindow() {
  const win = installWindow
  installWindow = null
  if (win && !win.isDestroyed()) win.destroy()
}

/**
 * 把安装阶段推给进度窗口。
 * 窗口不在就丢弃：安装也可能由"重启本地后端"触发，那时用户已经在主界面里了。
 */
function broadcastRuntimeStage(info) {
  const win = installWindow
  if (!win || win.isDestroyed()) return
  try {
    win.webContents.send(RUNTIME_STAGE_CHANNEL, info)
  } catch {
    /* 窗口正在销毁：进度丢了不影响安装本身 */
  }
}

/**
 * 只读询问：本地运行时现在能不能直接用。
 * 内部任何失败都当作"就绪"，让启动照原路走 —— 进度窗只是个提示，不该成为新的失败点。
 */
function inspectLocalRuntime() {
  const components = loadOptionalModule('./components.cjs')
  if (!components || typeof components.inspectLocalRuntime !== 'function') {
    return { ready: true, reason: 'unknown', message: '' }
  }
  try {
    const result = components.inspectLocalRuntime()
    // 就地改绑是"零成本修好、但用户永远看不见"的那类自愈：不在日志里留一行，
    // 下次出问题就没法区分"环境从没装过"与"绑定还跟着上一个包跑"。
    // 记的是 `rebinds`（core + 引擎两套）而不是 `result.binding`（只有最终返回那套）：
    // core 修好后函数会继续判引擎，返回值带的是引擎的绑定，core 那次就没人记了。
    for (const b of Array.isArray(result?.rebinds) ? result.rebinds : []) {
      if (!b || !b.rebound) continue
      logMain('info', `runtime python binding rebound (${b.target}): ${b.from} -> ${b.to}`)
    }
    return result && typeof result === 'object' ? result : { ready: true, reason: 'unknown', message: '' }
  } catch (err) {
    logMain('warn', `runtime inspect failed: ${err?.message || err}`)
    return { ready: true, reason: 'unknown', message: '' }
  }
}

/**
 * 停止本地后端：退出前与切换模式时都要调；没接线就当作已经停了。
 *
 * **两条链路各有各的停止函数，停错那套等于什么都没停。** 真机现场（Windows 安装包）：
 * 打包态走「引导器 → PG/Redis → 引擎 → Go 网关」，进程归 `local-stack.cjs` 所有，
 * 而这里过去只调 `components.stopCore` —— 那条链没起 core，`state.core` 为空，
 * stopCore 直接返回 false，日志照样打 `local backend stopped`。结果是退出之后
 * postgres 与 redis-server 仍在监听、仍占着 data dir；下一次启动又拉起一套 PG，
 * 两套实例抢同一个目录（本轮能连续启动成功只因为上一套是被我手动清掉的）。
 *
 * 现在按启动时记下的 `via` 分派，并把每一条的回收结果如实写进日志。
 * 是否要停只看 `localBackend.ok`（成功启动过才谈得上停）—— 归属是 ok 之后才会写的
 * 从属信息，拿它当入口闸门等于把不变量抄两份，改一处忘一处就回到"静默没停"。
 * `via` 为空 = 从没成功起过本地后端，没什么要停，直接返回 false（不打"stopped"）。
 */
async function stopLocalBackend() {
  if (localBackendStopped || !localBackend.ok) return false
  const ownership = localBackend
  localBackend = blankLocalBackend()
  localBackendStopped = true

  const via = ownership.via
  if (!via) {
    logMain('info', 'local backend 未曾成功启动，无需停止')
    return false
  }

  const stoppers = {
    'go-stack': () => {
      const stack = loadOptionalModule('./local-stack.cjs')
      return stack && typeof stack.stopLocalStack === 'function' ? stack.stopLocalStack() : null
    },
  }
  const stopper = stoppers[via]
  if (!stopper) {
    // 未知链路必须响：静默返回 true 就是上面那个 bug 的翻版（换了 owner 却没换停止函数）。
    logMain('warn', `本地后端链路未知（via=${via}），没有对应的停止函数：进程可能未被回收`)
    return false
  }

  try {
    const result = await stopper()
    if (result === null) {
      logMain('warn', `本地后端链路 ${via} 的停止函数不可用：进程可能未被回收`)
      return false
    }
    // go-stack 返回三条回收结果；core 返回布尔。有明细就打明细，别看板报"stopped"了事。
    const detail =
      via === 'go-stack' && result && typeof result === 'object'
        ? `（gateway=${Boolean(result.gatewayStopped)} engine=${Boolean(result.engineStopped)} launcher=${Boolean(result.launcherStopped)}）`
        : `（core=${Boolean(result)}）`
    logMain('info', `local backend stopped ${via}${detail}`)
    return true
  } catch (err) {
    logMain('warn', `local backend stop failed (${via}): ${err?.message || err}`)
    return false
  }
}

/**
 * 窗口 session 的通用配置：CORS/CSP 补头、权限、下载日志。
 *
 * 窗口用一个持久 partition（persist:chiron-local），所以这些监听必须挂在
 * 窗口真正使用的 session 上，不能再写死 session.defaultSession —— 否则补头会落在
 * 没人用的默认 session 上，界面会缺 CSP / CORS。
 */
function configureSession(partition) {
  const winSession = session.fromPartition(partition)
  if (configuredSessions.has(winSession)) return winSession
  configuredSessions.add(winSession)

  // 桌面壳从 file:// 访问 API：浏览器把 Origin 序列化成 null，后端 CORS 白名单
  // 不会为 null 返回 Access-Control-Allow-Origin，带 Authorization 的预检会被
  // 浏览器直接拒绝（表现为登录后所有请求失败）。这里只给用户配置的 API 前缀
  // 补上精确匹配的 null（不是 *），其它站点响应不受影响。
  winSession.webRequest.onHeadersReceived((details, callback) => {
    const responseHeaders = details.responseHeaders || {}
    const isApiRequest =
      currentApiUrl &&
      (details.url === currentApiUrl || details.url.startsWith(`${currentApiUrl}/`))
    if (isApiRequest) {
      responseHeaders['Access-Control-Allow-Origin'] = ['null']
    }
    // file:// 主文档默认没有 CSP，Electron 会直接报安全警告；只放行本地资源
    // 与用户配置的 API/WS origin，XSS 无法把数据外传到任意站点。
    // 判据抽在 shared.cjs：辅助页自带 meta CSP 且依赖内联脚本，注入响应头会让
    // 两者取交集后拒掉内联脚本，整页按钮静默失效（详见该函数的注释与单测）。
    if (shared.shouldInjectHeaderCsp(details)) {
      responseHeaders['Content-Security-Policy'] = [buildCsp()]
    }
    callback({ responseHeaders })
  })

  // 权限：只允许本地应用页面使用麦克风（语音输入）、剪贴板读取与全屏
  const allowedPermissions = new Set(['media', 'clipboard-read', 'fullscreen'])
  winSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    const requestingUrl = details?.requestingUrl || wc.getURL()
    if (!isTrustedUrl(requestingUrl) || !allowedPermissions.has(permission)) {
      logMain('warn', `denied permission ${permission} from ${requestingUrl}`)
      callback(false)
      return
    }
    if (permission === 'media') {
      const types = details?.mediaTypes || []
      callback(types.length === 0 || types.every(type => type === 'audio'))
      return
    }
    callback(true)
  })
  winSession.setPermissionCheckHandler((wc, permission, requestingOrigin, details) => {
    const requestingUrl = details?.requestingUrl || wc.getURL() || requestingOrigin
    if (!isTrustedUrl(requestingUrl) || !allowedPermissions.has(permission)) return false
    if (permission === 'media' && details?.mediaType === 'video') return false
    return true
  })

  // 下载走 Electron 默认保存流程，记录到本地日志便于排查
  winSession.on('will-download', (_event, item) => {
    logMain('info', `download start: ${item.getFilename()} ${item.getURL()}`)
    item.once('done', (_e, state) => {
      logMain(
        state === 'completed' ? 'info' : 'warn',
        `download ${state}: ${item.getFilename()} -> ${item.getSavePath()}`,
      )
    })
  })

  return winSession
}

/**
 * 首启选择运行模式（契约 §1）。
 *
 * 两种情形共用这一处询问，但**判据不同**：
 *   - 全新安装：磁盘上连配置文件都没有（`configExists=false`），用户从未选过 → 必须问；
 *   - 老配置迁移：有配置文件但没有 `mode` → 也必须问，且要提示它原来用的云端地址。
 *
 * 为什么不沿用 `needsMigration` 一个字段：它含 `configExists` 条件，全新安装恒为 false，
 * 结果就是"首启一声不响按默认值起步" —— 用户从未做过选择。这里改用 `needsModeChoice`
 * （只取决于 `modeChosen`），迁移文案再按 `needsMigration` 细分。
 *
 * 默认按钮是「本地模式」：本地是默认选项，但必须由用户确认后才继续。
 * 用户直接关掉对话框（取消）时按**本地**继续而不是退出应用 —— 点图标毫无反应
 * 比"按默认值开始"更糟，且模式随时可在菜单里切换、不会丢数据。
 */
async function askInitialMode(settings) {
  if (!settings.needsModeChoice) return false
  // 云端模式废除后只剩本地一种运行方式，首启再弹「选本地还是云端」已无意义 ——
  // 直接把唯一选项落盘，让配置文件离开「未选择」状态即可。
  // 老配置里残留的 mode:"cloud" 也在这里被覆盖成 local。
  saveMode('local')
  logMain('info', `initial mode recorded: local (was ${settings.mode})`)
  return true
}

/** 菜单里的模式切换：默认保留本地数据（契约 §1 规定默认不删 userData/local） */
async function confirmModeSwitch(nextMode) {
  const label = nextMode === 'local' ? '本地模式' : '云端模式'
  const detail =
    nextMode === 'local'
      ? '应用会重启并尝试启动本机后端；本地组件未安装时会显示提示页。本地数据目录不会被删除。'
      : '应用会重启，并清空本地模式下的登录状态与缓存。本地数据目录不会被删除。'
  const { response } = await dialog.showMessageBox({
    type: 'question',
    title: '切换运行模式',
    message: `切换到${label}？`,
    detail,
    buttons: ['切换并重启', '取消'],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  })
  if (response !== 0) return
  try {
    await applyModeSwitch(nextMode, {})
  } catch (err) {
    dialog.showErrorBox('切换运行模式失败', err?.message || String(err))
  }
}

/** 记下最近一次本地后端的启动信息（契约 §1 的 local.lastStartedAt / local.dataDir） */
function recordLocalStart(status) {
  if (!status || !status.ok) return
  try {
    const patch = { local: { lastStartedAt: new Date().toISOString() } }
    if (status.dataDir) patch.local.dataDir = String(status.dataDir)
    saveDeviceSettings(patch)
  } catch (err) {
    logMain('warn', `record local start failed: ${err?.message || err}`)
  }
}

/**
 * 切换模式：清零离开模式的会话数据 → 写盘 → relaunch。
 * 实际流程在 mode-switch.cjs（可单测），这里只负责注入 Electron 侧的回调：
 * 停止本地后端是必须的，否则重启后两个后端会抢同一个数据目录。
 */
async function applyModeSwitch(mode, opts) {
  const result = await switchMode({
    nextMode: mode,
    deleteLocalData: Boolean(opts && opts.deleteLocalData),
    stopBackend: async () => {
      if (currentMode === 'local') {
        // 标记由 stopLocalBackend 自己置位；这里再置一次只会掩盖"到底停没停"
        await stopLocalBackend()
      }
    },
    electron: { app, session },
    log: logMain,
  })
  return result
}

/** 重启应用（不改模式）：本地后端先停，避免 restart 后两个后端抢端口 */
function restartNow() {
  void (async () => {
    try {
      if (currentMode === 'local') {
        await stopLocalBackend()
      }
    } catch (err) {
      logMain('warn', `stop before restart failed: ${err?.message || err}`)
    }
    app.relaunch()
    // 让 IPC 回复先发出，再杀进程，否则渲染进程只会看到 "frame was disposed"
    setImmediate(() => app.exit(0))
  })()
}

/**
 * 本地模式启动失败时的恢复选项。
 * 用原生对话框而不是自建页面：这时前端 dist 还没被信任能跑（后端可能压根没起），
 * 而用户需要的是两个明确动作：重试 / 退出，不要静默卡在白屏上。
 * （云端模式已废除，原来那个「切回云端」的去路一并去掉。）
 */
async function askLocalBackendRecovery(status) {
  const actions = ['retry', 'quit']
  try {
    const { response } = await dialog.showMessageBox({
      type: 'warning',
      title: '本地模式未就绪',
      message: '本机后端没能启动，Chiron 打不开主界面',
      detail: `${status.message || '未知原因'}\n\n可以重试一次，或退出应用排查安装。`,
      buttons: ['重试启动', '退出'],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    })
    return actions[response] || 'quit'
  } catch (err) {
    logMain('warn', `local recovery dialog failed: ${err?.message || err}`)
    return 'quit'
  }
}

/**
 * 引擎在**运行中**退出。
 *
 * 这是本地模式里最难自己发现的一种故障：界面还活着，网关还活着，只有聊天会在
 * SSE 上回一句"服务暂时不可用"、并把这一轮标成 failed —— 用户完全看不出原因。
 * 网关侧对引擎是按请求解析地址、不探活也不熔断，所以它自己也不会主动宣告引擎掉了。
 *
 * 所以这里做三件事：写一条**带退出码**的错误日志；只提示一次（反复崩不该弹一串窗）；
 * 给一个真的能按的按钮（重启应用），而不是只说"请重启"。
 */
let engineExitNotified = false

function notifyEngineExit(info = {}) {
  const how =
    info.code === null || info.code === undefined
      ? `信号 ${info.signal || '未知'}`
      : `退出码 ${info.code}`
  const tail = String(info.stderrTail || '').trim().split('\n').slice(-8).join('\n')
  logMain('error', `python-engine 运行中退出（${how}）${tail ? `\n${tail}` : ''}`)
  if (engineExitNotified) return
  engineExitNotified = true

  const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
  if (!win) return
  dialog
    .showMessageBox(win, {
      type: 'error',
      title: '本地 AI 引擎已停止',
      message: '聊天、知识库与记忆现在都不可用',
      detail:
        `python-engine 进程退出了（${how}）。界面与本地数据库还在，所以其它页面照常，` +
        '但一发消息就会失败。\n\n' +
        (tail ? `引擎输出（尾部）：\n${tail}\n\n` : '') +
        '可以立刻重启应用；若反复出现，请连同这条信息一起反馈。',
      buttons: ['重启应用', '稍后手动重启'],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    })
    .then(({ response }) => {
      if (response === 0) restartNow()
    })
    .catch(err => logMain('warn', `engine exit dialog failed: ${err?.message || err}`))
}

/**
 * 需要装环境时把进度窗开起来，装完（成败都算）关掉。
 *
 * 首启那条路径自己管窗口（启动循环期间窗口一直开着，重试不闪窗），这里服务的是
 * **已经进了主界面之后**再起本地后端的情况（菜单/前端的"重启本地后端"）。那时
 * 安装一跑就是几分钟，没有窗口就等于让界面静默假死。
 */
async function startLocalBackendWithProgress(options = {}) {
  const runtime = inspectLocalRuntime()
  if (runtime.ready) return startLocalBackend(options)
  logMain('info', `local runtime not ready (${runtime.reason}): ${runtime.message || ''}`)
  openInstallWindow()
  try {
    return await startLocalBackend({
      ...options,
      onRuntimeStage: info => broadcastRuntimeStage(info),
      onRuntimeLine: line => logMain('info', `[runtime] ${line}`),
    })
  } finally {
    closeInstallWindow()
  }
}

/**
 * 建窗口：模式决定加载目标与 session 分区。
 *   local 且本地后端就绪 → 主界面（apiUrl = 本地 baseUrl）
 *   local 且未就绪 → 先给恢复选项，仍失败则落到 error.html（提示原因，不崩溃）
 * 云端模式已废除，不再有「填服务地址」的分流。
 */
/**
 * 启动时的 Windows Hello 二次确认。
 *
 * 为什么需要：本地身份来自 OS 账户，免口令会话由网关按 loopback 直接签发 ——
 * 能坐到这台机器前的人自动就是"账户主人"。共用工作站上，登录 Windows 的人
 * 走开而屏幕未锁时，回来的是另一个人。这道确认证明"仍是同一个已通过 OS 登录
 * 的人在场"。
 *
 * 分级处理（**不能一刀切锁死**）：
 *   · `Available` 且 `Verified`        → 放行
 *   · `Available` 但 `Canceled` /
 *     `RetriesExhausted`               → 用户明确没通过，退出应用
 *   · 设备不可用（`DeviceNotPresent`
 *     / `NotConfiguredForUser` / …）   → 跳过。加固不该让没有 Hello 硬件的
 *                                        机器永远进不去
 *   · 探测或调用本身失败（组策略禁用
 *     powershell、脚本异常）           → 跳过，只记日志
 *
 * @returns {Promise<boolean>} 是否继续启动
 */
async function confirmUnlockWithHello() {
  if (process.platform !== 'win32') return true

  let availability = null
  try {
    availability = await windowsHello.checkAvailability()
  } catch (err) {
    logMain('warn', `Windows Hello 可用性探测失败，跳过确认：${err?.message || err}`)
    return true
  }
  if (availability !== 'Available') {
    logMain('info', `Windows Hello 不可用（${availability || 'unknown'}），跳过二次确认`)
    return true
  }

  let result = null
  try {
    result = await windowsHello.requestVerificationResult('确认是您本人正在打开 Chiron')
  } catch (err) {
    logMain('warn', `Windows Hello 确认调用失败，跳过：${err?.message || err}`)
    return true
  }

  if (result === 'Verified') {
    logMain('info', 'Windows Hello 二次确认通过')
    return true
  }

  // 走到这里说明设备可用、用户却没过：取消或重试耗尽。这是明确的拒绝。
  logMain('warn', `Windows Hello 二次确认未通过（${result || 'unknown'}），退出应用`)
  dialog.showErrorBox(
    '未通过身份确认',
    'Chiron 需要先确认是您本人才能打开。请重新启动应用并完成 Windows Hello 验证。',
  )
  return false
}

function createWindow() {
  const settings = readDeviceSettings()
  currentMode = resolveMode(settings)
  const partition = partitionFor(currentMode)

  const route = resolveStartupWindow({
    localBackend,
  })
  const apiUrl = route.apiUrl
  const target = route.target
  currentApiUrl = apiUrl
  // 会话监听必须在建窗之前挂好：窗口一创建就开始发首次请求
  configureSession(partition)

  // 配置页与本机错误页都是单栏布局，主界面才用大窗口
  const isDialog = target !== 'app'

  // 每次创建窗口现传模式 / 地址 / token：渲染进程的 process.env 是启动时继承的，
  // 用户保存新地址后旧 env 不会更新。
  const additionalArguments = [`${MODE_ARG_PREFIX}${currentMode}`]
  if (apiUrl) additionalArguments.push(`${API_URL_ARG_PREFIX}${apiUrl}`)
  // 本地模式的设备 token 由网关侧提供；拿到 token 才注入。
  if (currentMode === 'local' && localBackend.token) {
    additionalArguments.push(`${DEVICE_TOKEN_ARG_PREFIX}${localBackend.token}`)
  }

  mainWindow = new BrowserWindow({
    width: isDialog ? 520 : 1440,
    height: isDialog ? 620 : 900,
    minWidth: isDialog ? 420 : 960,
    minHeight: isDialog ? 480 : 640,
    resizable: !isDialog,
    backgroundColor: WINDOW_BACKGROUND,
    show: false, // 等首帧就绪再显示，避免白屏闪一下
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      // 双模式各用一个持久分区：cookie / localStorage / IndexedDB 互不可见；
      // 切换模式时 mode-switch.cjs 清的就是「离开模式」的那个分区。
      partition,
      contextIsolation: true,
      nodeIntegration: false,
      // preload 要读 process.argv / versions，故不开 sandbox；
      // contextIsolation 已经隔离了页面与 preload 的执行上下文。
      sandbox: false,
      additionalArguments,
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  // 外部链接交给系统浏览器：壳内不该出现第二个浏览器的地址栏
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  // 同窗口导航限制：只允许本地 app 根目录 / 开发服务器；外部 http(s) 交系统浏览器
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isTrustedUrl(url)) return
    event.preventDefault()
    if (/^https?:/i.test(url)) shell.openExternal(url)
    else logMain('warn', `blocked navigation: ${url}`)
  })

  // 主文档加载失败：切到本地错误页，而不是留一个空白窗口
  mainWindow.webContents.on('did-fail-load', (_event, code, description, validatedURL, isMainFrame) => {
    if (!isMainFrame) return
    showLoadError(code, description, validatedURL)
  })

  // 渲染进程崩溃：记录并给用户选择重载
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    logMain('error', `renderer gone: ${details.reason} exitCode=${details.exitCode}`)
    if (details.reason === 'clean-exit' || !mainWindow || mainWindow.isDestroyed()) return
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'error',
      title: '页面进程已退出',
      message: `界面进程异常退出（${details.reason}）。`,
      buttons: ['重新加载', '关闭'],
      defaultId: 0,
      cancelId: 1,
    })
    if (choice === 0) mainWindow.reload()
  })

  // 界面长时间无响应：记录并让用户决定是否重载
  mainWindow.on('unresponsive', () => {
    logMain('warn', 'window unresponsive')
    if (!mainWindow || mainWindow.isDestroyed()) return
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'warning',
      title: '界面无响应',
      message: '界面暂时没有响应，是否重新加载？',
      buttons: ['继续等待', '重新加载'],
      defaultId: 0,
      cancelId: 1,
    })
    if (choice === 1) mainWindow.reload()
  })

  if (isDev && target === 'app') {
    mainWindow.loadURL(DEV_URL)
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else if (target === 'setup') {
    mainWindow.loadFile(path.join(__dirname, 'setup.html'))
  } else if (target === 'error') {
    // 本地模式但后端没起来：复用通用错误页，把原因写进详情
    mainWindow.loadFile(path.join(__dirname, 'error.html'), {
      query: {
        code: localBackend.reason || '',
        desc: localBackend.message || '本地后端未就绪',
        url: '',
        retry: '',
      },
    })
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

/** 保存地址后重建窗口：尺寸、preload 注入的地址、加载目标都要换 */
function relaunchIntoApp() {
  const old = mainWindow
  mainWindow = null
  holdingWindowQuota(() => {
    if (old) old.destroy()
    createWindow()
  })
}

/**
 * 从「连接设置…」菜单进入配置页。
 * 走"重建窗口"而不是在同窗口 loadFile：配置页是 520×620 的单栏布局，
 * 塞进 1440 宽的主窗口会显得空荡；重建一次反而干净（代价是主界面重载，可接受）。
 */
function relaunchIntoSetup() {
  const old = mainWindow
  mainWindow = null
  holdingWindowQuota(() => {
    if (old) old.destroy()
    // createWindow 会重新读配置：此时若已有地址，会直接进主界面 —— 所以这里
    // 先把窗口配置成"强制 setup"模式，避免用户点了"连接设置"却又弹回主界面。
    forceSetup = true
    createWindow()
    forceSetup = false
  })
}

/**
 * 打包后才检查更新：开发环境没有 app-update.yml，也没有签名产物。
 * 更新源在 electron-builder.yml 的 publish 段（GitHub Releases）。
 */
function setupAutoUpdater() {
  if (!app.isPackaged) return
  let autoUpdater
  try {
    ;({ autoUpdater } = require('electron-updater'))
  } catch (err) {
    logMain('warn', `electron-updater unavailable: ${err.message}`)
    return
  }
  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true
  autoUpdater.on('error', err => logMain('warn', `update error: ${err?.message || err}`))
  autoUpdater.on('update-available', info => logMain('info', `update available: ${info?.version || ''}`))
  autoUpdater.on('update-not-available', () => logMain('info', 'update not available'))
  autoUpdater.on('update-downloaded', info => {
    logMain('info', `update downloaded: ${info?.version || ''}`)
    void dialog
      .showMessageBox({
        type: 'info',
        title: '更新已下载',
        message: `Chiron ${info?.version || '新版本'} 已准备好，重启后生效。`,
        buttons: ['立即重启', '稍后'],
        defaultId: 0,
        cancelId: 1,
      })
      .then(({ response }) => {
        if (response === 0) autoUpdater.quitAndInstall()
      })
  })
  // 延迟到首屏之后再检查，避免和首页资源抢带宽
  setTimeout(() => {
    autoUpdater.checkForUpdatesAndNotify().catch(err => {
      logMain('warn', `update check failed: ${err?.message || err}`)
    })
  }, 8000)
}

// userData 根必须在这里定下来，**早于单实例锁与 whenReady**：
// 锁文件、config.json、日志、两套 venv、PG 数据目录全都在它下面。晚一步定，就会出现
// 「锁在旧根、数据在新根」这种同一进程内两套目录并存的局面。
//
// 为什么不靠 package.json 的 name：那是 `frontend-vue`，安装后的数据目录叫
// `%APPDATA%\frontend-vue`，用户既认不出也不敢删。`resolveUserDataDir` 把它换成
// 可识别的固定名，同时保留 CHIRON_DESKTOP_USER_DATA 的显式覆盖。
//
// Electron 的 `appData` 在 ready 之前就能读（它只依赖 APPDATA 环境变量），
// 因此这里不会因为过早调用而拿到空值；拿不到就保持默认，不拿空串去 setPath。
// 隔离模式：本地模式的数据必须留在**当前 OS 用户的 appData** 之内。
// 判据取自 config.json 里已持久化的运行模式；全新安装（还没有 mode）按本地处理 ——
// 那是桌面端的主路径，宁可多收一次口子，也不要让首启就落在一个共享目录里。
// 云端模式已废除 ⇒ 数据永远留在本机，隔离恒开。
const enforceUserDataIsolation = true

try {
  const resolvedUserData = shared.resolveUserDataDir({
    appData: app.getPath('appData'),
    override: process.env.CHIRON_DESKTOP_USER_DATA,
    enforceIsolation: enforceUserDataIsolation,
  })
  if (resolvedUserData) app.setPath('userData', resolvedUserData)
} catch (err) {
  // 定目录失败不能让应用起不来：保持 Electron 默认值，后续安装/启动各自会报自己的错
  logMain('warn', `userData 目录改写失败，沿用默认值：${err?.message || err}`)
}

// 数据目录收口：本地模式的数据必须只有当前 OS 用户与 SYSTEM 可访问。
// 必须在单实例锁之前做 —— 锁文件本身就在这个目录里。
//
// 时机说明：这里只对**已存在**的 userData 根收口；`local/` 子树由引导器与
// Electron 后续创建，会继承根的 ACL，所以不必逐个子目录递归。
// 失败只记日志：权限操作可能因组策略/网络盘失败，那不该让应用起不来。
if (enforceUserDataIsolation) {
  try {
    const aclResult = dataDirAcl.hardenDataDir({ dir: app.getPath('userData') })
    if (!aclResult.ok) {
      logMain('warn', `数据目录权限收口未完成：${aclResult.reason}`)
    }
  } catch (err) {
    logMain('warn', `数据目录权限收口异常：${err?.message || err}`)
  }
}

// 单实例：第二次启动时聚焦已有窗口，而不是再开一个进程
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  })

  app.whenReady().then(async () => {
    // Windows 通知/任务栏需要显式的 AppUserModelId，且要与打包 appId 一致
    if (process.platform === 'win32') app.setAppUserModelId(APP_ID)

    // 1) 首启选择运行模式：必须在建窗之前完成 —— 它决定 session 分区、启动分流与注入的地址。
    //    全新安装与老配置迁移都走这里（判据是 needsModeChoice，见 askInitialMode）。
    try {
      await askInitialMode(readDeviceSettings())
    } catch (err) {
      logMain('warn', `initial mode selection failed: ${err?.message || err}`)
    }

    // 2) 二次确认：本地身份来自 OS 账户，补一道"人在场"的证明。
    //    必须在任何业务启动之前 —— 确认没过就不该建窗、不该拉后端。
    currentMode = resolveMode(readDeviceSettings())
    if (currentMode === 'local' && enforceUserDataIsolation) {
      if (!(await confirmUnlockWithHello())) {
        app.quit()
        return
      }
    }

    // 3) 按模式准备后端：本地模式先把本机后端拉起来（未安装则提示并给恢复选项）
    logMain('info', `starting in ${currentMode} mode`)
    if (currentMode === 'local') {
      // 首次启动（或运行时被破坏）时，安装要跑几分钟。此时主窗口还不存在，
      // 所以先弹一个进度窗口 —— 否则用户看到的是"点了图标，什么都没有"。
      // 已就绪就完全不建这个窗口，避免正常启动时闪一下。
      const runtimeState = inspectLocalRuntime()
      const needsInstall = !runtimeState.ready
      if (needsInstall) {
        logMain('info', `local runtime not ready (${runtimeState.reason}): ${runtimeState.message || ''}`)
        openInstallWindow()
      }

      // 进度/输出行只推给安装窗口；它在装完后会被销毁，广播会自行丢弃
      const runtimeHooks = needsInstall
        ? {
            onRuntimeStage: info => broadcastRuntimeStage(info),
            onRuntimeLine: line => logMain('info', `[runtime] ${line}`),
          }
        : {}

      try {
        localBackend = await startLocalBackend(runtimeHooks)
        let attempts = 0
        // 最多再重试两次；仍失败就继续建窗口，让错误页把原因写在界面上
        while (!localBackend.ok && attempts < 2) {
          attempts += 1
          const action = await askLocalBackendRecovery(localBackend)
          if (action === 'quit') {
            app.quit()
            return
          }
          localBackend = await startLocalBackend(runtimeHooks)
        }
      } finally {
        // 无论成败都必须关掉：失败时留着它会盖住后面的错误页
        closeInstallWindow()
      }
      recordLocalStart(localBackend)
    }

    // 3) 精简菜单：保留复制/粘贴/全选/重载/开发者工具，去掉 Electron 默认的多余项
    Menu.setApplicationMenu(
      Menu.buildFromTemplate([
        {
          label: app.name,
          submenu: [
            { role: 'about' },
            { type: 'separator' },
            { role: 'hide' },
            { role: 'hideOthers' },
            { role: 'unhide' },
            { type: 'separator' },
            { role: 'quit' },
          ],
        },
        {
          label: '编辑',
          submenu: [
            { role: 'undo' },
            { role: 'redo' },
            { type: 'separator' },
            { role: 'cut' },
            { role: 'copy' },
            { role: 'paste' },
            { role: 'selectAll' },
          ],
        },
        {
          label: '视图',
          submenu: [
            { role: 'reload' },
            { role: 'forceReload' },
            { role: 'toggleDevTools' },
            { type: 'separator' },
            { role: 'resetZoom' },
            { role: 'zoomIn' },
            { role: 'zoomOut' },
            { type: 'separator' },
            { role: 'togglefullscreen' },
            { type: 'separator' },
          ],
        },
      ]),
    )

    // 配置页的两个动作。地址校验与持久化都在 config.cjs 里，主进程只做转发。
    // 同时校验 sender：只有应用自身页面（或开发服务器）能改服务地址。
    ipcMain.handle('desktop:test-api-url', (event, url) => {
      assertTrustedSender(event)
      return probe(url)
    })
    ipcMain.handle('desktop:set-api-url', (event, url) => {
      assertTrustedSender(event)
      saveApiUrl(url)
      relaunchIntoApp()
      return true
    })

    // 「修改服务地址」的界面入口（设置页与登录页用）。
    // 在此之前唯一的入口是菜单里那个「连接设置…」—— 快捷键 Ctrl+,，而菜单栏默认隐藏，
    // 等于没有入口：地址填错时主界面连不上后端，设置页又在登录之后，用户只能手改 config.json。
    ipcMain.handle('desktop:open-setup', event => {
      assertTrustedSender(event)
      relaunchIntoSetup()
      return true
    })

    // token：异步读取（safeStorage 的 async API），保存/清除走 invoke；全部校验 sender。
    ipcMain.handle('desktop:get-token', async event => {
      assertTrustedSender(event)
      return readToken()
    })
    ipcMain.handle('desktop:save-token', (event, token) => {
      assertTrustedSender(event)
      return writeToken(String(token || ''))
    })
    ipcMain.handle('desktop:clear-token', event => {
      assertTrustedSender(event)
      return clearToken()
    })

    // 设备设置与模式：真相在主进程的 config.json（契约 §1），渲染进程读到的只是它。
    ipcMain.handle('desktop:get-device-settings', event => {
      assertTrustedSender(event)
      return readDeviceSettings()
    })
    // 当前 OS 账户：本地模式下它**就是**应用用户（一个 OS 账户一套数据，
    // 见 data-dir-acl.cjs 与 userData 的隔离根）。账户页显示它，用户才知道
    // 自己看到的是哪一份数据。
    ipcMain.handle('desktop:get-os-account', event => {
      assertTrustedSender(event)
      return dataDirAcl.currentAccount()
    })
    ipcMain.handle('desktop:set-mode', (event, mode, opts) => {
      assertTrustedSender(event)
      return applyModeSwitch(mode, opts)
    })
    ipcMain.handle('desktop:restart', event => {
      assertTrustedSender(event)
      restartNow()
      return true
    })

    // ── 本地文档选择（契约 §2 的 `pickDocument`）──
    //
    // 只回 `{ path, name }`，**不读内容**：文档正文属于用户数据，读取与抽取由本地链路
    // 按需处理，主进程不在这里把它带进内存。用户取消返回 `null`（不是错误）。
    //
    // 对话框归属发起请求的窗口：`BrowserWindow.fromWebContents(event.sender)` 而不是
    // 模块级的 `mainWindow` —— 设置页开在独立窗口时，模态父窗口必须是它自己。
    // 不加 `filters`：本地抽取接受任意文件，限死扩展名只会让用户选不到自己的文档。
    ipcMain.handle('desktop:pick-document', async event => {
      assertTrustedSender(event)
      const owner = BrowserWindow.fromWebContents(event.sender)
      const options = { title: '选择文档', properties: ['openFile'] }
      const result = owner
        ? await dialog.showOpenDialog(owner, options)
        : await dialog.showOpenDialog(options)
      return shared.pickDocumentResult(result)
    })

    // ── 本地模型配置（provider / 端点 / 模型名 / key）──
    //
    // 三个动作都不抛异常给渲染进程：IPC 抛错会被 Electron 包成
    // "Error invoking remote method '...': Error: ..." 的一大串，界面只能整段回显。
    // 统一返回 { ok, message } —— message 是给人看的一句话（含原因），界面直接显示。
    //
    // 明文 apiKey 只在这一层与 secrets.cjs 之间流动：读回的配置里只有 hasKey 布尔，
    // 保存时也不回显、不写日志（契约 §8：不要把用户 provider key 写入日志/明文）。
    const MODEL_CONFIG_PROVIDERS = new Set(['ollama', 'openai-compatible'])

    function optionalSecrets() {
      const mod = loadOptionalModule('./secrets.cjs')
      if (!mod || typeof mod.readSecrets !== 'function' || typeof mod.writeSecrets !== 'function') {
        return null
      }
      return mod
    }

    /**
     * 合并单个 provider 条目：
     *   · baseUrl / model 显式给出才覆盖（允许清成空串 —— 用户主动恢复默认）；
     *   · apiKey 空白视为"保持不变"（界面上不回显 key，用户不重填就不该被清掉）；
     *   · clearApiKey === true 时删除已存的 key（明确的删密钥动作）。
     */
    function mergeProviderEntry(previous, patch) {
      const merged = { ...(previous && typeof previous === 'object' ? previous : {}) }
      for (const field of ['baseUrl', 'model']) {
        if (typeof patch[field] === 'string') merged[field] = patch[field].trim()
      }
      if (typeof patch.apiKey === 'string' && patch.apiKey.trim()) merged.apiKey = patch.apiKey.trim()
      if (patch.clearApiKey === true) delete merged.apiKey
      return merged
    }

    ipcMain.handle('desktop:get-model-config', async event => {
      assertTrustedSender(event)
      const mod = optionalSecrets()
      if (!mod || typeof mod.readPublicSecrets !== 'function') {
        return { ok: false, reason: 'secrets-unavailable', message: '当前版本不支持模型配置（secrets.cjs 未随包发布）' }
      }
      try {
        return { ok: true, config: await mod.readPublicSecrets() }
      } catch (err) {
        return { ok: false, reason: 'read-failed', message: err?.message || String(err) }
      }
    })

    ipcMain.handle('desktop:save-model-config', async (event, payload) => {
      assertTrustedSender(event)
      const mod = optionalSecrets()
      if (!mod) {
        return { ok: false, reason: 'secrets-unavailable', message: '当前版本不支持模型配置（secrets.cjs 未随包发布）' }
      }
      try {
        const incoming = payload && typeof payload === 'object' ? payload : {}
        const current = await mod.readSecrets()
        const next = {
          version: current && current.version ? current.version : undefined,
          active: current && typeof current.active === 'string' ? current.active : '',
          providers: { ...(current && current.providers ? current.providers : {}) },
        }
        if (typeof incoming.active === 'string') next.active = incoming.active.trim().toLowerCase()
        const patches = incoming.providers && typeof incoming.providers === 'object' ? incoming.providers : {}
        for (const [name, patch] of Object.entries(patches)) {
          if (!MODEL_CONFIG_PROVIDERS.has(name)) {
            return { ok: false, reason: 'unknown-provider', message: `未知的模型服务：${name}` }
          }
          next.providers[name] = mergeProviderEntry(next.providers[name], patch && typeof patch === 'object' ? patch : {})
        }
        const written = await mod.writeSecrets(next)
        if (!written) {
          // 最常见的原因：Linux 上没有可用的系统密钥环 / Windows DPAPI 被策略禁用
          return {
            ok: false,
            reason: 'write-failed',
            message: '无法写入密钥库：系统加密存储（safeStorage）不可用或写入失败，配置未保存',
          }
        }
        logMain('info', `model config saved (active: ${next.active || 'default'})`)
        const config = typeof mod.readPublicSecrets === 'function' ? await mod.readPublicSecrets() : null
        return { ok: true, config }
      } catch (err) {
        return { ok: false, reason: 'save-failed', message: err?.message || String(err) }
      }
    })

    /**
     * 重启本地后端（改完模型配置后让核心重新握手；不需要重启整个应用）。
     * 只允许本地模式调用 —— 云端模式下没有本地后端，静默成功会让用户以为生效了。
     */
    ipcMain.handle('desktop:restart-local-backend', async event => {
      assertTrustedSender(event)
      if (currentMode !== 'local') {
        return { ok: false, reason: 'not-local', message: '当前不是本地模式，没有可重启的本地后端' }
      }
      try {
        await stopLocalBackend()
        // stopLocalBackend 里有"只停一次"的标志位，重启要把它复位，否则下次退出不会停
        localBackendStopped = false
        // 走带进度窗的那一条：引擎依赖是"用到才装"的，从主界面重启后端时
        // 完全可能撞上从没装过的引擎环境，不弹窗就是静默假死几分钟。
        localBackend = await startLocalBackendWithProgress()
        recordLocalStart(localBackend)
        return {
          ok: Boolean(localBackend.ok),
          reason: localBackend.reason,
          message: localBackend.message,
          baseUrl: localBackend.ok ? localBackend.baseUrl : '',
        }
      } catch (err) {
        return { ok: false, reason: 'restart-failed', message: err?.message || String(err) }
      }
    })

    createWindow()
    // 主窗口已建出来，启动流程到此结束。此后 window-all-closed 才代表"用户真的关了窗口"；
    // 之前没有窗口只是启动期的正常中间态（见 window-all-closed 里的说明）。
    startupComplete = true
    setupAutoUpdater()

    // macOS：点 Dock 图标且没有窗口时重开
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  // macOS 习惯：关掉窗口不退出应用
  app.on('window-all-closed', () => {
    if (
      !shouldQuitOnAllWindowsClosed({
        platform: process.platform,
        startupComplete,
        holdingWindows,
      })
    ) {
      // 不能静默：真·退出和"被启动流程误伤"在日志里必须能区分开
      logMain(
        'info',
        `window-all-closed ignored (startupComplete=${startupComplete}, holdingWindows=${holdingWindows})`,
      )
      return
    }
    app.quit()
  })

  // 退出前停掉本地后端：否则 embedded-postgres / 推理代理会留成孤儿进程。
  // 停的过程是异步的，所以先 preventDefault，停完再真的 quit。
  // "只停一次"的标记由 stopLocalBackend 自己维护：它一进门就清空 localBackend 的归属，
  // 这里若再置一次，等于在壳里留第二个状态真值来源。
  app.on('before-quit', event => {
    if (currentMode !== 'local' || localBackendStopped || !localBackend.ok) return
    event.preventDefault()
    void stopLocalBackend().finally(() => app.quit())
  })
}

/**
 * 仅供诊断脚本 / 单测引用注入前缀，避免第二处再抄一遍字面量。
 * `electron .` 加载主进程入口时不看这个导出，写在这里无副作用。
 */
module.exports = { DEVICE_TOKEN_ARG_PREFIX }
