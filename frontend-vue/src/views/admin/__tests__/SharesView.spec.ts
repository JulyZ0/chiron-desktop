import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `SharesView`（后台「分享管理」）的三语适配。
 *
 * 这一页曾经整页显示成原始 key（`admin.share_col_title` 之类）：文案在
 * `locales/<lang>/admin.ts` 里被多包了一层 `share: { … }`，真实路径是
 * `admin.share.share_col_title`，而源码按 `admin.share_col_title` 引用 ——
 * vue-i18n 找不到键就把 key 原样显示，且不报错（fallbackWarn 在生产是关的）。
 *
 * 所以这里钉两层：
 *   1. 源码引用的每个 `admin.share_*` 键在三语里都真实存在（防再被包一层）；
 *   2. 表头随界面语言切换而更新（防 `t()` 在 setup 顶层只求值一次）。
 */

// antd 的 Table 走 responsiveObserve，jsdom 没有 matchMedia（同 SettingsPanel.smoke.spec.ts）。
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
  })) as unknown as typeof window.matchMedia
}

const apiMocks = vi.hoisted(() => ({
  listAdminShares: vi.fn(),
  revokeAdminShare: vi.fn(),
  revokeAdminShares: vi.fn(),
}))

vi.mock('@/api/admin', () => ({
  listAdminShares: apiMocks.listAdminShares,
  revokeAdminShare: apiMocks.revokeAdminShare,
  revokeAdminShares: apiMocks.revokeAdminShares,
}))

import SharesView from '../SharesView.vue'
import { i18n, setLocale } from '@/i18n'

/** 源码里实际引用的分享页键（改 SharesView 时同步维护；漏一个就会显示原始 key）。 */
const SHARE_KEYS = [
  'admin.share_management',
  'admin.share_management_desc',
  'admin.share_search_placeholder',
  'admin.share_col_title',
  'admin.share_col_owner',
  'admin.share_col_messages',
  'admin.share_col_created',
  'admin.share_col_expires',
  'admin.share_col_actions',
  'admin.share_revoke',
  'admin.share_empty',
  'admin.share_untitled',
  'admin.share_load_failed',
  'admin.share_revoke_failed',
  // P1-8 分享运营：统计列 / 批量撤销 / 详情
  'admin.share_col_views_24h',
  'admin.share_col_views_total',
  'admin.share_col_last_viewed',
  'admin.share_detail',
  'admin.share_detail_title',
  'admin.share_detail_close',
  'admin.share_never_viewed',
  'admin.share_trend_7d',
  'admin.share_bulk_selected',
  'admin.share_bulk_revoke',
  'admin.share_bulk_confirm_text',
  'admin.share_bulk_confirm_ok',
  'admin.share_bulk_cancel',
  'admin.share_bulk_done',
  'admin.share_bulk_partial',
  'admin.share_bulk_failed',
]

const LANGUAGES = ['zh-CN', 'en-US', 'ar'] as const

/** 与服务端 AdminShareItem 形状一致的最小行。 */
function share(overrides: Record<string, unknown> = {}) {
  return {
    share_id: 'shr_abc123',
    session_id: 'ses_1',
    title: '发布前的检查清单',
    user_id: 'u_1',
    user_email: 'ops@example.com',
    message_count: 12,
    created_at: '2026-11-01T10:00:00Z',
    expires_at: '2026-12-01T10:00:00Z',
    views_24h: 4,
    views_total: 27,
    last_viewed_at: '2026-11-20T08:00:00Z',
    daily: [
      { date: '2026-11-14', count: 0 },
      { date: '2026-11-15', count: 2 },
      { date: '2026-11-16', count: 0 },
      { date: '2026-11-17', count: 5 },
      { date: '2026-11-18', count: 1 },
      { date: '2026-11-19', count: 3 },
      { date: '2026-11-20', count: 4 },
    ],
    ...overrides,
  }
}

function mountView() {
  return mount(SharesView, { attachTo: document.body })
}

/** 表头文本（antd-vue 渲染成 thead th）。 */
function headerTexts(wrapper: ReturnType<typeof mountView>): string[] {
  return wrapper.findAll('thead th').map(th => th.text())
}

describe('SharesView 的三语适配', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listAdminShares.mockResolvedValue([share()])
    setLocale('zh-CN', { persist: false })
  })

  afterEach(() => {
    // 恢复源语言：i18n 实例是全局单例，别让语言泄漏到后续用例。
    setLocale('zh-CN', { persist: false })
  })

  it('分享页引用的键在 zh-CN / en-US / ar 三语里都存在', () => {
    const missing: string[] = []
    for (const lang of LANGUAGES) {
      for (const key of SHARE_KEYS) {
        // te(key, locale) 才检查指定语言；不带 locale 会落到当前语言。
        if (!i18n.global.te(key, lang)) missing.push(`${lang}:${key}`)
      }
    }
    expect(missing, `缺失或路径错位的文案键:\n${missing.join('\n')}`).toEqual([])
  })

  it('表头随界面语言切换而更新，而不是停在 setup 时的语言', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(headerTexts(wrapper)).toContain('会话标题')

    setLocale('en-US', { persist: false })
    await flushPromises()
    expect(headerTexts(wrapper)).toContain('Conversation')
    expect(headerTexts(wrapper)).not.toContain('会话标题')

    setLocale('ar', { persist: false })
    await flushPromises()
    expect(headerTexts(wrapper)).toContain('المحادثة')

    wrapper.unmount()
  })

  it('界面不是中文时不再显示原始 key', async () => {
    setLocale('en-US', { persist: false })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).not.toContain('admin.share_')

    wrapper.unmount()
  })
})

/**
 * ── P1-8 分享运营：统计列 / 批量撤销 / 详情 ──
 *
 * 三条都守"运营能不能真的做处置"：
 *  - 统计列要显示服务端聚合值（不是前端自己数）；
 *  - 批量撤销必须经过一次就地二次确认（撤销会让已发出的链接立刻 410）；
 *  - 详情面板的趋势图必须是**固定 7 个点**（服务端缺日补 0，前端按长度画）。
 */
describe('SharesView 的分享运营能力', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listAdminShares.mockResolvedValue([share()])
    apiMocks.revokeAdminShares.mockResolvedValue({
      requested: 1,
      revoked: 1,
      revoked_ids: ['shr_abc123'],
      failed: [],
    })
    setLocale('zh-CN', { persist: false })
  })

  afterEach(() => {
    setLocale('zh-CN', { persist: false })
  })

  it('展示 24h / 累计 / 最近访问三列', async () => {
    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('24 小时访问')
    expect(text).toContain('累计访问')
    expect(text).toContain('最近访问')
    // 行内数值来自服务端聚合
    expect(text).toContain('27')

    wrapper.unmount()
  })

  it('批量撤销必须二次确认，确认后按 id 批量提交', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 选中第一行（antd 的行选择复选框）
    const box = wrapper.find('tbody .ant-checkbox-input')
    expect(box.exists()).toBe(true)
    await box.setValue(true)
    await flushPromises()

    // 第一次点击只是展开确认，不发请求
    const ask = wrapper.find('.share-bulk-ask')
    expect(ask.exists()).toBe(true)
    await ask.trigger('click')
    await flushPromises()
    expect(apiMocks.revokeAdminShares).not.toHaveBeenCalled()
    expect(wrapper.find('.share-bulk-confirm-text').text()).toContain('1')

    await wrapper.find('.share-bulk-confirm').trigger('click')
    await flushPromises()

    expect(apiMocks.revokeAdminShares).toHaveBeenCalledWith(['shr_abc123'])
    // 撤销成功的行从本地列表移除
    expect(wrapper.text()).not.toContain('shr_abc123')

    wrapper.unmount()
  })

  it('取消二次确认不会发请求', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('tbody .ant-checkbox-input').setValue(true)
    await flushPromises()
    await wrapper.find('.share-bulk-ask').trigger('click')
    await flushPromises()
    await wrapper.find('.share-bulk-cancel').trigger('click')
    await flushPromises()

    expect(apiMocks.revokeAdminShares).not.toHaveBeenCalled()
    expect(wrapper.find('.share-bulk-confirm').exists()).toBe(false)

    wrapper.unmount()
  })

  it('详情面板展示趋势图（7 个点的 polyline，不引图表库）', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('.share-detail-btn').trigger('click')
    await flushPromises()

    const panel = wrapper.find('.share-detail')
    expect(panel.exists()).toBe(true)
    const line = panel.find('.share-spark-line')
    expect(line.exists()).toBe(true)
    // 7 天固定 7 个坐标点
    expect((line.attributes('points') ?? '').trim().split(/\s+/).length).toBe(7)
    expect(panel.text()).toContain('27')

    wrapper.unmount()
  })
})
