/**
 * 设备级运行模式（`local` / `cloud`）store。
 *
 * 契约（docs/desktop-local/contracts.md §1–§2）：
 *   · 模式的**真相在主进程** `userData/config.json`，经 preload 的 `window.chironDesktop`
 *     暴露（`mode` 属性 + `getDeviceSettings()` / `setMode()` / `restart()`）；
 *   · 渲染进程 `localStorage` 只做**镜像**（`chiron.device.mode`）—— 用于 preload 尚未
 *     注入完成、或主进程读取失败时的一次性兜底，不反向写回主进程；
 *   · 模式是**设备级**设置，与登录无关；切换后必须重启应用（见 §1），因此界面不能
 *     假设"切换后同会话内一切立即就位"。
 *
 * 为什么把 `currentDeviceMode()` 做成**模块级函数**（而不是只暴露 pinia store）：
 * `src/api/index.ts`（axios 响应拦截器）与 `src/router/guard.ts`（路由守卫）都要在
 * 非组件上下文判断模式，其中守卫在单测里**没有 pinia**（`router/__tests__/guard.spec.ts`
 * 只 mock 了 auth store）。让它们依赖模块级函数，既避开 "no active pinia" 崩溃，
 * 也避免 api ↔ store 的循环依赖（本文件**不**导入 `../api`）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/** 模式取值：`local` = 数据与模型都在本机；`cloud` = 连接云端/团队服务 */
export type ChironMode = 'local' | 'cloud'

/** preload 契约 `DeviceSettings`（字段顺序与含义见 contracts.md §2） */
export interface DeviceSettings {
  mode: ChironMode
  apiUrl: string
  local: { dataDir: string; lastStartedAt: string }
  ui: { locale: string; theme: string }
  migration: { legacyCloudDetected: boolean; modeChosenAt: string }
}

/**
 * `window.chironDesktop` 的**可选**视图。
 *
 * 全部字段可选：W1（Electron 主进程）与我们并行开发，preload 可能还没暴露模式相关
 * 成员。这里的每个调用点都必须能在"外壳不支持"时优雅降级 —— 不能因为桌面壳版本旧
 * 就把 Web 端也能用的界面打挂。
 */
export interface ChironDesktopHost {
  isDesktop?: boolean
  platform?: string
  apiUrl?: string
  mode?: ChironMode
  getDeviceSettings?: () => Promise<DeviceSettings>
  setMode?: (mode: ChironMode, opts?: { deleteLocalData?: boolean }) => Promise<void>
  restart?: () => Promise<void>
  /** 打开主进程的连接配置页（改服务地址）；老外壳没有这个成员 */
  openSetup?: () => Promise<unknown>
  /**
   * 当前 OS 账户的 `DOMAIN\username`。
   *
   * 本地模式下它就是"应用用户"：一个 OS 账户 = 一个应用用户，数据目录由
   * `%APPDATA%` + ACL 按账户隔离。老外壳没有这个成员，界面要能降级。
   */
  getOsAccount?: () => Promise<string>
  /**
   * 清空本机数据（不可恢复）。主进程内部完成 Windows Hello 确认、
   * 停后端、清缓存与 token、删 `userData/local`，然后重启应用。
   *
   * 老外壳没有这个成员，界面要能降级。
   */
  wipeLocalData?: (password?: string) => Promise<WipeLocalDataResult>
  /** 本机 Windows Hello 是否可用；不可用时清空数据必须改验账户密码 */
  getHelloAvailability?: () => Promise<{ available: boolean; availability?: string }>
}

/** `wipeLocalData` 的结构化结果：失败也要能区分原因，而不是一律弹「失败了」 */
export interface WipeLocalDataResult {
  ok: boolean
  /** `ok=true` 时表示是否已自动重启（false 时数据仍已清空，只需手动重开） */
  relaunched?: boolean
  /**
   * `not-local` 当前不是本地模式
   * `needs-password` 没有可用的 Windows Hello，需要账户密码（不是失败，是下一步）
   * `denied` Hello 上被用户拒绝
   * `bad-credential` 密码不正确
   * `account-issue` 账户层面问题（过期/禁用/需改密码）
   * `verification-unavailable` 验证所需的环境不可用（断网 / 域控不可达）——
   *   不是用户做错了什么，重试也不会好，界面要给可执行的下一步
   * `wipe-failed` 清空过程出错
   */
  reason?: string
  message?: string
  cleaned?: string[]
  warnings?: string[]
  /** 验证不可用时由主进程带回的真实数据目录 —— 界面用它给出可执行的手动出路 */
  dataDir?: string
}

/** localStorage 镜像键（真相在主进程 config.json，这里只镜像） */
export const DEVICE_MODE_STORAGE_KEY = 'chiron.device.mode'

export function isChironMode(value: unknown): value is ChironMode {
  return value === 'local' || value === 'cloud'
}

/** 桌面外壳对象；Web 端（含 jsdom 测试环境）为 undefined */
export function desktopHost(): ChironDesktopHost | undefined {
  return (globalThis as { chironDesktop?: ChironDesktopHost }).chironDesktop
}

export function isDesktopHost(): boolean {
  return Boolean(desktopHost()?.isDesktop)
}

/**
 * 开发环境登录绕过标志位的存储键。
 *
 * 为什么要这个东西：浏览器直连 `vite`（`:5173`）时**没有** Electron preload，
 * `resolveDeviceMode()` 只能判成 `cloud`；而 cloud 模式的 `isAuthenticated` 要求
 * `user` 非空 —— 开发时后端没起、没登录态，于是每个 `requiresAuth` 页都被守卫
 * 弹回首页，改了界面根本看不到。
 */
export const DEV_AUTH_BYPASS_STORAGE_KEY = 'chiron.dev.auth-bypass'

/** 一次性开启用的 URL 参数（`?devAuthBypass=1` / `=0`），命中后写回 localStorage */
const DEV_AUTH_BYPASS_QUERY = 'devAuthBypass'

/**
 * 是否启用了「开发环境登录绕过」。
 *
 * 三重约束，缺一不可：
 *   1. **只在 DEV 构建生效** —— `import.meta.env.DEV` 是编译期常量，生产构建里
 *      整个函数体被 DCE 移除，不可能变成线上后门；
 *   2. 显式开启：`VITE_DEV_AUTH_BYPASS=1` / localStorage / URL 参数任一；
 *   3. 判定为 `local` 模式后果最小 —— 本地模式本来就"没有登录这回事"。
 */
export function isDevAuthBypassEnabled(): boolean {
  // 生产构建：这个分支会被整段剔除（不要在这里加任何副作用）
  if (!import.meta.env.DEV) return false

  if (import.meta.env.VITE_DEV_AUTH_BYPASS === '1') return true

  try {
    const raw = globalThis.localStorage?.getItem(DEV_AUTH_BYPASS_STORAGE_KEY)
    return raw === '1'
  } catch {
    return false
  }
}

/**
 * 从当前 URL 读取绕过标志的原始值（不存在返回 null）。
 *
 * 两种路由模式都要覆盖，否则"桌面端开了没反应"会变成一个很难查的哑弹：
 *   - Web / `vite` 开发服务器：history 路由，参数在 `location.search`；
 *   - 桌面壳：`file://` 下切 hash 路由，参数落在 `#/chat?devAuthBypass=1` 的 hash 里。
 */
function readBypassFlagFromLocation(): string | null {
  const loc = globalThis.location
  if (!loc) return null
  const fromSearch = new URLSearchParams(loc.search || '').get(DEV_AUTH_BYPASS_QUERY)
  if (fromSearch !== null) return fromSearch
  const hash = loc.hash || ''
  const queryStart = hash.indexOf('?')
  if (queryStart === -1) return null
  return new URLSearchParams(hash.slice(queryStart + 1)).get(DEV_AUTH_BYPASS_QUERY)
}

/**
 * 开发环境自动登录用的凭据（仅在 `?devAuthBypass=1` 且 DEV 构建下被读取）。
 *
 * 从 `frontend-vue/.env.local` 读（该文件被 `*.local` 规则忽略，不会进版本库）：
 *
 * ```
 * VITE_DEV_AUTH_EMAIL=owner@local.dev
 * VITE_DEV_AUTH_PASSWORD=...
 * ```
 *
 * 没有配置时返回 `null` —— 此时绕过只放宽前端守卫、不尝试登录（页面会因为 401
 * 而空数据，但不会刷错误提示，见 api/index.ts 的 401 分支）。
 *
 * 生产构建里整个函数被 DCE 移除，凭据不会进产物。
 */
export function devAuthCredentials(): { email: string; password: string } | null {
  if (!import.meta.env.DEV) return null
  const email = import.meta.env.VITE_DEV_AUTH_EMAIL
  const password = import.meta.env.VITE_DEV_AUTH_PASSWORD
  if (!email || !password) return null
  return { email, password }
}

/**
 * 从 URL 同步绕过标志（命中即写回 localStorage，之后刷新/深链都保持）。
 *
 * 必须在任何模式判定**之前**调用一次（`main.ts` 启动时），否则首帧的
 * `resolveDeviceMode()` 读到的还是旧值。
 *
 * 用法：`http://localhost:5173/?devAuthBypass=1` 开启，
 * `...?devAuthBypass=0` 撤销。
 */
export function syncDevAuthBypassFromUrl(): void {
  if (!import.meta.env.DEV) return
  try {
    const value = readBypassFlagFromLocation()
    if (value === null) return
    const on = value === '1' || value === 'true'
    if (on) globalThis.localStorage?.setItem(DEV_AUTH_BYPASS_STORAGE_KEY, '1')
    else globalThis.localStorage?.removeItem(DEV_AUTH_BYPASS_STORAGE_KEY)
  } catch {
    /* 存储不可用（隐私模式）时忽略：标志位本就是开发辅助 */
  }
}

/** 仅供开发者手动关闭（控制台 `__chironDisableAuthBypass()`），随后需刷新 */
export function clearDevAuthBypass(): void {
  try {
    globalThis.localStorage?.removeItem(DEV_AUTH_BYPASS_STORAGE_KEY)
  } catch {
    /* 同上 */
  }
}

/** 读镜像；损坏或非法值一律当作"没有镜像"（不抛错，存储可能被禁用） */
export function readMirroredMode(): ChironMode | null {
  try {
    const raw = globalThis.localStorage?.getItem(DEVICE_MODE_STORAGE_KEY) ?? null
    return isChironMode(raw) ? raw : null
  } catch {
    return null
  }
}

function writeMirroredMode(mode: ChironMode): void {
  try {
    globalThis.localStorage?.setItem(DEVICE_MODE_STORAGE_KEY, mode)
  } catch {
    /* 存储不可用（隐私模式）时只保留内存态；模式真相本来就在主进程 */
  }
}

/** `setMode` 成功后的模块级缓存：重启前的窗口期内，各模块读到的是**新**模式 */
let activeMode: ChironMode | null = null

/** 仅供 store / 单测使用：显式设置（或 `null` 清除）模块级模式缓存 */
export function setActiveMode(mode: ChironMode | null): void {
  activeMode = mode
}

/**
 * 解析当前生效模式（同步，无副作用）：
 *   1. `setMode` 后的模块级缓存；
 *   2. Web 端没有桌面外壳 ⇒ 一律 `cloud`（本地模式是桌面独有能力，Web 构建不含本地后端）；
 *   3. preload 暴露的 `mode`（真相）；
 *   4. localStorage 镜像；
 *   5. 兼容降级：外壳尚无 `mode` 字段时，已配置服务地址按 `cloud`（保持既有行为），
 *      全新安装（无地址）按契约默认 `local`。
 */
export function resolveDeviceMode(): ChironMode {
  if (activeMode) return activeMode
  if (!isDesktopHost()) return 'cloud'
  const host = desktopHost()
  // 显式先判 host：可选链上的类型谓词无法把 host 本身缩窄成非空
  if (host && isChironMode(host.mode)) return host.mode
  const mirrored = readMirroredMode()
  if (mirrored) return mirrored
  // 云端模式已废除：既不从 argv 拿到模式、也没有镜像时，应用只可能是本地形态。
  // 曾经这里用「有 apiUrl 就当 cloud」兜底，那在只剩本地的版本里必然误判。
  return 'local'
}

/** 非组件上下文的模式读取入口（axios 拦截器 / 路由守卫） */
export function currentDeviceMode(): ChironMode {
  return resolveDeviceMode()
}

export const useDeviceStore = defineStore('device', () => {
  /** 当前模式（响应式；切换成功后由 `setMode` 更新） */
  const mode = ref<ChironMode>(resolveDeviceMode())
  /** preload 下发的设备设置快照（拿不到时为 null） */
  const settings = ref<DeviceSettings | null>(null)
  const loading = ref(false)
  const switching = ref(false)
  /** 当前 OS 账户（`DOMAIN\username`）；Web 端或老外壳上为空串 */
  const osAccount = ref('')
  /** 清空数据正在进行中（防重复点击；主进程成功后应用会重启） */
  const wiping = ref(false)

  /** 是否运行在桌面外壳里（Web 端无法切换模式） */
  const isDesktop = computed(() => isDesktopHost())

  /** 外壳是否支持「在应用内改服务地址」：Web 端与老 preload 都是 false */
  const canOpenSetup = computed(() => typeof desktopHost()?.openSetup === 'function')

  /** 本地模式：不走登录、不连云端接口 */
  function isLocal(): boolean {
    return mode.value === 'local'
  }

  /** 云端模式：保持既有登录 + cookie/Bearer 行为 */
  function isCloud(): boolean {
    return mode.value === 'cloud'
  }

  /** 应用一个模式：同步 store、模块级缓存与 localStorage 镜像 */
  function applyMode(next: ChironMode): void {
    mode.value = next
    setActiveMode(next)
    writeMirroredMode(next)
  }

  /**
   * 从 preload 拉一次设备设置（幂等；失败不抛错）。
   * 首次挂载/进入设置页时调用：主进程的 `config.json` 才是真相，镜像只是兜底。
   */
  async function loadSettings(): Promise<void> {
    const host = desktopHost()
    // 旧外壳没有 getDeviceSettings：保持解析出的推断值
    if (!host || !host.getDeviceSettings) return
    loading.value = true
    try {
      const next = await host.getDeviceSettings()
      if (!next || typeof next !== 'object') return
      settings.value = next
      if (isChironMode(next.mode)) applyMode(next.mode)
    } catch {
      /* 主进程不可读时保留推断值：模式判断不能拖垮启动 */
    } finally {
      loading.value = false
    }
  }

  /**
   * 读取当前 OS 账户。
   *
   * 失败不抛错：账户显示属于信息性内容，读不到就留空，不该让设置页报错。
   */
  async function loadOsAccount(): Promise<void> {
    const host = desktopHost()
    if (!host || typeof host.getOsAccount !== 'function') return
    try {
      const account = await host.getOsAccount()
      if (typeof account === 'string') osAccount.value = account
    } catch {
      /* 老外壳 / 主进程异常：保持空值 */
    }
  }

  let ready: Promise<void> | null = null

  /** 启动时同步一次设备设置（多次调用共享同一次读取） */
  async function ensureReady(): Promise<void> {
    if (!ready) {
      ready = Promise.all([loadSettings(), loadOsAccount()]).then(() => undefined)
    }
    await ready
  }

  /** 强制重新读取（设置页可见时刷新用） */
  async function refresh(): Promise<void> {
    ready = Promise.all([loadSettings(), loadOsAccount()]).then(() => undefined)
    await ready
  }

  /**
   * 切换模式。
   *
   * 只把"要不要同时删除本地数据"这一个选择透传给主进程 —— 清理哪些内容（cookie /
   * localStorage / IndexedDB / CacheStorage / Service Worker / HTTP 缓存 / token）
   * 由主进程按契约执行，渲染进程**不**自行删除用户数据（误删 `userData/local/**`
   * 是不可逆的，见 contracts.md §8）。
   *
   * 抛错语义：外壳不支持、主进程拒绝时直接抛给调用方（界面据此提示），
   * 不静默吞掉 —— 否则用户以为切了，重启后又回到原模式。
   */
  async function setMode(next: ChironMode, opts: { deleteLocalData?: boolean } = {}): Promise<void> {
    if (!isChironMode(next)) throw new Error(`invalid device mode: ${String(next)}`)
    const host = desktopHost()
    if (!host || !host.setMode) throw new Error('desktop shell does not expose setMode')
    switching.value = true
    try {
      await host.setMode(next, { deleteLocalData: opts.deleteLocalData === true })
      applyMode(next)
      // 主进程已写入 config.json；把本地快照也对齐，避免设置页显示旧值
      if (settings.value) settings.value = { ...settings.value, mode: next }
    } finally {
      switching.value = false
    }
  }

  /**
   * 打开「连接配置页」改服务地址。
   *
   * 界面里不内嵌第二个编辑器：地址的校验、连通性探测与落盘都已经在主进程有一套
   * （`config.cjs` + `setup.html`），复制一份就等于两套口径 —— 而这次要修的正是
   * "改不了地址"：此前唯一的入口是隐藏菜单里的 Ctrl+,。
   *
   * 返回 false 表示外壳不支持（老 preload 没有 `openSetup`），由调用方提示用户。
   * 成功时主进程会重建窗口，本页面随之卸载，不需要在这里收尾。
   */
  /**
   * 本机 Windows Hello 是否可用。
   *
   * 不可用时清空数据必须改验账户密码（主进程不会放行）。界面要**提前**问出来，
   * 而不是等用户点完两次确认才告知"还需要密码"。
   * 老外壳没有这个成员时按「不可用」处理：宁可多要一次密码，也不要少一道验证。
   */
  async function checkHelloAvailability(): Promise<boolean> {
    const host = desktopHost()
    if (!host || typeof host.getHelloAvailability !== 'function') return false
    try {
      const result = await host.getHelloAvailability()
      return Boolean(result && result.available)
    } catch {
      return false
    }
  }

  async function openConnectionSetup(): Promise<boolean> {
    const host = desktopHost()
    if (!host || !host.openSetup) return false
    await host.openSetup()
    return true
  }

  /**
   * 重启应用（契约：切换后必须重启，避免两个后端并存）。
   * 若主进程的 `setMode` 自己已经 `app.relaunch()`，这里多半来不及执行 —— 无副作用。
   */
  async function restart(): Promise<void> {
    const host = desktopHost()
    if (!host || !host.restart) return
    await host.restart()
  }

  /**
   * 清空本机数据（不可恢复）。
   *
   * 不在这里做任何确认 —— 勾选、手打确认词、Windows Hello 都由调用方与主进程负责。
   * store 只把结果**原样**交回：`ok=false` 时调用方要能按 `reason` 区分
   * 「用户没过身份验证」（不该报错）与「清空过程出错」（要给出原因）。
   *
   * 外壳不支持时返回 `{ ok: false, reason: 'unsupported' }` 而不是抛错：
   * 这是能力缺失，不是异常，界面据此禁用按钮即可。
   */
  async function wipeLocalData(password = ''): Promise<WipeLocalDataResult> {
    const host = desktopHost()
    if (!host || !host.wipeLocalData) {
      return { ok: false, reason: 'unsupported', message: 'desktop shell does not expose wipeLocalData' }
    }
    wiping.value = true
    try {
      const result = await host.wipeLocalData(password)
      return result && typeof result === 'object'
        ? result
        : { ok: false, reason: 'wipe-failed', message: 'unexpected response' }
    } finally {
      wiping.value = false
    }
  }

  // 首启把解析值写进镜像：下次 preload 读失败时仍有兜底依据
  writeMirroredMode(mode.value)

  return {
    mode,
    settings,
    loading,
    switching,
    isDesktop,
    osAccount,
    canOpenSetup,
    isLocal,
    isCloud,
    ensureReady,
    refresh,
    setMode,
    restart,
    wiping,
    wipeLocalData,
    checkHelloAvailability,
    openConnectionSetup,
  }
})
