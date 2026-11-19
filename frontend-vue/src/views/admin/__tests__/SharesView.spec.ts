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
}))

vi.mock('@/api/admin', () => ({
  listAdminShares: apiMocks.listAdminShares,
  revokeAdminShare: apiMocks.revokeAdminShare,
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
