import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 设置面板的冒烟网（抽 composable 前置）。
 *
 * 为什么先补这几条：`SettingsPanel.vue` 是 1236 行且零测试的组件，页面与头像弹窗
 * 两个入口共用它。在这种体量上直接抽 composable 等于盲改 —— 搬错一个 `ref` 的归属
 * 只会静默显示旧值，不报错。这里钉住的是前两个子域里最容易搬坏的东西：
 * 「账户资料」的初始值与提交载荷、「三方绑定」的三态互斥与已绑定过滤。
 *
 * 选择器一律用组件/文案，不为测试往模板里加 `data-test` 钩子。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}))

const authStoreMock = vi.hoisted(() => ({
  user: { name: 'Alice', email: 'alice@example.com', settings: {} as Record<string, unknown> },
  fetchProfile: vi.fn(async () => {}),
}))

const deviceStoreMock = vi.hoisted(() => ({
  isLocal: () => false,
  isDesktop: false,
  // 账户页挂载时会读一次系统账户（ensureReady 读设置 + OS 账户）。
  // 这里是替身：ensureReady 必须存在，否则 onMounted 直接 TypeError。
  ensureReady: vi.fn(async () => {}),
  osAccount: 'CORPlice',
}))

const themeStoreMock = vi.hoisted(() => ({
  preference: 'light',
  accent: '#000000',
  toggleTheme: vi.fn(),
  setAccent: vi.fn(),
}))

// antd 的 Tabs / Slider 走 responsiveObserve，jsdom 没有 matchMedia。
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

vi.mock('../../../api', () => ({ api: apiMocks }))
vi.mock('../../../stores/auth', () => ({ useAuthStore: () => authStoreMock }))
vi.mock('../../../stores/theme', () => ({ useThemeStore: () => themeStoreMock }))
vi.mock('../../../stores/device', () => ({ useDeviceStore: () => deviceStoreMock }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))

/**
 * `tallyCache` / `scheduleTally` 是「延后 + TTL 缓存」的组合器，这里压平成直通：
 * 测试要的是「取到值后视图变成什么样」，不是延后本身的时序。
 */
vi.mock('../../../utils/tallyCache', () => ({
  tallyCache: {
    load: vi.fn(async (_key: string, fetcher: () => Promise<unknown>) => fetcher()),
  },
  scheduleTally: vi.fn(
    async (
      _cache: unknown,
      _key: string,
      fetcher: () => Promise<unknown>,
      onValue: (v: unknown) => void,
      hooks: { onStart?: () => void; onError?: (e: unknown) => void },
    ) => {
      hooks.onStart?.()
      try {
        onValue(await fetcher())
      } catch (e) {
        hooks.onError?.(e)
      }
      return () => {}
    },
  ),
}))

import SettingsPanel from '../SettingsPanel.vue'
import { i18n } from '../../../i18n'

const tr = (key: string, args?: Record<string, unknown>): string =>
  (i18n.global as unknown as { t: (k: string, a?: Record<string, unknown>) => string }).t(key, args)

const STUBS = {
  HarnessProfilePanel: true,
  RunModePanel: true,
  LanguageSwitcher: true,
}

function mountAccount() {
  return mount(SettingsPanel, {
    props: { initialTab: 'account' },
    global: { plugins: [i18n], stubs: STUBS },
  })
}

function buttonByText(wrapper: ReturnType<typeof mountAccount>, text: string) {
  const hit = wrapper.findAll('button').find(b => b.text().includes(text))
  if (!hit) throw new Error(`找不到按钮：${text}`)
  return hit
}

function accountBlocks(wrapper: ReturnType<typeof mountAccount>) {
  return wrapper
    .findAll('.setting-block')
    .filter(b => b.text().includes(tr('settings.os_account')))
}

describe('SettingsPanel 账户页签', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    deviceStoreMock.osAccount = 'CORPlice'
  })

  it('只保留系统账户块', async () => {
    const wrapper = mountAccount()
    await flushPromises()

    expect(accountBlocks(wrapper)).toHaveLength(1)
  })

  it('基本资料 / 三方绑定 / 手机号 / 设置密码 已移除', async () => {
    const wrapper = mountAccount()
    await flushPromises()
    const text = wrapper.text()

    // 这四块是云端租户概念的残留在桌面端毫无意义，删掉后不应再出现在 DOM 里
    expect(text).not.toContain(tr('settings.basic_profile'))
    expect(text).not.toContain(tr('auth.third_party_account_binding'))
    expect(text).not.toContain(tr('auth.set_password'))
    // 手机号：文案键与「验证码」同时消失才算真删干净
    expect(text).not.toContain(tr('auth.verificationCode'))
  })

  it('不再为已删除的三方绑定发请求', async () => {
    mountAccount()
    await flushPromises()

    // 删块之前 onMounted 会打 SSO 身份 + 可绑定列表；删块之后这两次请求必须一起消失，
    // 否则只是把界面藏起来、后端照样被无意义地打一遍。
    const paths = apiMocks.get.mock.calls.map(c => String(c[0]))
    expect(paths.some(p => p.includes('/auth/sso/'))).toBe(false)
    expect(paths.some(p => p.includes('/auth/sms/'))).toBe(false)
  })

  it('系统账户为空时整块隐藏（Web 构建拿不到 OS 账户）', async () => {
    deviceStoreMock.osAccount = ''
    const wrapper = mountAccount()
    await flushPromises()

    expect(accountBlocks(wrapper)).toHaveLength(0)
  })
})
