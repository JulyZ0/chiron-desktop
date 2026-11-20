import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { NavigationGuardNext, RouteLocationNormalized } from 'vue-router'

/**
 * 首次运行引导（P1-10）的**自动进入**边界。
 *
 * 守卫侧的规则只有一句话：有登录、无模型、未走完引导 → 把两个主入口（"/"、"/chat"）
 * 送进 /welcome。这里逐条钉住它的"不打扰"与"不死循环"：
 *  · 判定为 false（已有模型 / 已完成）→ 原样放行；
 *  · /welcome 自己、/models 等目标页**永不**被拦 —— 否则「去配置」会立刻被弹回引导；
 *  · 判定抛错 → 放行，不让一次接口抖动卡住导航。
 */

const mocks = vi.hoisted(() => ({
  shouldAutoEnterWelcome: vi.fn(),
  useAuthStoreMock: vi.fn(),
}))

vi.mock('../../api/welcomeStatus', () => ({
  shouldAutoEnterWelcome: (userId?: string | null) => mocks.shouldAutoEnterWelcome(userId),
  WELCOME_ENTRY_PATHS: ['/', '/chat'],
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mocks.useAuthStoreMock(),
}))

import { authGuard } from '../guard'

function makeStore(init: { userId?: string | null; role?: string } = {}) {
  const store = {
    user: init.userId === null ? null : { id: init.userId ?? 'u1', role: init.role ?? 'user' },
    isAuthenticated: true,
    fetchProfile: vi.fn().mockResolvedValue(undefined),
    initToken: vi.fn().mockResolvedValue(undefined),
  }
  mocks.useAuthStoreMock.mockReturnValue(store)
  return store
}

function fakeRoute(path: string, meta: Record<string, unknown> = {}) {
  return { path, fullPath: path, meta } as unknown as RouteLocationNormalized
}

function fakeNext() {
  return vi.fn() as NavigationGuardNext
}

beforeEach(() => {
  localStorage.clear()
  mocks.shouldAutoEnterWelcome.mockReset()
})

describe('authGuard × 首次运行引导', () => {
  it('首页且"无模型 + 未完成" → 跳 /welcome', async () => {
    makeStore()
    mocks.shouldAutoEnterWelcome.mockResolvedValue(true)
    const next = fakeNext()

    await authGuard(fakeRoute('/'), {} as RouteLocationNormalized, next)

    expect(mocks.shouldAutoEnterWelcome).toHaveBeenCalledWith('u1')
    expect(next).toHaveBeenCalledWith('/welcome')
  })

  it('聊天页同样会进引导', async () => {
    makeStore()
    mocks.shouldAutoEnterWelcome.mockResolvedValue(true)
    const next = fakeNext()

    await authGuard(fakeRoute('/chat', { requiresAuth: true }), {} as RouteLocationNormalized, next)

    expect(next).toHaveBeenCalledWith('/welcome')
  })

  it('已有模型 / 已完成 → 不打扰', async () => {
    makeStore()
    mocks.shouldAutoEnterWelcome.mockResolvedValue(false)
    const next = fakeNext()

    await authGuard(fakeRoute('/'), {} as RouteLocationNormalized, next)

    expect(next).toHaveBeenCalledWith()
  })

  it('/welcome 自己不再重定向（防死循环）', async () => {
    makeStore()
    mocks.shouldAutoEnterWelcome.mockResolvedValue(true)
    const next = fakeNext()

    await authGuard(fakeRoute('/welcome', { requiresAuth: true }), {} as RouteLocationNormalized, next)

    expect(mocks.shouldAutoEnterWelcome).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('/models 等引导内目标页不被拦截（点「去配置」不会弹回引导）', async () => {
    makeStore({ role: 'admin' })
    mocks.shouldAutoEnterWelcome.mockResolvedValue(true)
    const next = fakeNext()

    await authGuard(
      fakeRoute('/models', { requiresAuth: true, requiresAdmin: true }),
      {} as RouteLocationNormalized,
      next,
    )

    expect(mocks.shouldAutoEnterWelcome).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('未登录不判定也不跳', async () => {
    makeStore({ userId: null })
    mocks.shouldAutoEnterWelcome.mockResolvedValue(true)
    const next = fakeNext()

    await authGuard(fakeRoute('/'), {} as RouteLocationNormalized, next)

    expect(mocks.shouldAutoEnterWelcome).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledWith()
  })

  it('判定抛错时放行，不卡住导航', async () => {
    makeStore()
    mocks.shouldAutoEnterWelcome.mockRejectedValue(new Error('network'))
    const next = fakeNext()

    await authGuard(fakeRoute('/'), {} as RouteLocationNormalized, next)

    expect(next).toHaveBeenCalledWith()
  })
})
