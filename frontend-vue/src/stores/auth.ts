import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, desktopDeviceToken, loadDesktopToken, setAuthToken } from '../api'
import { clearPrivateStorage } from '../utils/privateStorage'
import { useDeviceStore, isDevAuthBypassEnabled, devAuthCredentials } from './device'
import type { LockVerifyResult } from './device'
import { applyServerLocale } from '../utils/localeSync'

export interface User {
  id: string
  email: string
  username?: string
  name: string
  /** 展示名（欢迎语/个人信息页）；为空时回落到 name */
  nickname?: string
  role: string
  tenant_id: number
  /** 是否已由用户自己设置过密码；false = 本地 owner 首次设置 */
  password_set?: boolean
  /** 首登/管理员重置后必须改密（团队模式） */
  must_change_password?: boolean
  /** 用户设置（换肤、朗读音色等）。由 GET /v1/auth/profile 下发、PUT 局部合并。 */
  settings?: Record<string, unknown>
  /** 权限清单（profile 下发）。API Key 会话只有剔除管理面后的业务权限。 */
  perms?: string[]
}

// S 安全修复：仅持久化最小必要字段（id, role, name），不含 email/tenant_id 等敏感信息
interface UserStored {
  id: string
  role: string
  name: string
  nickname?: string
}

function loadUserFromStorage(): User | null {
  try {
    const raw = localStorage.getItem('user')
    if (!raw) return null
    const stored = JSON.parse(raw) as UserStored
    // 从存储恢复最小字段，其余字段由后端 profile 填充
    return {
      id: stored.id, role: stored.role, name: stored.name,
      nickname: stored.nickname, email: '', tenant_id: 0,
    }
  } catch {
    return null
  }
}

function persistUserToStorage(u: User | null) {
  if (u) {
    const stored: UserStored = { id: u.id, role: u.role, name: u.name, nickname: u.nickname }
    localStorage.setItem('user', JSON.stringify(stored))
  } else {
    localStorage.removeItem('user')
  }
}

/** 本次会话的凭据形态：交互式登录 vs API Key。界面据此收起管理面。 */
export type AuthMethod = 'session' | 'api_key'

const AUTH_METHOD_KEY = 'chiron.auth_method'

function loadAuthMethod(): AuthMethod | null {
  try {
    const raw = localStorage.getItem(AUTH_METHOD_KEY)
    return raw === 'api_key' || raw === 'session' ? raw : null
  } catch {
    return null
  }
}

function persistAuthMethod(method: AuthMethod | null) {
  try {
    if (method) localStorage.setItem(AUTH_METHOD_KEY, method)
    else localStorage.removeItem(AUTH_METHOD_KEY)
  } catch {
    /* 存不了只影响刷新后的界面收敛，不影响本次会话 */
  }
}

export const useAuthStore = defineStore('auth', () => {
  /**
   * 运行模式：本地模式没有登录流程（本地后端的第一批只给一个固定的本地用户），
   * 但它**仍然是**一个会话 —— 界面需要 `user` 来显示名字/角色、需要 token 走 Bearer。
   */
  const device = useDeviceStore()
  const isLocalMode = () => device.mode === 'local'

  const user = ref<User | null>(loadUserFromStorage())
  /** 凭据形态（session/api_key）；决定管理面是否可见 */
  const authMethod = ref<AuthMethod | null>(loadAuthMethod())
  /** API Key 会话：后端已剔除管理权限，界面也不应再给出那些入口 */
  const isApiKeySession = computed(() => authMethod.value === 'api_key')

  function setAuthMethod(method: AuthMethod | null) {
    authMethod.value = method
    persistAuthMethod(method)
  }
  // S 安全：Web 端 token 不再持久化到 localStorage（XSS 偷不走），仅保留内存态；
  // 桌面端 file:// 跨站拿不到 SameSite=Lax cookie，必须持久化 token 走 Bearer。
  const token = ref<string>('')
  let tokenInit: Promise<void> | null = null

  /** 统一写入内存 token 与请求层 Bearer（桌面端同时持久化，刷新后仍可用） */
  function applyToken(t: string) {
    token.value = t
    // 归属由模式决定：本地会话只进本地桶，云端登录态才进 safeStorage。
    // 混用会让"云端 token 被发给本机后端"这种 401 变得无法解释（见 api/index.ts）。
    setAuthToken(t, isLocalMode() ? 'local' : 'cloud')
  }

  /**
   * 桌面端 token 由 safeStorage 异步解密。main.ts 在挂载前 await，路由守卫再兜底；
   * 幂等，多次调用只读一次加密文件。Web 端立即返回。
   */
  async function initToken(): Promise<void> {
    if (!tokenInit) {
      tokenInit = (async () => {
        // 本地模式：优先建立**本地会话**（不读云端 token —— 那份 token 属于另一个后端，
        // 带过去只会 401/501）。
        //
        // 这里刻意**不 await**：main.ts 会等 initToken 再 mount，而本地后端可能还在启动，
        // 一个挂住的请求能让窗口迟迟不出第一帧。本地后端第一批允许无鉴权调用，会话稍后
        // 到达即可（user / token 都是响应式的）。
        if (isLocalMode()) {
          // 设备 token 先落地：它由 preload **同步**注入，不用等本地后端就绪。
          // 请求层（src/api/index.ts 的 requestToken）本地模式正是靠它带 Bearer，
          // 所以即使 /v1/auth/session 还没回来，开了本地鉴权的 core 也不会拒请求。
          const deviceToken = desktopDeviceToken()
          if (deviceToken) applyToken(deviceToken)
          void ensureLocalSession()
          // 已启用第二因素时**启动即锁**。
          //
          // `locked` 不持久化是有意的（没开 2FA 时，人刚过完 OS 登录，再问一次
          // 同一个密码是骚扰）。但那个理由在启用 2FA 之后不成立了：重启就能绕过
          // 验证码，等于"开了也白开" —— 正是用户报的"启用形同虚设"。
          void lockIfSecondFactorEnabled()
          return
        }
        // 开发环境绕过：浏览器直连 vite 时既没有 preload、也没有登录页。
        // 只放宽 `isAuthenticated` 是不够的 —— 业务请求照样 401，页面全是空数据
        // （上一版就是这样：每个请求都 401、每页弹十几条"登录已失效"）。
        // 这里用 `.env.local` 里的开发凭据**真登录一次**，拿到真 token。
        if (isDevAuthBypassEnabled()) {
          const creds = devAuthCredentials()
          if (!creds) {
            console.warn(
              '[dev-auth-bypass] enabled but VITE_DEV_AUTH_EMAIL / VITE_DEV_AUTH_PASSWORD '
              + 'are not set (put them in frontend-vue/.env.local, which is not committed). '
              + 'Pages will show empty data due to 401.',
            )
            return
          }
          // 超时上限：后端没起时若等 axios 默认的 30s，首屏会白屏半分钟。
          // 超时后登录仍在后台继续 —— 成功的话 token 稍后落地，后续请求照样带得上。
          let timer: ReturnType<typeof setTimeout> | undefined
          const loginPromise = login(creds.email, creds.password)
          // 竞速结束后这两条 promise 仍可能 settle：各自挂空 catch，避免
          // unhandled rejection 把控制台刷满真错误。
          loginPromise.catch(() => {})
          const timeoutPromise = new Promise<never>((_, reject) => {
            timer = setTimeout(() => reject(new Error('dev auto-login timeout (5s)')), 5000)
          })
          timeoutPromise.catch(() => {})
          try {
            await Promise.race([loginPromise, timeoutPromise])
          } catch (err) {
            // 后端没起 / 凭据过期时只记录，不阻塞挂载：绕过模式下守卫已放宽，
            // 界面仍可进入，只是数据为空。
            console.warn('[dev-auth-bypass] dev auto-login failed:', err)
          }
          return
        }
        // Web：先尝试用 httpOnly cookie 恢复会话（刷新/新标签页）。
        // 没有 cookie 时静默失败，由路由守卫送去登录页 —— 这里绝不能弹"登录过期"。
        if (await bootstrapSession()) return
        const stored = await loadDesktopToken()
        if (stored) {
          applyToken(stored)
          // 桌面端重启后从 safeStorage 恢复：`sk-` 前缀即 API Key 会话。
          // 没有这个判定，界面会把一把受限凭据当成交互式登录，重新露出管理入口。
          setAuthMethod(stored.startsWith('sk-') ? 'api_key' : 'session')
        }
      })()
    }
    return tokenInit
  }

  /**
   * 本地会话：`POST /v1/auth/session` 换本地用户 + 设备 token（contracts.md §4）。
   *
   * 幂等：已有 user + token 时直接返回 true。失败**不抛错** —— 本地后端可能还没
   * 启动完（首启 / 重启窗口），把启动流程拖死在 token 初始化里不值得；
   * 下一次 `initToken` / 设置页可见时会重试。
   */
  async function ensureLocalSession(force = false): Promise<boolean> {
    if (!force && user.value && token.value) return true
    try {
      // `__retainAcrossNavigation`：首屏路由导航会 `requestManager.cancelAll()`，
      // 而这次导航往往正好赶在这个请求还没回来的时候（打包版 file:// 冷启动必现）。
      // 被取消 ⇒ token 永远为空 ⇒ 之后所有 `/v1` 请求缺 Authorization ⇒ 全场 401。
      // 见 utils/requestOptimizer.ts 的 `cancelAll`。
      const response = await api.post('/v1/auth/session', undefined, {
        __retainAcrossNavigation: true,
      })
      const d = response.data?.data
      if (!d?.user) return false
      // token 以**设备 token**（preload 注入）为准：core 校验的就是它
      //（CHIRON_DEVICE_TOKEN 优先于落盘文件，见 desktop-local/app/api/auth.py）。
      // 两者不一致（注入值被 core 落盘值盖住）时，用错那个在开了鉴权的 core 上全场 401。
      const localToken = desktopDeviceToken() || d.token || ''
      if (!localToken) return false
      applyToken(localToken)
      setAuthMethod('session')
      user.value = d.user
      persistUserToStorage(d.user)
      return true
    } catch {
      return false
    }
  }

  const loading = ref(false)

  /**
   * 本地模式「锁定」状态。
   *
   * 本地模式的身份来自 OS 账户（一个 Windows 用户 = 一个应用用户），网关按本机
   * 回环来源免口令签发会话 —— **坐到这台机器前的人就自动是账户主人**。共用工作站上
   * 这不够：登录 Windows 后人走开、屏幕没锁，回来的是另一个人。
   *
   * 所以「退出登录」在本地模式下不是销毁会话（身份来自 OS，销毁了也重建得出来，
   * 而且还把用户锁在门外），而是**锁定界面**，再用该账户的 Windows 凭据解锁。
   *
   * 定位要说清：这是**加固**，不是访问控制。token 仍留在内存里，锁定挡的是
   * "旁人直接操作这个窗口"；真正的隔离靠 OS 账户本身（数据目录按账户 ACL 隔离）。
   * 正因如此，`locked` 刻意**不持久化** —— 刷新/重启即解锁，那时人已经重新经过了
   * OS 登录，再要他输一次密码只是骚扰。
   */
  const locked = ref(false)

  /**
   * 显式锁定界面（本地模式）。
   *
   * 这是「两步验证真的会被问到」的第一条路：`logout()` 在本地模式也走锁定，
   * 但那是个语义上叫"退出登录"的菜单项 —— 用户想的是离开账户，不是暂时挡住屏幕。
   * 缺一个叫"锁定"的入口时，共用工作站上的人根本不会去点"退出登录"来走开。
   *
   * 幂等：已锁定时再点一次不产生任何可见变化（菜单会被遮罩挡住，本来就点不到）。
   */
  function lock() {
    if (!isLocalMode()) return
    locked.value = true
  }

  /**
   * 启动时按「是否启用了第二因素」决定要不要锁。
   *
   * 为什么不能无条件启动即锁：没开 2FA 时，人刚在 OS 登录界面输过 Windows 密码，
   * 应用再问一遍同一个密码只是骚扰。而启用 2FA 之后情形正相反 —— 不锁的话，
   * 重启应用就绕过了验证码，"启用"等于没启用。
   *
   * 查询失败（老外壳没有 getTwoFactorStatus、主进程还没就绪）一律**不锁**：
   * 拿不到状态时宁可保持原来的体验，也不要凭空把人挡在门外。
   */
  async function lockIfSecondFactorEnabled(): Promise<void> {
    if (!isLocalMode()) return
    const status = await device.getTwoFactorStatus()
    if (status?.enabled) locked.value = true
  }

  // 本地模式：没有"登录"这回事，界面一律视为已登录（否则每个 requiresAuth 页都会弹回登录页，
  // 而登录页本身在本地模式是被拦截的云专属路由）。
  //
  // 开发环境绕过（DEV only）：浏览器直连 vite 时没有 preload，模式只能判成 cloud，
  // 于是没登录态就进不去任何 requiresAuth 页。`isDevAuthBypassEnabled()` 在生产构建里
  // 恒为 false（编译期 DCE），这里只是让它把"已登录"这条判断放宽一层。
  const isAuthenticated = computed(() =>
    isLocalMode() || isDevAuthBypassEnabled() ? true : !!user.value,
  )
  // S 修复：owner 角色拥有全部 admin 权限，isAdmin 须同时识别 admin + owner
  //
  // 开发环境绕过：没有登录态时 role 恒为 undefined，`/models`、审计等 admin 页
  // 会被守卫弹回首页。绕过标志位开启时视同 admin —— 它只在 DEV 构建里可能为 true。
  const isAdmin = computed(() =>
    !isApiKeySession.value &&
    (isDevAuthBypassEnabled() || user.value?.role === 'admin' || user.value?.role === 'owner'),
  )

  /**
   * 平台 owner：`/v1/admin/diagnostics/summary`、跨租户监控等端点只对它开放。
   * 与 Go 的 `isPlatformOwner(claims)` 同口径（`role == "owner"`）—— 多写一条
   * tenant 判断就会让团队 owner 明明有权限却在界面上看不到自检。
   */
  const isPlatformOwner = computed(
    () => !isApiKeySession.value && (isDevAuthBypassEnabled() || user.value?.role === 'owner'),
  )

  /** 登录：`identifier` 可以是邮箱或用户名（团队模式两者都支持）。 */
  async function login(identifier: string, password: string, captcha?: { token: string; randstr?: string }) {
    loading.value = true
    try {
      // `__silent`：密码错误的 401 由登录页就地展示，不能被全局拦截器
      // 当成"会话过期"再弹一次 toast / 跳转。
      const response = await api.post('/v1/auth/login', {
        identifier,
        email: identifier,
        password,
        captcha_token: captcha?.token || '',
        captcha_randstr: captcha?.randstr || '',
      }, { __silent: true })
      const d = response.data?.data
      if (!d) throw new Error('invalid login response')
      // Web 走 httpOnly cookie；桌面端 file:// 跨站拿不到 cookie，applyToken 同时
      // 把 token 交给请求层走 Bearer（仅桌面端持久化）。
      applyToken(d.token)
      setAuthMethod('session')
      user.value = d.user
      persistUserToStorage(d.user)
      return d
    } finally {
      loading.value = false
    }
  }

  /**
   * API Key 登录（远程模式）。
   *
   * API Key 以 `Authorization: Bearer sk-...` 发出 —— Go 网关按前缀把它识别为
   * API Key 通道（与 JWT 并列，见 internal/api/middleware.go）。这里先用 profile
   * 验活，失败立刻清掉，避免把无效 key 持久化成"已登录"。
   */
  async function loginWithApiKey(rawKey: string) {
    const key = rawKey.trim()
    if (!key) throw new Error('api key is required')
    loading.value = true
    try {
      applyToken(key)
      const response = await api.get('/v1/auth/profile', { __silent: true })
      const data = response.data?.data
      if (!data || !data.user_id) throw new Error('invalid api key')
      user.value = {
        id: data.user_id,
        email: data.email || '',
        name: data.name || data.email || 'API Key',
        role: data.role,
        tenant_id: data.tenant_id || 0,
        settings: data.settings,
        perms: Array.isArray(data.perms) ? data.perms : undefined,
      }
      setAuthMethod('api_key')
      persistUserToStorage(user.value)
      return user.value
    } catch (err) {
      applyToken('')
      setAuthMethod(null)
      throw err
    } finally {
      loading.value = false
    }
  }

  async function register(
    email: string,
    password: string,
    name: string,
    captcha?: { token: string; randstr?: string },
    emailCode?: string,
  ) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/register', {
        email,
        password,
        name,
        // 后台开启「注册邮箱验证」时必填，未开启时后端忽略
        email_code: emailCode || '',
        captcha_token: captcha?.token || '',
        captcha_randstr: captcha?.randstr || '',
      })
      const d = response.data?.data
      if (!d) throw new Error('invalid register response')
      applyToken(d.token)
      user.value = d.user
      persistUserToStorage(d.user)
      return d
    } finally {
      loading.value = false
    }
  }

  async function fetchProfile() {
    // 本地模式：权威身份来自本地会话。`POST /v1/auth/session` 返回的 user.role 就是
    // 后端行里的值（网关链路）或与设备 token 等价的本机身份（core 链路），
    // 不再多打一次 GET /v1/auth/profile —— 那条端点只有网关有，core 没有。
    if (isLocalMode()) {
      await ensureLocalSession()
      return
    }
    if (!user.value) return
    try {
      const response = await api.get('/v1/auth/profile')
      const data = response.data?.data
      if (!data) throw new Error('invalid profile response')
      user.value = {
        id: data.user_id,
        email: data.email,
        name: data.name || data.email,
        role: data.role,
        tenant_id: data.tenant_id || 0,
        settings: data.settings,
        perms: Array.isArray(data.perms) ? data.perms : undefined,
      }
      // 以服务端为准：同一份 token 可能来自 API Key 或交互式登录，
      // 前端不能只靠自己记忆的登录方式（刷新/恢复后尤其如此）。
      setAuthMethod(data.auth_method === 'api_key' ? 'api_key' : 'session')
      persistUserToStorage(user.value)
      // 按服务端语言偏好对齐界面（仅在本机未显式选择过语言时生效）。
      // 服务端渲染的通知邮件只能读 users.settings.locale，这里同步是为了让
      // 「不同设备上语言一致」，而非覆盖用户当下的选择。
      applyServerLocale(data.settings)
    } catch {
      // Cookie 无效，清除本地态
      await logout()
    }
  }

  /** SSO 回跳引导：凭 httpOnly cookie 换取会话信息（SSO 登录后建立本地 user 态） */
  async function bootstrapSession(): Promise<boolean> {
    try {
      // `__silent`：匿名访客没有 cookie 时这里必然 401，静默失败即可，
      // 由调用方决定去登录页还是继续。
      const response = await api.get('/v1/auth/session', { withCredentials: true, __silent: true })
      const d = response.data?.data
      if (!d?.token) return false
      // 桌面端同样走 Bearer：SSO 回跳的 cookie 在 file:// 下也不会被带上
      applyToken(d.token)
      setAuthMethod('session')
      user.value = d.user
      persistUserToStorage(d.user)
      return true
    } catch {
      return false
    }
  }

  /** 自助改密（含首登强制）。成功后服务端会重签 Cookie，旧会话全部失效。 */
  async function changePassword(oldPassword: string, newPassword: string) {
    loading.value = true
    try {
      const response = await api.post('/v1/auth/password', {
        old_password: oldPassword,
        new_password: newPassword,
      }, { __silent: true })
      const d = response.data?.data
      if (d?.token) applyToken(d.token)
      if (d?.user) {
        user.value = d.user
        persistUserToStorage(d.user)
      }
      return d
    } finally {
      loading.value = false
    }
  }

  /** 当前会话是否被要求先改密（团队模式首登/重置后）。 */
  const mustChangePassword = computed(() => user.value?.must_change_password === true)

  /** 建立/覆盖本地会话（短信登录等直接拿到 {token, user} 的入口复用） */
  function applySession(t: string, u: User) {
    if (!t) throw new Error('invalid session: empty token')
    applyToken(t)
    setAuthMethod('session')
    user.value = u
    persistUserToStorage(u)
  }

  // S 安全：logout 调后端清 httpOnly cookie（否则登出后 cookie 仍有效，鉴权不失效）
  async function logout() {
    // 本地模式：**锁定**，不销毁会话。
    //
    // 这里此前有两版错法：
    //   · 旧版清掉 token 后立刻 `ensureLocalSession(true)` —— 等于没登出（界面闪一下）；
    //   · 只清不清地删掉重建 —— 变成"点一次登出，整个应用永久不可用"（用户实测报告）。
    //
    // 正确的语义是锁定：身份来自 OS 账户，销毁会话既没必要（重建得出来）也危险
    // （重建失败就把用户锁在门外）。所以只把界面锁上，`user` / `token` 原样保留，
    // 解锁后回到原状态、不丢任何东西。
    if (isLocalMode()) {
      locked.value = true
      return
    }
    try {
      await api.post('/v1/auth/logout', undefined, { __silent: true })
    } catch {
      // 网络失败仍清理本地态
    }
    applyToken('')
    setAuthMethod(null)
    user.value = null
    persistUserToStorage(null)
    // 私有数据（输入历史、会话缓存…）必须随登出清掉：localStorage 是浏览器级的，
    // 不清就会在下一次登录时被**另一个账号**读到（实测：输入历史 `↑` 召回了他人内容）。
    clearPrivateStorage()
  }

  /**
   * 解锁（本地模式）：验证当前 Windows 账户密码。
   *
   * 密码只交给主进程验证，**不进内存态、不落盘、不写日志**。返回结构化的
   * `LockVerifyResult`，调用方据此区分"密码错"（可重试）与"验证不可用"
   * （断网 / 域控不可达 —— 用户没错，重试也不会好）。
   *
   * 验证通过才清 `locked`。**任何失败都保持锁定** —— 包括外壳不支持
   * （`unsupported`）：零验证的"解锁"等于没锁。
   */
  async function unlock(password: string, secondFactor = ''): Promise<LockVerifyResult> {
    if (!locked.value) return { ok: true, reason: 'verified', message: '' }
    const result = await device.verifyLockPassword(password, secondFactor)
    if (result.ok) locked.value = false
    return result
  }

  return {
    user,
    token,
    loading,
    /** 本地模式「锁定」状态：为 true 时界面被全屏遮罩挡住，只能用 OS 凭据解锁 */
    locked,
    isAuthenticated,
    isAdmin,
    isPlatformOwner,
    isApiKeySession,
    initToken,
    ensureLocalSession,
    login,
    loginWithApiKey,
    register,
    changePassword,
    mustChangePassword,
    fetchProfile,
    bootstrapSession,
    applySession,
    lock,
    logout,
    unlock,
  }
})
