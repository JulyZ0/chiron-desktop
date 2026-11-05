/**
 * 主进程与 preload 共享的启动契约。
 *
 * 两边各写一份 `--chiron-api-url=` 前缀时，改一处就会让地址注入静默失效：
 * 主进程换了前缀、preload 还按旧前缀找，页面会静默回落到同源请求。
 * 模式前缀与分区名同理，所以都放在这里。
 */
const path = require('node:path')

/**
 * userData 根目录名。
 *
 * **不能再用 Electron 默认值**：默认值来自 `package.json` 的 `name`（`frontend-vue`），
 * 于是本地模式的全部数据落在 `%APPDATA%\frontend-vue\` —— 一个看不出是谁家的目录，
 * 用户清理 AppData 时既不敢删也找不到。这里钉死一个域名反写的可识别名字。
 *
 * 改名的代价是一次「看起来像丢了数据」：老目录不会被搬，本地模式从零开始
 * （两套 venv 重装、PG 重新 initdb）。契约 §1 明确不删 `userData/local/**`，
 * 所以老目录原样留在磁盘上，需要时可手工取回。
 */
const USER_DATA_DIR_NAME = 'gh_Athenavi_chiron'

/**
 * `child` 是否落在 `parent` 之内（含相等）。
 *
 * 本地模式的隔离边界就是「当前 OS 用户的 appData」—— 两个 Windows 用户各有各的
 * `%APPDATA%`，因此这条判据等价于「数据是否留在本用户名下」。
 *
 * Windows 路径大小写不敏感，`path.relative` 在 win32 上已按小写归一，这里不再
 * 额外 toLowerCase，避免在 Linux/macOS 上把两个真实不同的目录判成同一个。
 *
 * @returns {boolean} 两者任一非绝对路径时可判定为 false（宁可拒，不可放行）
 */
function isPathInside(parent, child) {
  const p = typeof parent === 'string' ? parent.trim() : ''
  const c = typeof child === 'string' ? child.trim() : ''
  if (!p || !c) return false
  if (!path.isAbsolute(p) || !path.isAbsolute(c)) return false
  const rel = path.relative(path.resolve(p), path.resolve(c))
  if (rel === '') return true
  return !rel.startsWith('..') && !path.isAbsolute(rel)
}

/**
 * 本次进程该用的 userData 根。
 *
 * 优先级：`CHIRON_DESKTOP_USER_DATA`（真机 smoke / 多环境隔离 / 便携部署）> `appData` + 固定名。
 *
 * 抽成纯函数是因为 `main.cjs` 一 require 就会起 Electron，`app.setPath` 那一步没法在
 * 单测里跑；而这个判据错一次的后果是**两个目录各存一半**（config.json 在新根、
 * venv 在旧根），表现得像"运行时莫名其妙又要重装一遍"。
 *
 * ## 隔离模式（`enforceIsolation`）
 *
 * `CHIRON_DESKTOP_USER_DATA` 无条件优先，这在开发期很方便，但它同时也是**唯一能
 * 击穿「一个 OS 用户一套数据」的东西**：任何进程把它指向 `C:\ProgramData\chiron`，
 * 两个 Windows 用户就会共用同一套 embedded PG 与同一份 `APP_SECRET`。
 *
 * 因此本地模式传 `enforceIsolation: true`，此时越界 override 被**丢弃并回落**
 * 到本用户的 appData 根（不报错，因为便携部署是合法用法，只是不该在隔离模式下生效）。
 * 默认 false 保持既有语义，测试与脚本不受影响。
 *
 * @param {object} options `{ appData, override, enforceIsolation }`
 * @returns {string} 绝对路径；两者都拿不到时返回空串（调用方保持 Electron 默认值）
 */
function resolveUserDataDir(options = {}) {
  const override = typeof options.override === 'string' ? options.override.trim() : ''
  const appData = typeof options.appData === 'string' ? options.appData.trim() : ''
  const enforceIsolation = options.enforceIsolation === true
  if (override) {
    // 隔离模式下 override 只能留在本用户 appData 之内；越界一律回落。
    if (!enforceIsolation || (appData && isPathInside(appData, override))) return override
  }
  if (!appData) return ''
  return path.join(appData, USER_DATA_DIR_NAME)
}

const API_URL_ARG_PREFIX = '--chiron-api-url='
/** 主进程创建窗口时用它把当前运行模式告诉 preload（契约 §2 的 window.chironDesktop.mode） */
const MODE_ARG_PREFIX = '--chiron-mode='
/** 主进程建窗时把本地 core 的设备 token 注入 preload（契约 §2 的 deviceToken） */
const DEVICE_TOKEN_ARG_PREFIX = '--chiron-device-token='
const DEFAULT_DEV_URL = 'http://localhost:5173'

/**
 * 双模式的会话分区（契约 §1/§3）：模式和 partition 一一对应，
 * 切模式时清的就是「离开模式」的那个 partition。
 * 前缀必须带 `persist:` —— 否则 session 只在内存里，重启又会串味。
 */
const PARTITION_PREFIX = 'persist:chiron-'

/**
 * 安装页两个动作的 IPC 通道名。
 *
 * 与 `RUNTIME_STAGE_CHANNEL` 一样放在这里而不是各写一份：主进程 handle、preload invoke
 * 两边名字不一致时不会报错，只会静默失效 —— 用户点了"取消"却什么也没发生。
 */
const RUNTIME_CANCEL_CHANNEL = 'desktop:runtime-cancel'
const RUNTIME_MIRROR_CHANNEL = 'desktop:runtime-mirror'

/**
 * 可选的 PyPI 镜像源（**只在用户显式点击时才用**）。
 *
 * 为什么不写死或自动切换：README/代码里明确约定"不写死任何第三方地址"，自动换源等于
 * 把用户的依赖装进一个未经其同意的第三方镜像。所以这里只提供候选，由安装页的按钮
 * 触发；`indexUrl` 为空时 uv 仍走官方 PyPI。
 */
const PIP_INDEX_MIRRORS = [
  { id: 'tuna', label: '清华 TUNA', url: 'https://pypi.tuna.tsinghua.edu.cn/simple' },
  { id: 'aliyun', label: '阿里云', url: 'https://mirrors.aliyun.com/pypi/simple' },
]

function partitionFor(mode) {
  // 云端模式已废除：分区名恒为 local。保留入参是为了不打断既有调用点，
  // 但任何传入 'cloud' 的调用都不该再产生第二个分区。
  void mode
  return `${PARTITION_PREFIX}local`
}

/**
 * 窗口全关时到底该不该退出进程。
 *
 * 看起来是"非 macOS 就退出"一行，但启动流程里有两个**没有窗口是正常状态**的窗口期：
 *
 *   - 首次启动：安装进度窗是当时唯一的窗口，装完 `destroy()` 掉、主窗口还没建出来；
 *   - 重建窗口：`relaunchIntoApp` / `relaunchIntoSetup` 先销毁旧窗再建新窗。
 *
 * 而 `BrowserWindow#destroy()` 会**同步**派发 window-all-closed（electron 45 实测确认，
 * 不是等下个 tick）。照直 quit 的话，app.quit() 在 destroy() 内部就排进队列，
 * 紧接着建好的主窗口没机会显示 —— 用户看到的就是"检查本地运行时就闪退"。
 *
 * 抽成纯函数放这里，是因为 main.cjs 一 require 就会起 Electron，单测没法直接覆盖它。
 */
function shouldQuitOnAllWindowsClosed({ platform, startupComplete, holdingWindows }) {
  // macOS 习惯：关窗口不退应用
  if (platform === 'darwin') return false
  // 启动期 / 窗口重建期的空窗口不是用户意图
  if (!startupComplete) return false
  if (holdingWindows > 0) return false
  return true
}

/**
 * 把 `dialog.showOpenDialog` 的结果翻译成 `pickDocument` 的返回值（契约 §2）。
 *
 * 抽成纯函数放这里，同样是因为 main.cjs 一 require 就会起 Electron：
 * 对话框的回调没法在单测里跑，但"取消 / 空选 / 多选"这几条分支必须被钉住。
 *
 * 三条约定：
 *   - 用户取消 → `null`，不是错误（调用方按"没选"处理）；
 *   - 多选也只取第一个 —— 契约的返回类型是单个 `{ path, name }`；
 *   - 不做扩展名过滤：本地抽取接受任意文件，限死类型只会让用户选不到自己的文档。
 */
function pickDocumentResult(result) {
  if (!result || result.canceled) return null
  const filePath = Array.isArray(result.filePaths) ? result.filePaths[0] : ''
  if (!filePath) return null
  return { path: filePath, name: path.basename(filePath) }
}

/**
 * 把 `dialog.showOpenDialog({ properties: ['openDirectory'] })` 的结果翻译成
 * `pickDirectory` 的返回值。
 *
 * 与 `pickDocumentResult` 结构相同但**刻意分开**：两者的语义不同（一个是"要读的文档"，
 * 一个是"要绑定的项目根目录"），合成一个函数后，将来任一方要加字段（例如目录要回
 * `isGitRepo`）就会变成"另一个也得跟着改"。取消同样返回 `null` 而非错误。
 */
function pickDirectoryResult(result) {
  if (!result || result.canceled) return null
  const dir = Array.isArray(result.filePaths) ? result.filePaths[0] : ''
  if (!dir) return null
  // 末尾的路径分隔符会让后端 `filepath.Clean` 之外的地方（比较、显示）不一致；
  // 这里保留原样，规范化交给后端 —— 前端裁剪盘符根目录（`C:\`）会裁成 `C:`。
  return { path: dir, name: path.basename(dir) || dir }
}

/**
 * 建窗分流：这次启动该加载哪个页面，以及把哪个地址交给 preload。
 *
 * 抽成纯函数放这里还是那条老理由 —— main.cjs 一 require 就会起 Electron，单测没法直接
 * 覆盖它；而这条判据恰好是「切到云模式后改不了服务地址」的全部因果：旧实现只看
 * `apiUrl` 空不空，config.json 里一个陈旧地址（上个时代的自托管端口、写错的域名）
 * 就足以把窗口送进主界面 —— 配置页永不出现，而主界面里当时没有任何改地址的入口：
 * 设置页在登录之后才进得去，后端不可达就登不了录。
 *
 * `pendingApiUrl` 由 mode-switch.cjs 在「切到云端模式」时置位、建窗时消费，含义是
 * 「这次进云端还没确认过服务地址」。切模式必然重启，所以标记要落盘才活得到新进程；
 * 但只用一次 —— 云端模式下正常重开应用不该再被拦到配置页。
 */
function resolveStartupWindow(options = {}) {
  // 云端模式已废除：不再有「填服务地址」的分流，只剩本地后端就绪与否。
  // apiUrl 入参保留但只用于兜底返回，实际地址由 localBackend.baseUrl 决定。
  const { localBackend } = options
  const backend = localBackend || {}
  // 本地后端没就绪：落到通用错误页并写清楚原因，而不是留给用户一个空白窗口
  if (backend.ok && backend.baseUrl) return { target: 'app', apiUrl: backend.baseUrl }
  return { target: 'error', apiUrl: '' }
}

/**
 * 这张主文档该不该由主进程注入响应头 CSP。
 *
 * 曾经对所有 file:// 主文档无条件注入 `buildCsp()`，而 setup / install / error 三张
 * 辅助页各自带了更严的 meta CSP（`default-src 'none'`），并且全靠**内联 `<script>`**
 * 挂事件。多条 CSP 取**交集**，而 buildCsp() 的 script-src 只有 `'self' file:
 * 'wasm-unsafe-eval'`、没有 `'unsafe-inline'` —— 交集里内联脚本被拒，整段不执行。
 *
 * 后果不是"样式不对"而是**整页按钮全部静默失效**：监听器根本没绑上。实测证据
 * （electron 45，复刻本判据的真实探针）：
 *   - 注入后 setup.html：`Executing inline script violates ... 'script-src 'self' file: 'wasm-unsafe-eval''`
 *   - 页面状态：输入合法地址后「保存并进入」仍是 disabled ⇒ sync() 从未跑过
 * 这正是「改用本地模式」按钮点了没有任何效果的原因。
 *
 * 所以三张辅助页一律放过：它们自有 meta CSP 把关，松紧不由这里决定；SPA 的
 * `dist/index.html` 及其开发地址仍然注入，XSS 外传链路不受影响。
 *
 * @param {{url?: string, resourceType?: string}} details webRequest 的 details 子集
 * @returns {boolean} true 表示应当注入响应头 CSP
 */
function shouldInjectHeaderCsp(details = {}) {
  const url = typeof details.url === 'string' ? details.url : ''
  if (!url.startsWith('file:')) return false
  const bareUrl = url.split('#')[0].split('?')[0]
  const isMainDocument = details.resourceType === 'mainFrame' || bareUrl.endsWith('/index.html')
  if (!isMainDocument) return false
  // 辅助页：自带 meta CSP，且依赖内联脚本
  return !(/(^|[\/])(setup|install|error)\.html$/.test(bareUrl))
}

module.exports = {
  USER_DATA_DIR_NAME,
  resolveUserDataDir,
  isPathInside,
  API_URL_ARG_PREFIX,
  MODE_ARG_PREFIX,
  DEVICE_TOKEN_ARG_PREFIX,
  DEFAULT_DEV_URL,
  PARTITION_PREFIX,
  RUNTIME_CANCEL_CHANNEL,
  RUNTIME_MIRROR_CHANNEL,
  PIP_INDEX_MIRRORS,
  partitionFor,
  shouldQuitOnAllWindowsClosed,
  pickDocumentResult,
  pickDirectoryResult,
  resolveStartupWindow,
  shouldInjectHeaderCsp,
}
