import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

/**
 * 自检面板（P1-9）的渲染网。
 *
 * 面板在三个入口复用，所以这里钉的是"复用不会把行为改坏"的那几条：
 *  ① 非 admin 整体不渲染、且不发请求（403 不该被渲染成"自检失败"）；
 *  ② 七行状态徽标与说明（模型 / 价格的说明是按数值组句的，不是后端原文）；
 *  ③ 「去修复」只在**有落点且非 ok** 的行出现，点了真的走路由；
 *  ④ 折叠默认收起、刷新无视 5 分钟缓存。
 */

const authState = vi.hoisted(() => ({ isPlatformOwner: true }))

vi.mock('../../../stores/auth', () => ({
  useAuthStore: () => authState,
}))

const mocks = vi.hoisted(() => ({
  get: vi.fn<(url: string) => Promise<unknown>>(),
  listModels: vi.fn<() => Promise<unknown>>(),
  summary: vi.fn<() => Promise<unknown>>(),
}))

vi.mock('../../../api', () => ({
  api: { get: mocks.get },
  listModels: mocks.listModels,
}))

vi.mock('../../../api/admin', () => ({
  getDiagnosticsSummary: mocks.summary,
}))

import SelfCheckPanel from '../SelfCheckPanel.vue'
import { resetSelfCheckForTest } from '../../../composables/useSelfCheck'

const CHECKS = [
  { id: 'schema', title: 'Schema', status: 'ok', detail: '迁移版本一致', fix: '' },
  { id: 'tables', title: '必需表', status: 'warn', detail: '缺 1 张表', fix: '执行迁移' },
  { id: 'knowledge', title: '知识库', status: 'error', detail: '服务未启动', fix: '检查服务' },
  { id: 'checkpoints', title: '检查点', status: 'ok', detail: '已保护', fix: '' },
  { id: 'deps', title: '依赖', status: 'unknown', detail: '', fix: '' },
]

const MODELS = [
  { provider: 'p', name: 'm1', display_name: 'M1', context_window: 1000 },
  { provider: 'p', name: 'm2', display_name: 'M2', context_window: 1000 },
]

const COVERAGE = { period_days: 90, total_models: 4, priced_models: 3, unpriced_models: 1 }

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/schema-mismatch', component: { template: '<div />' } },
      { path: '/knowledge/settings', component: { template: '<div />' } },
      { path: '/admin/deps', component: { template: '<div />' } },
      { path: '/models', component: { template: '<div />' } },
      { path: '/billing', component: { template: '<div />' } },
    ],
  })
}

function mountPanel(props: Record<string, unknown> = {}, router = makeRouter()) {
  const pushSpy = vi.spyOn(router, 'push')
  const wrapper = mount(SelfCheckPanel, { props, global: { plugins: [router] } })
  return { wrapper, router, pushSpy }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSelfCheckForTest()
  authState.isPlatformOwner = true
  mocks.summary.mockResolvedValue({ generated_at: 1, status: 'error', counts: {}, checks: CHECKS })
  mocks.listModels.mockResolvedValue(MODELS)
  mocks.get.mockResolvedValue({ data: { data: COVERAGE } })
})

describe('SelfCheckPanel', () => {
  it('非 admin：面板整体隐藏且不发请求', async () => {
    authState.isPlatformOwner = false
    const { wrapper } = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="self-check-panel"]').exists()).toBe(false)
    expect(mocks.summary).not.toHaveBeenCalled()
    expect(mocks.listModels).not.toHaveBeenCalled()
    expect(mocks.get).not.toHaveBeenCalled()
  })

  it('admin：七行都在，状态徽标与本地化说明正确', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    for (const key of ['schema', 'tables', 'knowledge', 'checkpoints', 'deps', 'models', 'pricing']) {
      expect(wrapper.find(`[data-testid="self-check-item-${key}"]`).exists()).toBe(true)
    }

    const text = (key: string) =>
      wrapper.get(`[data-testid="self-check-status-${key}"]`).text()

    expect(text('schema')).toBe('正常')
    expect(text('tables')).toBe('注意')
    expect(text('knowledge')).toBe('异常')
    expect(text('deps')).toBe('未知')
    expect(text('models')).toBe('正常')

    // 模型 / 价格的说明由前端按数值组句（不显示后端中文原文）
    expect(wrapper.get('[data-testid="self-check-item-models"]').text()).toContain('已启用 2 个模型')
    expect(wrapper.get('[data-testid="self-check-item-pricing"]').text()).toContain(
      '4 个模型中 1 个缺单价',
    )
    // 五项清单直接透传后端事实
    expect(wrapper.get('[data-testid="self-check-item-knowledge"]').text()).toContain('服务未启动')

    // 整体结论：有 error 行 → 异常
    expect(wrapper.get('[data-testid="self-check-overall"]').text()).toBe('异常')
  })

  it('「去修复」按落点出现，点击走对应路由', async () => {
    const { wrapper, pushSpy } = mountPanel()
    await flushPromises()

    // 有落点的行都出按钮（ok 行也算 —— 它在那里是"去改配置"，视觉上退成次要色）
    expect(wrapper.find('[data-testid="self-check-fix-schema"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="self-check-fix-schema"]').classes()).toContain('is-muted')
    expect(wrapper.get('[data-testid="self-check-fix-deps"]').classes()).not.toContain('is-muted')
    // 没有落点的项一律不出按钮：tables 由迁移统一处理，checkpoints 是引擎内部保护
    expect(wrapper.find('[data-testid="self-check-fix-tables"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="self-check-fix-checkpoints"]').exists()).toBe(false)

    await wrapper.get('[data-testid="self-check-fix-schema"]').trigger('click')
    expect(pushSpy).toHaveBeenCalledWith('/schema-mismatch')

    await wrapper.get('[data-testid="self-check-fix-knowledge"]').trigger('click')
    expect(pushSpy).toHaveBeenCalledWith('/knowledge/settings')

    await wrapper.get('[data-testid="self-check-fix-deps"]').trigger('click')
    expect(pushSpy).toHaveBeenCalledWith('/admin/deps')

    await wrapper.get('[data-testid="self-check-fix-models"]').trigger('click')
    expect(pushSpy).toHaveBeenCalledWith('/models')

    await wrapper.get('[data-testid="self-check-fix-pricing"]').trigger('click')
    expect(pushSpy).toHaveBeenCalledWith('/billing')
  })

  it('默认折叠；展开后能看到清单，再点收起', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()

    const toggle = wrapper.get('[data-testid="self-check-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.get('.self-check-body').attributes('style')).toContain('display: none')

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.self-check-body').attributes('style') ?? '').not.toContain('display: none')

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
  })

  it('抽屉形态默认展开', async () => {
    const { wrapper } = mountPanel({ variant: 'drawer', defaultCollapsed: false })
    await flushPromises()

    expect(wrapper.get('[data-testid="self-check-toggle"]').attributes('aria-expanded')).toBe('true')
  })

  it('刷新按钮无视 5 分钟缓存，重新拉取', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()
    expect(mocks.summary).toHaveBeenCalledTimes(1)

    await wrapper.get('[data-testid="self-check-refresh"]').trigger('click')
    await flushPromises()
    expect(mocks.summary).toHaveBeenCalledTimes(2)
  })

  it('清单拉取失败：不抛错、退化为未知行并给出原因', async () => {
    mocks.summary.mockRejectedValue(new Error('gateway down'))
    const { wrapper } = mountPanel()
    await flushPromises()

    const error = wrapper.get('[data-testid="self-check-error"]')
    expect(error.text()).toContain('gateway down')
    expect(wrapper.get('[data-testid="self-check-status-schema"]').text()).toBe('未知')
    // 模型与价格各自独立：清单挂了不影响它们
    expect(wrapper.get('[data-testid="self-check-status-models"]').text()).toBe('正常')
  })
})
