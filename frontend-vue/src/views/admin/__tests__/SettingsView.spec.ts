import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `SettingsView` 的「Agent 运行配置」卡片（后台「全局配置」页）。
 *
 * 这一块最容易撒的两个谎，正是这里要钉住的：
 *
 *   1. **把「后台从没保存过」显示成「引擎关掉了这项」**。后端对未配置项返回空，
 *      直接绑到表单上会让 Switch 变成未勾选、InputNumber 变成空 —— 用户看到的是
 *      一个从未生效过的策略。所以初装时必须显示**引擎实际在用的默认值**。
 *   2. **只提交改动过的字段**。开关的「关」和「没设过」在 JSON 里都长得像"没有"，
 *      但语义相反：漏提交 = 后台永远删不掉这一项，用户关不掉提醒。
 */

const apiMocks = vi.hoisted(() => ({
  saveSettings: vi.fn(),
  getSettings: vi.fn(),
  getAgentSettings: vi.fn(),
  saveAgentSettings: vi.fn(),
}))

// 只 mock 网络函数，常量与归一化用真实实现 —— 测试要钉的正是那份默认值。
vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    saveSettings: apiMocks.saveSettings,
    getSettings: apiMocks.getSettings,
    getAgentSettings: apiMocks.getAgentSettings,
    saveAgentSettings: apiMocks.saveAgentSettings,
  }
})

import SettingsView from '../SettingsView.vue'
import { AGENT_SETTINGS_DEFAULTS } from '@/api/admin'

// antd 的 `Row`/`Col` 走 responsiveObserve，jsdom 没有 matchMedia —— 不给桩，
// mount 阶段就会 `window.matchMedia is not a function`，与页面本身无关。
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

const mountView = () => mount(SettingsView)

const numberInput = (w: ReturnType<typeof mountView>, test: string) =>
  w.find(`[data-test="${test}"]`)

/** InputNumber 会把 8 显示成 "8.0"，比数值而不是比字符串。 */
const numberValue = (w: ReturnType<typeof mountView>, test: string) =>
  Number((numberInput(w, test).element as HTMLInputElement).value)

describe('SettingsView 的「Agent 运行配置」', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getSettings.mockResolvedValue({})
    apiMocks.getAgentSettings.mockResolvedValue({ ...AGENT_SETTINGS_DEFAULTS })
  })

  it('初装显示引擎实际在用的默认值，而不是留空', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 归一化后的默认值必须逐字落到控件上：留空会让用户以为这项被关掉了。
    expect(numberValue(wrapper, 'agent-no-progress-threshold')).toBe(3)
    expect(numberValue(wrapper, 'agent-token-window-ratio')).toBe(8)
    expect(numberValue(wrapper, 'agent-interaction-wait')).toBe(240)
    wrapper.unmount()
  })

  it('后端已保存过值时以已保存值为准', async () => {
    apiMocks.getAgentSettings.mockResolvedValue({
      ...AGENT_SETTINGS_DEFAULTS,
      no_progress_threshold: 6,
      token_window_ratio: 2.5,
    })

    const wrapper = mountView()
    await flushPromises()

    expect(numberValue(wrapper, 'agent-no-progress-threshold')).toBe(6)
    expect(numberValue(wrapper, 'agent-token-window-ratio')).toBe(2.5)
    wrapper.unmount()
  })

  it('保存提交完整配置，而不是只提交改动过的字段', async () => {
    const wrapper = mountView()
    await flushPromises()

    await numberInput(wrapper, 'agent-no-progress-threshold').setValue('5')
    await wrapper.find('[data-test="agent-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveAgentSettings).toHaveBeenCalledTimes(1)
    expect(apiMocks.saveAgentSettings).toHaveBeenCalledWith({
      ...AGENT_SETTINGS_DEFAULTS,
      no_progress_threshold: 5,
    })
    wrapper.unmount()
  })
})
