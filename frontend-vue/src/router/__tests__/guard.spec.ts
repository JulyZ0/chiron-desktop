import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { authGuard } from '../guard'
import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'

// mock 掉 pinia store：守卫通过 useAuthStore() 读取会话态
const mocks = vi.hoisted(() => ({
  fetchProfileMock: vi.fn(),
  useAuthStoreMock: vi.fn(),
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mocks.useAuthStoreMock(),
}))

// 真实路由表（`router/index.ts`）在导入时会挂进度条与 schema 漂移探测。
// 本文件只关心路由元数据与守卫判定，两者都替换成空实现，避免导入时发起探测请求。
vi.mock('../progress', () => ({ setupRouteProgress: vi.fn() }))
vi.mock('../schemaMismatch', () => ({ setupSchemaMismatchWatch: vi.fn() }))

function makeStore(init: {
  role?: string | null
  isAuthenticated?: boolean
  isApiKeySession?: boolean
}) {
  const store = {
    user: init.role !== undefined ? { id: 'u1', email: 'a@b.c', name: 'A', role: init.role, tenant_id: 't1' } : null,
    isAuthenticated: init.isAuthenticated ?? !!init.role,
    isApiKeySession: init.isApiKeySession ?? false,
    mustChangePassword: false,
    fetchProfile: mocks.fetchProfileMock,
    initToken: vi.fn().mockResolvedValue(undefined),
  }
  mocks.useAuthStoreMock.mockReturnValue(store)
  return store
}

function fakeRoute(meta: Record<string, unknown>) {
  return { meta, fullPath: '/x' } as unknown as RouteLocationNormalized
}

function fakeNext() {
  const next = vi.fn() as NavigationGuardNext
  return next
}

describe('authGuard', () => {
  beforeEach(() => {
    localStorage.clear()
    mocks.fetchProfileMock.mockReset()
    mocks.fetchProfileMock.mockResolvedValue(undefined)
  })

  it('requiresAuth 路由无 user 时重定向登录页并带上回跳地址', async () => {
    makeStore({ role: null, isAuthenticated: false })
    const next = fakeNext()
    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith({ path: '/login', query: { redirect: '/x' } })
  })

  it('首登必须改密：已登录但 mustChangePassword 时拦到改密页', async () => {
    const store = makeStore({ role: 'user', isAuthenticated: true })
    store.mustChangePassword = true
    const next = fakeNext()
    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith('/change-password')
  })

  it('改密页本身不被强制改密判定再次拦截', async () => {
    const store = makeStore({ role: 'user', isAuthenticated: true })
    store.mustChangePassword = true
    const next = fakeNext()
    await authGuard({ meta: { requiresAuth: true }, path: '/change-password', fullPath: '/change-password' } as unknown as RouteLocationNormalized, {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith()
  })

  it('requiresAuth 路由有 user 时放行', async () => {
    makeStore({ role: 'user', isAuthenticated: true })
    const next = fakeNext()
    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith()
  })

  it('公开路由无 user 放行', async () => {
    makeStore({ role: null, isAuthenticated: false })
    const next = fakeNext()
    await authGuard(fakeRoute({}), {} as RouteLocationNormalized, next)
    expect(next).toHaveBeenCalledWith()
  })

  it('meta.public（分享落地页）匿名访问时不拉 profile', async () => {
    // 匿名访客没有身份可拉；多打一次 /v1/auth/profile 只会在控制台留一条 401，
    // 且在云端残留态下还会触发登出逻辑。
    makeStore({ role: null, isAuthenticated: false })
    const next = fakeNext()
    await authGuard(fakeRoute({ public: true }), {} as RouteLocationNormalized, next)
    expect(mocks.fetchProfileMock).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('★ meta.public 不等待鉴权初始化（initToken 挂住也必须放行）', async () => {
    // 这是"分享页永远转圈"的直接根因：守卫在组件挂载**之前** `await initToken()`，
    // 而它可能等主进程 IPC、等本地会话、等一次开发态登录。对匿名访客来说这些都是
    // 无关的等待，且没有超时 —— 不 settle 就等于页面永远停在加载态。
    // 用"永不 resolve"的 initToken 证明：公开路由不碰它。
    const store = makeStore({ role: null, isAuthenticated: false })
    store.initToken = vi.fn(() => new Promise<void>(() => {}))
    const next = fakeNext()

    await authGuard(fakeRoute({ public: true }), {} as RouteLocationNormalized, next)

    expect(store.initToken).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('非公开路由仍然等待 token 初始化（首屏请求要带 Bearer）', async () => {
    const store = makeStore({ role: 'user', isAuthenticated: true })
    const next = fakeNext()

    await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)

    expect(store.initToken).toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('★ 首登改密残留不影响公开分享页（访客不该被送到 /change-password）', async () => {
    // 同一个浏览器里上一个登录账号可能留下 `mustChangePassword`。分享页是匿名访客
    // 打开的只读页面，与任何账号的改密状态无关；把它一起拦走，访客看到的是改密页。
    const store = makeStore({ role: 'user', isAuthenticated: true })
    store.mustChangePassword = true
    const next = fakeNext()

    await authGuard(fakeRoute({ public: true }), {} as RouteLocationNormalized, next)

    expect(next).toHaveBeenCalledWith()
  })

  it('★ 真实路由表把 /s/:token 与 /share/:id 都标成 public', async () => {
    // 守卫只能按 meta 判定：只要哪条分享路由漏了 `meta.public`，匿名访客打开它就会
    // 被先拉一次 profile（401）、或被 requiresAuth 弹去登录页。因此**路由元数据本身**
    // 也是这条链路的被测对象。
    const { default: router } = await import('../index')

    for (const [path, name] of [['/s/tok-1', 'ShareShort'], ['/share/tok-1', 'Share']] as const) {
      const resolved = router.resolve(path)
      expect(resolved.name).toBe(name)
      expect(resolved.meta.public).toBe(true)
      expect(resolved.meta.requiresAuth).toBeFalsy()
      expect(resolved.meta.requiresAdmin).toBeFalsy()
    }
  })

  describe('requiresAdmin', () => {
    it('后端 profile 返回 admin 放行', async () => {
      const store = makeStore({ role: 'user' }) // localStorage 为 user
      // 模拟后端权威 profile 返回 admin -> 覆盖本地
      store.fetchProfile.mockImplementation(() => {
        store.user = { id: 'u1', email: 'a@b.c', name: 'A', role: 'admin', tenant_id: 't1' }
        store.isAuthenticated = true
        return Promise.resolve(undefined)
      })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })

    it('本地 role 被篡改为 owner,但后端返回普通 user 时拒绝放行(S 修复)', async () => {
      const store = makeStore({ role: 'owner' }) // 本地被篡改为 owner
      store.fetchProfile.mockImplementation(() => {
        store.user = { id: 'u1', email: 'a@b.c', name: 'A', role: 'user', tenant_id: 't1' }
        store.isAuthenticated = true
        return Promise.resolve(undefined)
      })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('普通用户（已登录）重定向首页 /', async () => {
      const store = makeStore({ role: 'user', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('API Key 会话即使 role=owner 也不能进管理员页', async () => {
      makeStore({ role: 'owner', isAuthenticated: true, isApiKeySession: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('未登录（无 user）重定向登录页并带上回跳地址', async () => {
      makeStore({ role: null, isAuthenticated: false })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith({ path: '/login', query: { redirect: '/x' } })
    })

    it('backend profile 拉取失败时按本地态降级(绝不误放行 admin)', async () => {
      const store = makeStore({ role: 'user', isAuthenticated: true })
      store.fetchProfile.mockRejectedValue(new Error('network'))
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true, requiresAdmin: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('非 admin 路由不受 user 角色影响', async () => {
      makeStore({ role: 'user', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })
  })

  // 本地模式：服务端就是跑在本机的同一个网关，身份由 `POST /v1/auth/session` 免口令
  // 下发（internal/api/auth_local_session.go）。守卫因此**不再跳过判定**，
  // 只有"云端独有"的页面继续被 meta.cloudOnly 拦掉。
  describe('本地模式', () => {
    beforeEach(() => {
      ;(globalThis as Record<string, unknown>).chironDesktop = { isDesktop: true, mode: 'local' }
    })
    afterEach(() => {
      delete (globalThis as Record<string, unknown>).chironDesktop
    })

    it('cloudOnly 页面（登录/注册/计费）回首页，不进 501', async () => {
      makeStore({ role: 'owner', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ cloudOnly: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith('/')
    })

    it('管理后台可达：会话返回 owner 即放行', async () => {
      const store = makeStore({ role: 'owner', isAuthenticated: true })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresAdmin: true }),
        {} as RouteLocationNormalized,
        next,
      )
      // 角色必须来自后端会话，不是 localStorage 里那一份
      expect(mocks.fetchProfileMock).toHaveBeenCalled()
      expect(next).toHaveBeenCalledWith()
      expect(store.user?.role).toBe('owner')
    })

    it('本地也拒绝伪 admin：会话里是普通 user 时不进管理后台', async () => {
      const store = makeStore({ role: 'owner', isAuthenticated: true }) // 本地存储被篡改
      store.fetchProfile = mocks.fetchProfileMock.mockImplementation(() => {
        store.user = { id: 'u1', email: 'a@b.c', name: 'A', role: 'user', tenant_id: 't1' }
        return Promise.resolve(undefined)
      })
      const next = fakeNext()
      await authGuard(
        fakeRoute({ requiresAuth: true, requiresAdmin: true }),
        {} as RouteLocationNormalized,
        next,
      )
      expect(next).toHaveBeenCalledWith('/')
    })

    it('会话还没建立时不误拦：本地模式没有登录页可弹', async () => {
      makeStore({ role: null, isAuthenticated: true })
      const next = fakeNext()
      await authGuard(fakeRoute({ requiresAuth: true }), {} as RouteLocationNormalized, next)
      expect(next).toHaveBeenCalledWith()
    })
  })
})