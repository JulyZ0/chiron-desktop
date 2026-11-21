import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// partial mock：保留 loadDesktopToken / setAuthToken 的真实实现（Web 端 isDesktop=false，
// 不会写 localStorage），只替换网络客户端。
vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: {
      post: vi.fn(),
      get: vi.fn(),
    },
  }
})

import { useAuthStore } from '../auth'
import { setActiveMode } from '../device'
import { api } from '../../api'
import { privateKey } from '../../utils/privateStorage'

const mockUser = { id: 'u1', email: 'a@b.c', name: 'A', role: 'admin', tenant_id: 1 }

/**
 * localStorage 里**只允许**出现最小字段。
 * email / tenant_id 属敏感信息，被 S 安全修复刻意排除（见 src/stores/auth.ts 的
 * persistUserToStorage / loadUserFromStorage）：刷新后由 GET /v1/auth/profile 回填。
 */
const mockUserStored = { id: 'u1', name: 'A', role: 'admin' }

describe('auth store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    // jsdom 30 兼容：localStorage 可能未正确初始化
    try { localStorage.clear() } catch (e) {
      // Ignore localStorage errors in test environment
      void e
    }
    vi.clearAllMocks()
  })

  describe('login', () => {
    it('成功后持久化 user（token 不进 localStorage，凭 httpOnly cookie）', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok', user: mockUser } } })
      const store = useAuthStore()
      const d = await store.login('a@b.c', 'password')
      expect(d.token).toBe('tok')
      expect(store.token).toBe('tok')
      expect(store.user).toEqual(mockUser)
      // S 安全：token 不写入 localStorage（凭 httpOnly cookie 鉴权）
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(mockUserStored)
      // 请求体带 captcha 字段（空串兜底）
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/login', {
        identifier: 'a@b.c',
        email: 'a@b.c',
        password: 'password',
        captcha_token: '',
        captcha_randstr: '',
      }, { __silent: true })
    })

    it('携带验证码参数时透传到请求体', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok', user: mockUser } } })
      const store = useAuthStore()
      await store.login('a@b.c', 'password', { token: 'cap-token', randstr: 'rnd' })
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/login', {
        identifier: 'a@b.c',
        email: 'a@b.c',
        password: 'password',
        captcha_token: 'cap-token',
        captcha_randstr: 'rnd',
      }, { __silent: true })
    })

    it('响应缺 data 时抛错且不写入状态', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await expect(store.login('a@b.c', 'password')).rejects.toThrow('invalid login response')
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('loading 在失败时也复位', async () => {
      vi.mocked(api.post).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      await expect(store.login('a@b.c', 'password')).rejects.toThrow('network')
      expect(store.loading).toBe(false)
    })
  })

  describe('register', () => {
    it('成功后持久化 user（token 不进 localStorage）', async () => {
      const user = { ...mockUser, role: 'user' }
      vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'rtok', user } } })
      const store = useAuthStore()
      await store.register('a@b.c', 'password', 'Name')
      expect(store.token).toBe('rtok')
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(
        { id: user.id, name: user.name, role: user.role },
      )
    })
  })

  describe('isAdmin', () => {
    it('admin 角色为 true', () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      const store = useAuthStore()
      expect(store.isAdmin).toBe(true)
    })

    it('普通角色为 false', () => {
      localStorage.setItem('user', JSON.stringify({ ...mockUser, role: 'user' }))
      const store = useAuthStore()
      expect(store.isAdmin).toBe(false)
    })

    it('无 user 为 false', () => {
      const store = useAuthStore()
      expect(store.isAdmin).toBe(false)
    })
  })

  describe('store 初始化', () => {
    it('从 localStorage 恢复 user（页面刷新场景；token 内存态为空但 isAuthenticated 凭 user 为 true）', () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      const store = useAuthStore()
      expect(store.token).toBe('')
      // 恢复时 email / tenant_id 为空 —— 它们不落 localStorage，靠 /v1/auth/profile 回填
      expect(store.user).toEqual({ ...mockUserStored, email: '', tenant_id: 0 })
      expect(store.isAuthenticated).toBe(true)
    })

    it('user localStorage 损坏时优雅降级为 null', () => {
      localStorage.setItem('user', '{broken')
      const store = useAuthStore()
      expect(store.user).toBeNull()
    })
  })

  describe('fetchProfile', () => {
    it('成功后更新并持久化 user', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.get).mockResolvedValue({
        data: { data: { user_id: 'u2', email: 'x@y.z', name: 'X', role: 'user', tenant_id: 2 } },
      })
      const store = useAuthStore()
      await store.fetchProfile()
      expect(store.user).toEqual({ id: 'u2', email: 'x@y.z', name: 'X', role: 'user', tenant_id: 2 })
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual({ id: 'u2', name: 'X', role: 'user' })
    })

    it('失败（cookie 无效）时自动登出并清理本地态', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.get).mockRejectedValue(new Error('401'))
      // logout 现调后端清 cookie
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await store.fetchProfile()
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('无 user 时直接跳过（刷新后未登录）', async () => {
      const store = useAuthStore()
      await store.fetchProfile()
      expect(api.get).not.toHaveBeenCalled()
    })
  })

  describe('bootstrapSession', () => {
    it('SSO cookie 引导成功后持久化 user（token 不进 localStorage）', async () => {
      vi.mocked(api.get).mockResolvedValue({ data: { data: { token: 'sso-tok', user: mockUser } } })
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(true)
      expect(store.token).toBe('sso-tok')
      expect(localStorage.getItem('token')).toBeNull()
      expect(JSON.parse(localStorage.getItem('user') || 'null')).toEqual(mockUserStored)
      // withCredentials: true（跨端口携带 httpOnly cookie）
      expect(vi.mocked(api.get)).toHaveBeenCalledWith('/v1/auth/session', { withCredentials: true, __silent: true })
    })

    it('响应无 token 时返回 false', async () => {
      vi.mocked(api.get).mockResolvedValue({ data: { data: {} } })
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(false)
    })

    it('请求失败时返回 false 不抛错', async () => {
      vi.mocked(api.get).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      const ok = await store.bootstrapSession()
      expect(ok).toBe(false)
    })
  })

  describe('logout', () => {
    it('调后端清 httpOnly cookie 并清理本地 user 态', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      const store = useAuthStore()
      await store.logout()
      expect(vi.mocked(api.post)).toHaveBeenCalledWith('/v1/auth/logout', undefined, { __silent: true })
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    it('后端调用失败仍清理本地态（不阻塞登出）', async () => {
      localStorage.setItem('user', JSON.stringify(mockUser))
      vi.mocked(api.post).mockRejectedValue(new Error('network'))
      const store = useAuthStore()
      await store.logout()
      expect(store.token).toBe('')
      expect(store.user).toBeNull()
      expect(localStorage.getItem('user')).toBeNull()
    })

    /**
     * **接线级**：`logout()` 真的调了 `clearPrivateStorage()`（2026-10-09）。
     *
     * `clearPrivateStorage` 本身在 `utils/__tests__/privateStorage.spec.ts` **有单测** ✓，
     * 但"`logout` 到底有没有调它"此前**没人测** ✗ —— 而 `auth.ts:159-160` 的注释记着
     * 一次**实测事故**：「输入历史（`↑` 召回）在换账号后召回了上一个账号发过的内容」✓。
     * 也就是说：**这条断言防的是一次真实发生过的越权读取**，不是假想 ✓。
     *
     * 同时钉住**边界**：UI 偏好（主题等）不属于私有数据，**不该**被登出清掉
     * （清掉会让用户每次登录都丢主题，是另一种回归）✓。
     */
    it('★ 登出清掉私有键（输入历史 / 会话缓存），但不碰 UI 偏好', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      // 私有数据：带账号命名空间
      localStorage.setItem(privateKey('chiron:composer-history', 'u1'), JSON.stringify(['上一个账号发过的话']))
      localStorage.setItem(privateKey('chat_sessions', 'u1'), JSON.stringify([{ id: 's-1' }]))
      // 历史遗留：**没有**账号命名空间的那份（越权来源，按前缀也要清掉）
      localStorage.setItem('chat_sessions', JSON.stringify([{ id: 'legacy' }]))
      // UI 偏好：不属于私有数据
      localStorage.setItem('theme', 'dark')

      const store = useAuthStore()
      await store.logout()

      expect(localStorage.getItem(privateKey('chiron:composer-history', 'u1'))).toBeNull()
      expect(localStorage.getItem(privateKey('chat_sessions', 'u1'))).toBeNull()
      expect(localStorage.getItem('chat_sessions')).toBeNull()   // 遗留裸键也清
      expect(localStorage.getItem('theme')).toBe('dark')          // 偏好保留
    })
  })

  /**
   * 本地模式的「退出登录」= **锁定**，不是销毁会话。
   *
   * 缺陷形态（用户实测报告）：点「退出登录」后整个系统完全不可用 —— 因为本地模式的
   * 身份来自 OS 账户，销毁会话后再重建既有失败风险，也把用户锁在门外；而旧实现
   * 清掉 token 后**立刻** `ensureLocalSession(true)`，等于点了没反应（界面闪一下）。
   *
   * 正确的语义：把界面锁上（`locked=true`），会话原样保留；验证 Windows 账户密码
   * 之后再解锁，用户回到原状态、不丢东西。
   */
  describe('本地模式锁定 / 解锁', () => {
    const originalHost = (globalThis as { chironDesktop?: unknown }).chironDesktop

    beforeEach(() => {
      // 本地模式由 preload 的 host 决定（`resolveDeviceMode` 读 host.mode）
      setActiveMode('local')
    })

    afterEach(() => {
      setActiveMode(null)
      if (originalHost === undefined) delete (globalThis as { chironDesktop?: unknown }).chironDesktop
      else (globalThis as { chironDesktop?: unknown }).chironDesktop = originalHost
    })

    function installHost(verifyLockPassword: (password: string) => Promise<unknown>) {
      ;(globalThis as { chironDesktop?: unknown }).chironDesktop = {
        isDesktop: true,
        mode: 'local',
        verifyLockPassword,
      }
    }

    it('★ 本地模式 logout 只置锁定位，不销毁 user/token、不打后端登出', async () => {
      vi.mocked(api.post).mockResolvedValue({ data: {} })
      localStorage.setItem('user', JSON.stringify(mockUser))

      const store = useAuthStore()
      store.user = mockUser
      store.token = 'local-token'

      await store.logout()

      expect(store.locked).toBe(true)
      // 会话原样保留：身份来自 OS，销毁它只会把用户锁在门外
      expect(store.token).toBe('local-token')
      expect(store.user).toEqual(mockUser)
      // 本地模式没有"登出后端"这一步
      expect(vi.mocked(api.post)).not.toHaveBeenCalledWith('/v1/auth/logout')
    })

    it('★ 解锁失败（密码错）必须保持锁定', async () => {
      installHost(async () => ({ ok: false, reason: 'bad-credential', message: '密码不正确' }))

      const store = useAuthStore()
      store.locked = true

      const result = await store.unlock('wrong')

      expect(result.ok).toBe(false)
      expect(store.locked).toBe(true)
    })

    it('★★ 外壳不支持验证时必须保持锁定（零验证的"解锁"等于没锁）', async () => {
      // 老 preload 没有 verifyLockPassword：这是能力缺失，不是"验证通过"
      ;(globalThis as { chironDesktop?: unknown }).chironDesktop = { isDesktop: true, mode: 'local' }

      const store = useAuthStore()
      store.locked = true

      const result = await store.unlock('anything')

      expect(result.reason).toBe('unsupported')
      expect(store.locked).toBe(true)
    })

    it('解锁成功才解除锁定', async () => {
      installHost(async () => ({ ok: true, reason: 'verified', message: '' }))

      const store = useAuthStore()
      store.locked = true

      const result = await store.unlock('correct')

      expect(result.ok).toBe(true)
      expect(store.locked).toBe(false)
    })

    it('★ 已启用第二因素时启动即锁（否则重启就绕过了验证码）', async () => {
      installHost(async () => ({ ok: true, reason: 'verified', message: '' }))
      ;(globalThis as { chironDesktop?: Record<string, unknown> }).chironDesktop = {
        ...(globalThis as { chironDesktop?: Record<string, unknown> }).chironDesktop,
        getTwoFactorStatus: async () => ({
          ok: true,
          status: { enabled: true, enabledAt: '', recoveryCodesLeft: 3, unreadable: false },
        }),
      }
      vi.mocked(api.post).mockResolvedValue({ data: { data: {} } })

      const store = useAuthStore()
      await store.initToken()

      // initToken 刻意不 await 这次查询（会把首帧拖在 IPC 上），所以等它落地
      await vi.waitFor(() => expect(store.locked).toBe(true))
    })

    it('未启用第二因素时启动不锁（人刚过完 OS 登录，再问一遍是骚扰）', async () => {
      ;(globalThis as { chironDesktop?: Record<string, unknown> }).chironDesktop = {
        isDesktop: true,
        mode: 'local',
        verifyLockPassword: async () => ({ ok: true, reason: 'verified', message: '' }),
        getTwoFactorStatus: async () => ({
          ok: true,
          status: { enabled: false, enabledAt: '', recoveryCodesLeft: 0, unreadable: false },
        }),
      }
      vi.mocked(api.post).mockResolvedValue({ data: { data: {} } })

      const store = useAuthStore()
      await store.initToken()
      // 给它同样的落地机会；断言的是"没有发生"
      await new Promise((r) => setTimeout(r, 0))

      expect(store.locked).toBe(false)
    })

    it('外壳不支持查询状态时不锁（拿不到状态宁可保持原体验，也不能凭空把人挡在门外）', async () => {
      ;(globalThis as { chironDesktop?: unknown }).chironDesktop = { isDesktop: true, mode: 'local' }
      vi.mocked(api.post).mockResolvedValue({ data: { data: {} } })

      const store = useAuthStore()
      await store.initToken()
      await new Promise((r) => setTimeout(r, 0))

      expect(store.locked).toBe(false)
    })

    it('云端模式下 lock() 不生效（锁屏是本地模式的加固，云端走登出）', async () => {
      setActiveMode(null)
      const store = useAuthStore()
      store.lock()
      expect(store.locked).toBe(false)
    })
  })
})
