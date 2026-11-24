import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

/**
 * `HomeView` 重构成工作台仪表盘（最近会话 + 快捷入口 + 用量概览）后的接线网。
 *
 * 钉住三件**改坏了不会报错、只会静默出错**的事：
 * ① **概览数字的口径**：`usage_events` 的按天视图里 `tx_count` 是**每天**的调用数，
 *    "近 7 天调用次数"必须把它**累加**。少加一步，界面只会显示成一天的量，没人报错。
 * ② **两块数据互不牵连**：会话拉失败时用量卡片仍要出数（反之亦然）。首页是入口页，
 *    任何一块坏掉都只该影响它自己 —— 一起变成错误态等于把整页废掉。
 * ③ **快速命令走同一条实现**：必须调 `executeQuickCommand`（建 uni 会话 → /v1/quick-execute
 *    → 跳 /chat?task=），不能在这里另起一套请求；且空白输入不得触发。
 */

// jsdom 没有 IntersectionObserver / ResizeObserver，而 Hero 背景组件用它们做懒启动。
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}
if (!('IntersectionObserver' in globalThis)) {
  vi.stubGlobal('IntersectionObserver', NoopObserver)
}
if (!('ResizeObserver' in globalThis)) {
  vi.stubGlobal('ResizeObserver', NoopObserver)
}

const apiMocks = vi.hoisted(() => ({
  // 调用签名带 url：下面的 routeGet() 按 URL 分派响应；实现本身忽略入参
  get: vi.fn<(url: string) => Promise<unknown>>(() => Promise.resolve({ data: { data: [] } })),
}))

const quickMocks = vi.hoisted(() => ({
  executeQuickCommand: vi.fn((): Promise<unknown> => Promise.resolve({ sessionId: 'uni_1', title: 't' })),
}))

vi.mock('../../api', () => ({ api: { get: apiMocks.get } }))

// 快速命令的真实实现会 import 全局路由实例：在视图测试里换成替身，
// 既避免把整张路由表拉进来，又能断言"确实复用了同一条路径"。
vi.mock('../../components/WorkstationNav.vue', () => ({
  default: { name: 'WorkstationNav', render: () => null },
  executeQuickCommand: quickMocks.executeQuickCommand,
}))

// three.js 的 WebGL 场景在 jsdom 里没有意义，HomeView 的 CSS 渐变光晕本就是它的兜底。
vi.mock('../../components/home/HomeScene3D.vue', () => ({
  default: { name: 'HomeScene3D', render: () => null },
}))

import HomeView from '../HomeView.vue'
import { useAuthStore } from '../../stores/auth'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
    { path: '/agents', component: { template: '<div />' } },
    { path: '/memory', component: { template: '<div />' } },
    { path: '/skills', component: { template: '<div />' } },
    { path: '/knowledge', component: { template: '<div />' } },
    { path: '/plugins', component: { template: '<div />' } },
  ],
})

const SESSIONS = [
  // 别名优先于标题：用户改过的名字比自动标题更可读
  { id: 's-1', title: '自动标题', alias: '季度分析', updated_at: '2026-11-17T10:00:00Z' },
  { id: 's-2', title: '只有标题', updated_at: '2026-11-16T09:00:00Z' },
]

// 近 7 天：3 + 2 + 1 = 6 次调用，花费合计 42 分
const USAGE = {
  daily: [
    { day: '2026-11-15', tx_count: 3, credits_spent: 30, credits_added: 0 },
    { day: '2026-11-16', tx_count: 2, credits_spent: 10, credits_added: 0 },
    { day: '2026-11-17', tx_count: 1, credits_spent: 2, credits_added: 0 },
  ],
  total_spent: 42,
  period_days: 7,
}

/** 按 URL 分派响应；`overrides` 用来单独让某一路失败 */
function routeGet(overrides: { sessions?: unknown; usage?: unknown } = {}) {
  apiMocks.get.mockImplementation((url: string): Promise<unknown> => {
    if (url === '/v1/conversations') {
      if (overrides.sessions === 'reject') return Promise.reject(new Error('boom'))
      return Promise.resolve({ data: { data: overrides.sessions ?? SESSIONS } })
    }
    if (url === '/v1/billing/usage') {
      if (overrides.usage === 'reject') return Promise.reject(new Error('boom'))
      return Promise.resolve({ data: { data: overrides.usage ?? USAGE } })
    }
    return Promise.resolve({ data: { data: [] } })
  })
}

function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  // 直接写 store 的 ref：不进登录流程，避免多打一次接口
  useAuthStore().user = {
    id: 'u-1', email: '', name: 'me', role: 'user', tenant_id: 1,
    password_set: true, must_change_password: false,
  }
  return mount(HomeView, {
    global: {
      plugins: [router, pinia],
      // 语言 / 主题切换依赖各自的 store 与 matchMedia；首页测试只验内容与数据口径。
      stubs: { LanguageSwitcher: true, ThemeSwitcher: true },
    },
  })
}

describe('HomeView（工作台仪表盘）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    routeGet()
    quickMocks.executeQuickCommand.mockResolvedValue({ sessionId: 'uni_1', title: 't' })
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 请求口径：会话取一屏、用量固定按天看近 7 天', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/v1/conversations', { params: { per_page: 6 } })
    // groupBy/range 传错不会报错，只会静默返回另一种口径的数据
    expect(apiMocks.get).toHaveBeenCalledWith('/v1/billing/usage', {
      params: { groupBy: 'day', range: '7d' },
    })
    wrapper.unmount()
  })

  it('★ 最近会话渲染：别名优先，没有别名退回标题', async () => {
    const wrapper = mountView()
    await flushPromises()

    const rows = wrapper.findAll('.session-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('季度分析')
    expect(rows[0].text()).not.toContain('自动标题')
    expect(rows[1].text()).toContain('只有标题')
    wrapper.unmount()
  })

  it('★ 概览数字：调用次数是 daily.tx_count 的**累加**，不是某一天', async () => {
    const wrapper = mountView()
    await flushPromises()

    const values = wrapper.findAll('.usage-value').map(v => v.text())
    expect(values[0]).toBe('6') // 3 + 2 + 1
    expect(values[1]).toBe('42') // total_spent，单位分（与计费页同口径）
    wrapper.unmount()
  })

  it('★ 故障隔离：会话挂了，用量概览照常出数', async () => {
    routeGet({ sessions: 'reject' })
    const wrapper = mountView()
    await flushPromises()

    // 会话区：空状态 + 加载失败文案
    expect(wrapper.find('.session-list').exists()).toBe(false)
    expect(wrapper.text()).toContain('加载失败')
    // 用量区：数字仍然在
    expect(wrapper.findAll('.usage-value').map(v => v.text())).toEqual(['6', '42'])
    wrapper.unmount()
  })

  it('★ 故障隔离：用量挂了，会话列表照常渲染', async () => {
    routeGet({ usage: 'reject' })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.findAll('.session-row')).toHaveLength(2)
    expect(wrapper.find('.usage-stats').exists()).toBe(false)
    expect(wrapper.text()).toContain('加载失败')
    wrapper.unmount()
  })

  it('★ 快速命令：复用 executeQuickCommand，成功后清空输入', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 用 placeholder 定位而不是 `.quick-input input`：antd 的 Input 会把 class 落到
    // `<input>` 自身（无 prefix/suffix 时不额外包一层），后代选择器匹配不到。
    const input = wrapper.find('input[placeholder*="交代任务"]')
    await input.setValue('  统计上季度营收  ')
    await wrapper.find('.quick-run').trigger('click')
    await flushPromises()

    expect(quickMocks.executeQuickCommand).toHaveBeenCalledWith('统计上季度营收')
    expect((input.element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('★ 快速命令：空白输入不触发', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="交代任务"]').setValue('   ')
    await wrapper.find('.quick-run').trigger('click')
    await flushPromises()

    expect(quickMocks.executeQuickCommand).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('点会话行会带 ?session= 跳到 /chat', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.findAll('.session-row')[0].trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/chat')
    expect(router.currentRoute.value.query.session).toBe('s-1')
    wrapper.unmount()
  })
})
