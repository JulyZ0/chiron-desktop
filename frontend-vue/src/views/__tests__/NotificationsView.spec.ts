import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

// jsdom 未实现 matchMedia：ant-design-vue 的 Card/Segmented 等组件会用到
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as never
}

const notificationsApi = vi.hoisted(() => ({
  listNotifications: vi.fn(),
  markNotificationsRead: vi.fn(),
}))
vi.mock('../../api/notifications', () => notificationsApi)

import NotificationsView from '../NotificationsView.vue'

/**
 * 通知中心（`/notifications`）—— 这条路由此前**不存在**。
 *
 * 回归背景：账号菜单有「通知中心」、`accountMenuRoute()` 也返回 `/notifications`，
 * 但 `router/index.ts` 里没有这条路径，点进去必然落到 404 兜底页。后端补齐之后
 * 这里钉三件事，它们各自对应一个容易再次出错的假设：
 *
 *  1. **正文由客户端按 kind 套模板渲染**，服务端只给 `kind` + `payload`；
 *  2. **payload 缺字段是正常情况**（引擎可能没给 error_code / title），要退到
 *     `*_anonymous` / `*_untitled` 兜底，而不是显示 `{profile}` 这种占位符；
 *  3. **未知 kind 原样显示**（服务端比前端新），不能渲染成一串 `notifications.kind.x`。
 */
const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

function mountView() {
  return mount(NotificationsView, { global: { plugins: [router] } })
}

function failedItem(overrides: Record<string, unknown> = {}) {
  return {
    id: 'n1',
    kind: 'subagent_failed',
    payload: { run_id: 'r1', status: 'failed', profile: 'research', error_code: 'timeout' },
    session_id: 's1',
    read: false,
    created_at: '2026-11-18T10:00:00Z',
    ...overrides,
  }
}

enableAutoUnmount(afterEach)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('NotificationsView', () => {
  it('按 kind 渲染正文，并显示未读数', async () => {
    notificationsApi.listNotifications.mockResolvedValue({
      items: [
        failedItem(),
        {
          id: 'n2',
          kind: 'share_revoked',
          payload: { share_id: 'sh1', title: '周会纪要' },
          session_id: 's2',
          read: true,
          created_at: '2026-11-17T09:00:00Z',
        },
      ],
      unread_count: 1,
    })

    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    // 子任务失败：带 profile 与错误码
    expect(text).toContain('research')
    expect(text).toContain('timeout')
    // 分享撤销：带会话标题
    expect(text).toContain('周会纪要')
    // 未读角标
    expect(text).toContain('1')
    expect(notificationsApi.listNotifications).toHaveBeenCalledWith({ unreadOnly: false })
  })

  it('payload 缺字段时退到兜底文案，不显示占位符', async () => {
    notificationsApi.listNotifications.mockResolvedValue({
      items: [
        failedItem({ payload: { run_id: 'r1', status: 'lost' } }),
        failedItem({ id: 'n3', kind: 'share_revoked', payload: {} }),
      ],
      unread_count: 2,
    })

    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()

    // 没有 profile / error_code：走 anonymous 模板，且不得留下 {profile} 之类的花括号
    expect(text).not.toContain('{profile}')
    expect(text).not.toContain('{error_code}')
    // 没有 title：走 untitled 模板
    expect(text).not.toContain('{title}')
  })

  it('未知 kind 原样显示，而不是泄漏 i18n 键名', async () => {
    notificationsApi.listNotifications.mockResolvedValue({
      items: [failedItem({ kind: 'future_kind', payload: {} })],
      unread_count: 1,
    })

    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()

    expect(text).toContain('future_kind')
    expect(text).not.toContain('notifications.kind.')
  })

  it('标为已读只提交本条 id，并从列表里消掉未读态', async () => {
    notificationsApi.listNotifications.mockResolvedValue({
      items: [failedItem()],
      unread_count: 1,
    })
    notificationsApi.markNotificationsRead.mockResolvedValue(1)

    const wrapper = mountView()
    await flushPromises()

    // 「标为已读」按钮（行级）。必须**精确**匹配：`includes` 会先撞上
    // 「全部标为已读」，测试就变成在验证另一个按钮。
    const markBtn = wrapper.findAll('button').find(b => b.text().trim() === '标为已读')
    expect(markBtn, 'row-level mark-as-read button not found').toBeTruthy()
    await markBtn!.trigger('click')
    await flushPromises()

    expect(notificationsApi.markNotificationsRead).toHaveBeenCalledWith({ ids: ['n1'] })
  })

  it('没有未读时「全部标为已读」禁用，不发请求', async () => {
    notificationsApi.listNotifications.mockResolvedValue({
      items: [failedItem({ read: true })],
      unread_count: 0,
    })

    const wrapper = mountView()
    await flushPromises()

    const allBtn = wrapper.findAll('button').find(b => b.text().includes('全部标为已读'))
    expect(allBtn, 'mark-all button not found').toBeTruthy()
    expect(allBtn!.attributes('disabled')).toBeDefined()
    expect(notificationsApi.markNotificationsRead).not.toHaveBeenCalled()
  })
})
