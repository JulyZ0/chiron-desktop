import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const apiMocks = vi.hoisted(() => ({
  getMetrics: vi.fn(),
  listApiKeys: vi.fn(),
  getOnboardingFunnel: vi.fn(),
  listAdminUsers: vi.fn(),
  createAdminUser: vi.fn(),
  createAdminUserInvite: vi.fn(),
  deleteAdminUser: vi.fn(),
  forceLogoutAdminUser: vi.fn(),
  resetAdminUserPassword: vi.fn(),
  setAdminUserDisabled: vi.fn(),
  updateAdminUser: vi.fn(),
}))

vi.mock('@/api/admin', () => apiMocks)
vi.mock('vue-echarts', () => ({ default: { name: 'VChart', render: () => null } }))

import DashboardView from '../DashboardView.vue'
import UsersView from '../UsersView.vue'

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}))

async function mountView(component: typeof DashboardView | typeof UsersView) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component }],
  })
  await router.push('/')
  return mount(component, { global: { plugins: [createPinia(), router] } })
}

describe('后台核心数据加载与空态', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getMetrics.mockResolvedValue({ concurrent_connections: 2 })
    apiMocks.listApiKeys.mockResolvedValue([])
    apiMocks.getOnboardingFunnel.mockResolvedValue(null)
    apiMocks.listAdminUsers.mockResolvedValue({ users: [], total: 0 })
  })

  it('Dashboard 核心指标失败显示可重试错误，恢复后显示告警空表说明', async () => {
    apiMocks.getMetrics.mockRejectedValueOnce(new Error('offline'))
    apiMocks.getOnboardingFunnel.mockRejectedValue(new Error('funnel unavailable'))
    const wrapper = await mountView(DashboardView)
    await flushPromises()

    const error = wrapper.find('.admin-error-state')
    expect(error.text()).toContain('数据加载失败')
    expect(wrapper.find('.stats-grid').exists()).toBe(false)

    await error.find('button').trigger('click')
    await flushPromises()

    expect(apiMocks.getMetrics).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.admin-error-state').exists()).toBe(false)
    expect(wrapper.find('.stats-grid').exists()).toBe(true)
    expect(wrapper.find('.alert-card .admin-table').text()).toContain('暂无告警')
    expect(wrapper.find('.alert-card .admin-table').text()).toContain('系统运行正常')
    expect(wrapper.find('.alert-card .ant-pagination').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Dashboard 服务商状态接口失败同样进入核心错误态', async () => {
    apiMocks.listApiKeys.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await mountView(DashboardView)
    await flushPromises()

    expect(wrapper.find('.admin-error-state').exists()).toBe(true)
    expect(wrapper.find('.dashboard .nav-grid').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Users 加载失败可重试，成功后恢复成员表格', async () => {
    apiMocks.listAdminUsers
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        users: [{ id: 'u1', email: 'member@example.com', name: '成员', role: 'user', status: 'active' }],
        total: 1,
      })
    const wrapper = await mountView(UsersView)
    await flushPromises()

    const error = wrapper.find('.admin-error-state')
    expect(error.text()).toContain('读取用户列表失败')
    expect(wrapper.find('.admin-table').exists()).toBe(false)

    await error.find('button').trigger('click')
    await flushPromises()

    expect(apiMocks.listAdminUsers).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.admin-error-state').exists()).toBe(false)
    expect(wrapper.find('.admin-table').text()).toContain('member@example.com')
    wrapper.unmount()
  })

  it('Users 空数据提供新建说明且不显示误导分页', async () => {
    const wrapper = await mountView(UsersView)
    await flushPromises()

    expect(wrapper.find('.admin-table-empty-hint').text()).toContain('暂无团队成员')
    expect(wrapper.find('.admin-table-empty-hint').text()).toContain('新建用户')
    expect(wrapper.find('.ant-pagination').exists()).toBe(false)
    wrapper.unmount()
  })
})
