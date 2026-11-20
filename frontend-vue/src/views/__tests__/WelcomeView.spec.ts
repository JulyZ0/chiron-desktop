import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

/**
 * 首次运行引导页（`/welcome`，P1-10）的四步流转。
 *
 * 钉住的四件事：
 *  1. 四步按序推进，每一步都能跳过，最后一步完成后进 `/chat` 且记 done；
 *  2. 已完成的引导再进来只显示完成态，**不再探测也不再跳转**（"重新运行"是显式动作）；
 *  3. 模型就绪时第一步可直接完成（不需要用户先点"重新检查"）；
 *  4. 连通性有组件不可用时主按钮禁用，但"跳过"永远可用 —— 不把用户锁在引导里。
 */

const authState = vi.hoisted(() => ({
  user: { id: 'u1', role: 'admin' } as { id: string; role: string } | null,
  isAdmin: true,
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => authState,
}))

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(),
  getIsolationStatus: vi.fn(),
  fetchComponentsHealth: vi.fn(),
  listKnowledgeBases: vi.fn(),
}))

vi.mock('../../api', () => ({
  listModels: () => mocks.listModels(),
  getIsolationStatus: () => mocks.getIsolationStatus(),
}))

vi.mock('../../api/systemHealth', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/systemHealth')>()
  return { ...actual, fetchComponentsHealth: () => mocks.fetchComponentsHealth() }
})

vi.mock('../../api/knowledge', () => ({
  listKnowledgeBases: () => mocks.listKnowledgeBases(),
}))

import WelcomeView from '../WelcomeView.vue'
import {
  invalidateWelcomeModelCache,
  isWelcomeDone,
  loadWelcomeProgress,
  welcomeStorageKey,
} from '../../api/welcomeStatus'

function healthyComponents() {
  return {
    postgres: { available: true },
    redis: { available: true },
    engine: { healthy: true },
    gateway: { available: true },
  }
}

function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/welcome', component: { template: '<div />' } },
      { path: '/chat', component: { template: '<div />' } },
      { path: '/models', component: { template: '<div />' } },
      { path: '/knowledge', component: { template: '<div />' } },
      { path: '/admin/diagnostics', component: { template: '<div />' } },
    ],
  })
  const pushSpy = vi.spyOn(router, 'push')
  const wrapper = mount(WelcomeView, { global: { plugins: [router] } })
  return { wrapper, pushSpy }
}

function primaryDisabled(wrapper: ReturnType<typeof mount>): boolean {
  return wrapper.get('[data-testid="welcome-primary"]').attributes('disabled') !== undefined
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  invalidateWelcomeModelCache()
  authState.user = { id: 'u1', role: 'admin' }
  authState.isAdmin = true
  mocks.listModels.mockResolvedValue([])
  mocks.fetchComponentsHealth.mockResolvedValue(healthyComponents())
  mocks.listKnowledgeBases.mockResolvedValue([])
  mocks.getIsolationStatus.mockResolvedValue({ effective: 'job', below_floor: false, reasons: [] })
})

describe('WelcomeView 四步流转', () => {
  it('跳过模型 → 连通性通过 → 能力 → 第一条消息进 /chat 并记 done', async () => {
    mocks.listKnowledgeBases.mockResolvedValue([{ id: 'kb1', status: 'ready', enabled: true }])
    const { wrapper, pushSpy } = mountView()
    await flushPromises()

    // ① 没有模型：主按钮不可用，但可以跳过
    expect(wrapper.find('[data-testid="welcome-panel-models"]').exists()).toBe(true)
    expect(primaryDisabled(wrapper)).toBe(true)
    expect(wrapper.text()).toContain('还没有可用模型')

    await wrapper.get('[data-testid="welcome-skip"]').trigger('click')
    await flushPromises()

    // ② 连通性：必需组件全健康 → 可直接完成
    expect(wrapper.find('[data-testid="welcome-panel-connectivity"]').exists()).toBe(true)
    expect(primaryDisabled(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('所有必需组件均可用')

    await wrapper.get('[data-testid="welcome-primary"]').trigger('click')
    await flushPromises()

    // ③ 可选能力：知识库已就绪 + 隔离档位只读展示
    expect(wrapper.find('[data-testid="welcome-panel-capabilities"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('1 个知识库已就绪')
    expect(wrapper.text()).toContain('作业对象')

    await wrapper.get('[data-testid="welcome-primary"]').trigger('click')
    await flushPromises()

    // ④ 第一条消息：完成 → /chat
    expect(wrapper.find('[data-testid="welcome-panel-firstMessage"]').exists()).toBe(true)
    await wrapper.get('[data-testid="welcome-primary"]').trigger('click')
    await flushPromises()

    expect(pushSpy).toHaveBeenCalledWith('/chat')
    expect(isWelcomeDone('u1')).toBe(true)
  })

  it('连通性有组件不可用：主按钮禁用、跳过仍可用', async () => {
    mocks.fetchComponentsHealth.mockResolvedValue({ postgres: { available: true } })
    const { wrapper } = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="welcome-skip"]').trigger('click')
    await flushPromises()

    expect(primaryDisabled(wrapper)).toBe(true)
    expect(wrapper.text()).toContain('有组件不可用')

    await wrapper.get('[data-testid="welcome-skip"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="welcome-panel-capabilities"]').exists()).toBe(true)
  })

  it('模型就绪时第一步直接可完成', async () => {
    mocks.listModels.mockResolvedValue([
      { provider: 'p', name: 'm1', display_name: 'M1', context_window: 1000 },
      { provider: 'p', name: 'm2', display_name: 'M2', context_window: 1000 },
    ])
    const { wrapper } = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('已配置 2 个可用模型')
    expect(primaryDisabled(wrapper)).toBe(false)

    await wrapper.get('[data-testid="welcome-primary"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="welcome-panel-connectivity"]').exists()).toBe(true)
    expect(loadWelcomeProgress('u1').steps.models).toBe('completed')
  })

  it('非管理员不显示「去配置」，但仍提示原因并可跳过', async () => {
    authState.user = { id: 'u1', role: 'user' }
    authState.isAdmin = false
    const { wrapper } = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="welcome-configure"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('需要管理员权限')
    expect(wrapper.find('[data-testid="welcome-skip"]').exists()).toBe(true)
  })

  it('「上一步」回到模型步骤且保留已完成记录', async () => {
    mocks.listModels.mockResolvedValue([
      { provider: 'p', name: 'm1', display_name: 'M1', context_window: 1000 },
    ])
    const { wrapper } = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="welcome-primary"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="welcome-panel-connectivity"]').exists()).toBe(true)
    await wrapper.get('[data-testid="welcome-back"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="welcome-panel-models"]').exists()).toBe(true)
    expect(loadWelcomeProgress('u1').steps.models).toBe('completed')
    expect(primaryDisabled(wrapper)).toBe(false)
  })

  it('「跳过引导」把四步都记为跳过，回首页且不再自动进入', async () => {
    const { wrapper, pushSpy } = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="welcome-skip-all"]').trigger('click')
    await flushPromises()

    expect(pushSpy).toHaveBeenCalledWith('/')
    expect(isWelcomeDone('u1')).toBe(true)
    expect(loadWelcomeProgress('u1').steps.firstMessage).toBe('skipped')
    expect(wrapper.find('[data-testid="welcome-done"]').exists()).toBe(true)
  })
})

describe('WelcomeView 已完成态', () => {
  beforeEach(() => {
    localStorage.setItem(
      welcomeStorageKey('u1'),
      JSON.stringify({
        version: 1,
        step: 'firstMessage',
        steps: {
          models: 'completed',
          connectivity: 'completed',
          capabilities: 'skipped',
          firstMessage: 'completed',
        },
        done: true,
        updatedAt: 1,
      }),
    )
  })

  it('已完成再进入只显示完成态：不探测、不跳转', async () => {
    const { wrapper, pushSpy } = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="welcome-done"]').exists()).toBe(true)
    expect(mocks.listModels).not.toHaveBeenCalled()
    expect(mocks.fetchComponentsHealth).not.toHaveBeenCalled()
    expect(pushSpy).not.toHaveBeenCalled()
  })

  it('「重新运行引导」清空进度并从第一步重新探测', async () => {
    const { wrapper } = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="welcome-restart"]').trigger('click')
    await flushPromises()

    expect(isWelcomeDone('u1')).toBe(false)
    expect(wrapper.find('[data-testid="welcome-panel-models"]').exists()).toBe(true)
    expect(mocks.listModels).toHaveBeenCalledTimes(1)
  })

  it('进度按账号隔离：换账号不继承上一个账号的完成态', async () => {
    authState.user = { id: 'u2', role: 'admin' }
    const { wrapper } = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="welcome-done"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="welcome-panel-models"]').exists()).toBe(true)
  })
})
