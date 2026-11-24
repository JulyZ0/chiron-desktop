/**
 * Chiron Desktop — preload
 *
 * 只暴露只读信息与明确的几个动作，不暴露 require / 任意 ipc 通道。
 * 页面通过 `window.chironDesktop` 读取（契约见 docs/desktop-local/contracts.md §2）。
 */
const { contextBridge, ipcRenderer } = require('electron')
const shared = require('./shared.cjs')
const { API_URL_ARG_PREFIX, MODE_ARG_PREFIX, TITLEBAR_ARG_PREFIX } = shared

/**
 * 设备 token 的注入前缀，必须与 main.cjs 的 DEVICE_TOKEN_ARG_PREFIX 完全一致。
 * 两处都先读 shared.cjs（契约 §3.1 要求共享常量集中在那里），读不到才用同一个
 * 兜底字面量 —— 这样把常量搬进 shared.cjs 时两边会同时生效，不会单边漂移。
 */
const DEVICE_TOKEN_ARG_PREFIX = shared.DEVICE_TOKEN_ARG_PREFIX || '--chiron-device-token='

/**
 * 服务地址从 `additionalArguments` 读取（主进程创建窗口时注入）。
 * 为什么不用 `process.env`：渲染进程的环境变量是**启动时**继承的，
 * 主进程在用户保存地址后才设置的值，preload 读不到。argv 是每次创建窗口时现传的。
 */
const apiFromArgv = process.argv.find(a => a.startsWith(API_URL_ARG_PREFIX))
const apiUrl = apiFromArgv
  ? apiFromArgv.slice(API_URL_ARG_PREFIX.length)
  : process.env.CHIRON_API_URL || ''

/**
 * 运行模式同样从 argv 读。argv 缺失时回落到 **local**：云端模式已废除，
 * 应用只有本地一种形态；回落到 cloud 会让页面去等一个不存在的远端后端。
 */
const modeFromArgv = process.argv.find(a => a.startsWith(MODE_ARG_PREFIX))
const rawMode = (
  modeFromArgv ? modeFromArgv.slice(MODE_ARG_PREFIX.length) : process.env.CHIRON_DESKTOP_MODE || ''
).toLowerCase()
const mode = rawMode === 'local' || rawMode === 'cloud' ? rawMode : 'local'

/**
 * 本地设备 token：local 模式下由主进程注入（core 的握手结果），cloud 模式不注入。
 *
 * 为什么可以放在 argv 里：它不是云端的用户密码，只是本机 core 认的"这是本机主人"的
 * 设备标识（desktop-local/app/api/auth.py）。云端 token 仍走 safeStorage + IPC
 * （getToken/saveToken/clearToken），两条路径互不影响。
 * argv 缺失时回落到 process.env（只对开发态 `CHIRON_DEVICE_TOKEN=... electron .` 有效）。
 */
const deviceTokenFromArgv = process.argv.find(a => a.startsWith(DEVICE_TOKEN_ARG_PREFIX))
const deviceToken = deviceTokenFromArgv
  ? deviceTokenFromArgv.slice(DEVICE_TOKEN_ARG_PREFIX.length)
  : // env 只在 local 模式回退：cloud 模式恒为空串，别把本地凭据带进另一个分区
    mode === 'local'
    ? process.env.CHIRON_DEVICE_TOKEN || ''
    : ''

/**
 * 标题栏形态：主进程按 `(平台, 加载目标)` 算好了再从 argv 传进来。
 *
 * 为什么不在渲染层自己按 `platform` 判断：同一台机器上，主界面是 frameless、
 * setup.html 却保留系统框 —— 平台判断在这里是**错**的，只有主进程知道
 * 当前窗口到底是哪一个。缺失时回落到 `native`（宁可多一组系统按钮，
 * 也不能画出一组点了没用的按钮）。
 */
const titleBarFromArgv = process.argv.find(a => a.startsWith(TITLEBAR_ARG_PREFIX))
const rawTitleBar = titleBarFromArgv ? titleBarFromArgv.slice(TITLEBAR_ARG_PREFIX.length) : ''
const titleBarMode = rawTitleBar === 'custom' || rawTitleBar === 'inset' ? rawTitleBar : 'native'

// 主进程用 safeStorage 加密保存；读取是异步的（Electron 45 的 async API），
// 由 store 在首屏挂载前 await，避免首屏请求缺少 Bearer token。
contextBridge.exposeInMainWorld('chironDesktop', {
  /** 'win32' | 'darwin' | 'linux' */
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
  /** 当前生效的服务地址；空串表示尚未配置 */
  apiUrl,
  /** 'local' | 'cloud'：当前运行模式（真相在主进程 config.json） */
  mode,
  /**
   * 本地设备 token（只读）：local 模式由主进程注入，cloud 模式恒为空串。
   * 页面在 local 模式下可以拿它做 `Authorization: Bearer`；也可以在空串时调
   * POST /v1/auth/session 从 core 换一个（第一批 auth disabled，两者都能用）。
   */
  deviceToken,
  isDesktop: true,

  /** 读取设备级设置（模式 / apiUrl / 本地数据目录 / UI / 迁移状态） */
  getDeviceSettings: () => ipcRenderer.invoke('desktop:get-device-settings'),
  /**
   * 当前 OS 账户的 `DOMAIN\username`。
   *
   * 本地模式下这就是"应用用户"本身 —— 一个 OS 账户对应一个应用用户，
   * 数据目录由 `%APPDATA%` + ACL 按账户隔离。账户页要显示它，让用户知道
   * 自己看到的是哪一份数据。
   */
  getOsAccount: () => ipcRenderer.invoke('desktop:get-os-account'),
  /**
   * 验证当前 Windows 账户密码 —— 本地模式「锁定」后的解锁入口。
   *
   * 只验密码，不落盘、不进日志、不回传明文。返回结构化结果：
   * `{ ok, reason, message, account? }`，`reason` 区分
   * `verified` / `bad-credential`（密码错，可重试）/ `verification-unavailable`
   * （断网、域控不可达 —— 用户没错，别让人反复重试正确密码）。
   */
  verifyLockPassword: payload => {
    // 两种形态都要接受：老调用点传纯密码串，启用第二因素后传 `{ password, token }`。
    // 统一成对象再发给主进程 —— 主进程只认对象（它始终要读 token）。
    const body =
      payload && typeof payload === 'object'
        ? { password: String(payload.password || ''), token: String(payload.token || '') }
        : { password: String(payload || ''), token: '' }
    return ipcRenderer.invoke('desktop:verify-lock-password', body)
  },
  /**
   * 第二因素（TOTP，可选）。
   *
   * 密钥与恢复码都只在主进程里（safeStorage 加密落盘），渲染进程只能拿到
   * "开没开 / 还剩几个恢复码"这类状态，以及启用那一刻**唯一一次**的明文恢复码。
   */
  getTwoFactorStatus: () => ipcRenderer.invoke('desktop:get-two-factor-status'),
  /**
   * 订阅**被动锁定**信号（系统锁屏 / 休眠 / 空闲超时）。
   *
   * 返回取消订阅函数。老外壳没有这条通道时返回一个空的取消函数 —— 界面照常
   * 可用，只是没有被动锁定；这比让调用点存不存在都要判断一层更简单。
   */
  onAutoLock: callback => {
    if (typeof callback !== 'function') return () => {}
    const handler = (_event, payload) => callback(payload || {})
    ipcRenderer.on(shared.AUTO_LOCK_CHANNEL, handler)
    return () => ipcRenderer.removeListener(shared.AUTO_LOCK_CHANNEL, handler)
  },
  beginTwoFactor: () => ipcRenderer.invoke('desktop:begin-two-factor'),
  confirmTwoFactor: token => ipcRenderer.invoke('desktop:confirm-two-factor', { token: token || '' }),
  disableTwoFactor: password =>
    ipcRenderer.invoke('desktop:disable-two-factor', { password: password || '' }),
  regenerateRecoveryCodes: password =>
    ipcRenderer.invoke('desktop:regenerate-recovery-codes', { password: password || '' }),
  /**
   * 切换运行模式：主进程会清掉离开模式的会话数据、写盘并重启应用。
   * deleteLocalData 默认 false —— 契约 §1 不允许默认删 userData/local。
   */
  setMode: (nextMode, opts) => ipcRenderer.invoke('desktop:set-mode', nextMode, opts || {}),
  /** 重启应用（不改模式） */
  restart: () => ipcRenderer.invoke('desktop:restart'),
  /**
   * 清空本机数据（不可恢复）：主进程会先做 Windows Hello 确认，再停后端、
   * 清界面缓存与 token、删 userData/local，最后重启应用。
   *
   * 身份核验**只在主进程做** —— 渲染进程说"用户确认过"不算数，
   * 一个被注入的页面同样能调这个通道。
   */
  wipeLocalData: password => ipcRenderer.invoke('desktop:wipe-local-data', { password: password || '' }),
  /**
   * 本机 Windows Hello 是否可用。
   *
   * 不可用时清空数据必须改验账户密码（主进程不会放行），
   * 界面据此提前把密码输入框显示出来，而不是等用户点完两次确认才告知。
   */
  getHelloAvailability: () => ipcRenderer.invoke('desktop:get-hello-availability'),
  /** 打开连接配置页（改服务地址）：主进程会重建窗口，当前界面随之重载 */
  openSetup: () => ipcRenderer.invoke('desktop:open-setup'),

  /** 测试连通性（打后端 /health）：成功 resolve，失败 reject 出可读 message */
  testApiUrl: url => ipcRenderer.invoke('desktop:test-api-url', url),
  /** 保存地址并进入主界面 */
  setApiUrl: url => ipcRenderer.invoke('desktop:set-api-url', url),
  /** 从系统凭据库异步读取 token */
  getToken: () => ipcRenderer.invoke('desktop:get-token'),
  /** 加密持久化 / 清除 Bearer token */
  saveToken: token => ipcRenderer.invoke('desktop:save-token', token),
  clearToken: () => ipcRenderer.invoke('desktop:clear-token'),

  /**
   * 选一个本地文档（契约 §2）。
   *
   * 只回 `{ path, name }`，**不读内容** —— 正文由本地链路按需处理。
   * 用户取消时 resolve `null`（不是错误），调用方按"没选"处理即可。
   */
  pickDocument: () => ipcRenderer.invoke('desktop:pick-document'),

  /**
   * 选一个本地文件夹作为**项目根目录**（会话地图改造后新增）。
   *
   * 只回 `{ path, name }`，**不校验、不写入**：路径合法性由后端 `validateRootPath`
   * 判定（绝对路径 / 存在 / 是目录 / 非系统目录），这一层只负责让用户选。
   * 用户取消时 resolve `null`（不是错误）。
   */
  pickDirectory: () => ipcRenderer.invoke('desktop:pick-directory'),

  /** 设备文件桥：稳定设备 id（项目归属用）与连接状态（设置页展示） */
  getDeviceId: () => ipcRenderer.invoke('desktop:get-device-id'),
  getDeviceBridgeStatus: () => ipcRenderer.invoke('desktop:get-device-bridge-status'),
  /** 团队挂载依赖（rclone/WinFsp）：状态与按需安装（仅服务端主机） */
  getMountDepsStatus: () => ipcRenderer.invoke('desktop:mount-deps-status'),
  installMountDep: name => ipcRenderer.invoke('desktop:install-mount-dep', name),

  /**
   * 用系统默认程序打开本地路径，或在文件管理器里定位（`reveal: true`）。
   *
   * H49：主进程不再只看"绝对 + 存在"。可以带上 `root`（工作台从 API 拿到的项目根），
   * 主进程会用 realpath 之后的路径做判定：
   *   - 不在已批准的项目根内 ⇒ 弹主进程自己的原生对话框问用户，页面无权批准；
   *   - `reveal` 只定位、不执行，始终放行；
   *   - 文档白名单以外（可执行/未知类型）需要用户在系统对话框里勾选确认；
   *   - UNC / 网络路径直接拒绝。
   *
   * 返回 `{ ok, reason?, message?, action?, kind? }` —— 页面要能区分
   * "文件没了" / "根没授权" / "类型需要确认" / "没有关联程序"。
   */
  openPath: (payload) => ipcRenderer.invoke('desktop:open-path', payload || {}),

  /** 已批准的项目根（只读名单；批准只能由主进程的原生对话框产生） */
  listProjectRoots: () => ipcRenderer.invoke('desktop:project-roots'),
  /** 请求批准一个项目根：主进程会弹原生对话框，用户同意才入名单 */
  grantProjectRoot: root => ipcRenderer.invoke('desktop:grant-project-root', root),
  /** 撤销一个已批准的项目根 */
  revokeProjectRoot: root => ipcRenderer.invoke('desktop:revoke-project-root', root),

  /**
   * 本地模型配置（provider / 端点 / 模型名 / key）。
   *
   * 读回来的**永远不含明文 key**：只有 `{ active, providers: { <name>: { baseUrl, model, hasKey } } }`。
   * 明文 key 只往主进程方向流动（saveModelConfig），保存后就只剩 hasKey 一个布尔。
   * 三个动作统一 resolve `{ ok, message? }` 形态，不抛错给页面整段回显。
   */
  getModelConfig: () => ipcRenderer.invoke('desktop:get-model-config'),
  /** 保存（合并语义）：providers.<name>.apiKey 为空 = 保留已存的 key */
  saveModelConfig: config => ipcRenderer.invoke('desktop:save-model-config', config || {}),
  /** 重启本地后端，让新的 provider 配置生效（仅本地模式） */
  restartLocalBackend: () => ipcRenderer.invoke('desktop:restart-local-backend'),

  /**
   * 重启**单个**子组件（健康面板的「重启」按钮）。
   *
   * 组件名由主进程按白名单收敛（engine / gateway / knowledge）—— 页面不决定
   * "哪些进程可以被重启"。与 `restartLocalBackend` 的区别：那个是整栈重启且会
   * 换端口（页面随后会被重建），这个用原端口重建，`apiUrl` 不变。
   */
  restartComponent: name => ipcRenderer.invoke('desktop:restart-component', { name: name || '' }),

  /**
   * 引擎环境变量开关（白名单）。
   *
   * 为什么需要：`SUPERAGENT_TAKEOVER_ENABLED` 这类开关只认进程环境变量，
   * 打包态从开始菜单启动没有 shell 可设，于是永远停在默认值（= 关）。
   * 这里只暴露**白名单内**的功能开关；服务端安全相关的 env（`APP_SECRET`、
   * `SANDBOX_*`）刻意不可改，主进程会丢弃白名单外的键。
   *
   * 保存后不会自动重启引擎 —— 重启会打断正在进行的会话，由用户显式触发
   * `restartLocalBackend()`。
   */
  getEngineEnvSettings: () => ipcRenderer.invoke('desktop:get-engine-env-settings'),
  saveEngineEnvSettings: toggles =>
    ipcRenderer.invoke('desktop:save-engine-env-settings', toggles || {}),

  /** 版本信息与在线升级：检查 GitHub 发布清单、下载并校验安装包（见主进程 UPDATE_REPO）。 */
  getAppInfo: () => ipcRenderer.invoke('desktop:get-app-info'),
  checkUpdate: () => ipcRenderer.invoke('desktop:check-update'),
  installUpdate: () => ipcRenderer.invoke('desktop:install-update'),
  getUpdateStatus: () => ipcRenderer.invoke('desktop:update-status'),

  /**
   * 复制文本（主进程通道，`utils/clipboard.ts` 的降级路径）。
   *
   * 页面侧的 `navigator.clipboard.writeText()` 在桌面端会因权限白名单、文档失焦
   * （`Document is not focused`）或非安全上下文而**静默失败**。Electron 的
   * `clipboard` 模块在主进程里不受这三条约束，所以留一条确定的兜底通道。
   */
  copyText: text => ipcRenderer.invoke('desktop:copy-text', typeof text === 'string' ? text : ''),


  /**
   * 订阅首次启动的运行时安装进度（install.html 用）。
   *
   * 只在安装进度页需要：正常启动时运行时已就绪，一条消息都不会来。
   * 返回取消订阅函数 —— 页面重载前不解绑会累积监听器。
   */
  onRuntimeInstallStage: callback => {
    if (typeof callback !== 'function') return () => {}
    const handler = (_event, info) => callback(info)
    ipcRenderer.on('desktop:runtime-stage', handler)
    return () => ipcRenderer.removeListener('desktop:runtime-stage', handler)
  },

  /**
   * 订阅"引擎崩溃取证已落盘"（崩溃自愈放弃时由主进程推送）。
   *
   * 负载 `{ path, lastLine, at }`：崩溃日志文件位置 + 最后一行错误。页面**不需要**
   * 解析格式，只需要把这两项摆给用户（"把这个文件发给我们"）。返回取消订阅函数。
   */
  onEngineCrash: callback => {
    if (typeof callback !== 'function') return () => {}
    const handler = (_event, info) => callback(info)
    ipcRenderer.on(shared.ENGINE_CRASH_CHANNEL, handler)
    return () => ipcRenderer.removeListener(shared.ENGINE_CRASH_CHANNEL, handler)
  },

  /**
   * 取消正在进行的运行时安装。
   *
   * 安装页必须有这条路：真机现场用户等了十几分钟没有任何反馈，只能关掉整个应用，
   * 结果下了一半的东西全部作废、下次重来。给它一个能按的取消，至少让"我不想等了"
   * 变成一个明确动作而不是杀进程。
   */
  cancelRuntimeInstall: () => ipcRenderer.invoke(shared.RUNTIME_CANCEL_CHANNEL),

  /** 改用镜像源重试（由用户在安装页显式点击，不自动换源） */
  retryRuntimeInstallWithMirror: () => ipcRenderer.invoke(shared.RUNTIME_MIRROR_CHANNEL),

  /**
   * 自绘标题栏的窗口控制。
   *
   * 窗口是 frameless 的，系统标题栏不再存在：最小化 / 最大化 / 关闭 / 拖拽
   * 全部由页面自己提供。这里只暴露**这四个**动作，不暴露任意 IPC 通道 ——
   * 页面不能借这条路过问窗口以外的东西。
   *
   * `mode` 由主进程注入（见 main.cjs 的 TITLEBAR_ARG_PREFIX）：
   *   - `custom`：无系统标题栏（Windows/Linux），页面必须画完整的控制条；
   *   - `inset` ：macOS，原生红绿灯保留，页面只画拖拽区，按钮位置让出左侧；
   *   - `native`：保留系统框（setup/error 等静态页），页面不应画标题栏。
   */
  titleBar: {
    mode: titleBarMode,
    minimize: () => ipcRenderer.invoke(shared.WINDOW_CHANNELS.minimize),
    /** 切换最大化/还原，resolve 出切换后的 `{ maximized, fullScreen, focused }` */
    toggleMaximize: () => ipcRenderer.invoke(shared.WINDOW_CHANNELS.toggleMaximize),
    close: () => ipcRenderer.invoke(shared.WINDOW_CHANNELS.close),
    /** 当前窗口状态；首帧用它把图标画对（页面不知道上一个进程留下的状态） */
    getState: () => ipcRenderer.invoke(shared.WINDOW_CHANNELS.getState),
    /**
     * 订阅窗口状态变化，返回取消订阅函数。
     * 拖到屏幕边缘（Aero Snap）、Win+方向键、F11 都会改变状态，页面收不到任何点击，
     * 只能靠这个事件跟住。
     */
    onStateChange: callback => {
      if (typeof callback !== 'function') return () => {}
      const handler = (_event, state) => callback(state)
      ipcRenderer.on(shared.WINDOW_CHANNELS.state, handler)
      return () => ipcRenderer.removeListener(shared.WINDOW_CHANNELS.state, handler)
    },
  },
})
