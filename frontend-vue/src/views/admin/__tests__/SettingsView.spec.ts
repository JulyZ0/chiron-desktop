import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { Modal } from 'ant-design-vue'

/**
 * 后台系统设置 Tab 壳的回归测试。
 *
 * 这里钉住的是信息架构重构不能改变的系统语义：
 *   1. 面板懒挂载、按 Tab 按需读取，切回不重复请求；
 *   2. URL ?tab= 可定位，切换后写回；
 *   3. Agent 未保存过时显示引擎默认值，保存时提交完整配置；
 *   4. 每个 Tab 的失败互不阻塞，失败后可重试；
 *   5. 邮件密钥留空时不进入请求，避免把“保持原值”误发成“清空”。
 */

const apiMocks = vi.hoisted(() => ({
  saveSettings: vi.fn(),
  getSettings: vi.fn(),
  getAgentSettings: vi.fn(),
  saveAgentSettings: vi.fn(),
  getMailConfig: vi.fn(),
  saveMailConfig: vi.fn(),
  testMailConfig: vi.fn(),
  exportTenantData: vi.fn(),
}))

vi.mock('@/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/api/admin')>('@/api/admin')
  return {
    ...actual,
    saveSettings: apiMocks.saveSettings,
    getSettings: apiMocks.getSettings,
    getAgentSettings: apiMocks.getAgentSettings,
    saveAgentSettings: apiMocks.saveAgentSettings,
    getMailConfig: apiMocks.getMailConfig,
    saveMailConfig: apiMocks.saveMailConfig,
    testMailConfig: apiMocks.testMailConfig,
    exportTenantData: apiMocks.exportTenantData,
  }
})

import SettingsView from '../SettingsView.vue'
import { AGENT_SETTINGS_DEFAULTS } from '@/api/admin'

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

const mailConfig = {
  provider: 'smtp' as const,
  host: 'smtp.example.com',
  port: 587,
  username: 'mailer',
  security: 'starttls' as const,
  from: 'noreply@example.com',
  from_name: 'Chiron',
  reply_to: '',
  base_url: '',
  timeout_seconds: 30,
  has_password: true,
  has_api_key: false,
  ready: true,
  problem: '',
}

async function mountView(initial = '/admin/settings') {
  const Root = defineComponent({ render: () => h(RouterView) })
  const Destination = defineComponent({ render: () => h('div', { class: 'destination-page' }, '用户管理') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin/settings', component: SettingsView },
      { path: '/admin/users', component: Destination },
    ],
  })
  await router.push(initial)
  await router.isReady()
  const wrapper = mount(Root, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

function mockLeaveConfirm() {
  return vi.spyOn(Modal, 'confirm').mockImplementation(() => ({
    destroy: vi.fn(),
    update: vi.fn(),
  }) as ReturnType<typeof Modal.confirm>)
}

const clickTab = async (wrapper: Awaited<ReturnType<typeof mountView>>['wrapper'], label: string) => {
  const tab = wrapper.findAll('.ant-tabs-tab').find((item) => item.text().includes(label))
  expect(tab, `tab ${label} should exist`).toBeDefined()
  await tab!.trigger('click')
  await flushPromises()
}

const numberInput = (wrapper: Awaited<ReturnType<typeof mountView>>['wrapper'], test: string) =>
  wrapper.find(`[data-test="${test}"]`)

const numberValue = (wrapper: Awaited<ReturnType<typeof mountView>>['wrapper'], test: string) =>
  Number((numberInput(wrapper, test).element as HTMLInputElement).value)

describe('SettingsView Tab 壳', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getSettings.mockResolvedValue({})
    apiMocks.saveSettings.mockResolvedValue(undefined)
    apiMocks.getAgentSettings.mockResolvedValue({ ...AGENT_SETTINGS_DEFAULTS })
    apiMocks.saveAgentSettings.mockResolvedValue(undefined)
    apiMocks.getMailConfig.mockResolvedValue({ ...mailConfig })
    apiMocks.saveMailConfig.mockResolvedValue({ ...mailConfig })
    apiMocks.testMailConfig.mockResolvedValue({ sent: true, to: 'admin@example.com' })
    apiMocks.exportTenantData.mockResolvedValue(new Blob(['zip']))
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('默认只挂载通用面板并只请求限流分类', async () => {
    const { wrapper } = await mountView()

    expect(wrapper.findAll('.ant-tabs-tab')).toHaveLength(5)
    expect(wrapper.find('[data-test="rate-limit-config"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="agent-config"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="mail-config"]').exists()).toBe(false)
    expect(apiMocks.getSettings).toHaveBeenCalledTimes(1)
    expect(apiMocks.getSettings).toHaveBeenCalledWith('rate_limit')
    expect(apiMocks.getAgentSettings).not.toHaveBeenCalled()
    expect(apiMocks.getMailConfig).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('?tab=runtime 直接定位运行面板，初装显示引擎默认值', async () => {
    const { wrapper, router } = await mountView('/admin/settings?tab=runtime')

    expect(router.currentRoute.value.query.tab).toBe('runtime')
    expect(numberValue(wrapper, 'agent-no-progress-threshold')).toBe(3)
    expect(numberValue(wrapper, 'agent-token-window-ratio')).toBe(8)
    expect(numberValue(wrapper, 'agent-interaction-wait')).toBe(240)
    expect(apiMocks.getAgentSettings).toHaveBeenCalledTimes(1)
    expect(apiMocks.getSettings).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('后端已保存的 Agent 值覆盖默认值', async () => {
    apiMocks.getAgentSettings.mockResolvedValue({
      ...AGENT_SETTINGS_DEFAULTS,
      no_progress_threshold: 6,
      token_window_ratio: 2.5,
    })

    const { wrapper } = await mountView('/admin/settings?tab=runtime')

    expect(numberValue(wrapper, 'agent-no-progress-threshold')).toBe(6)
    expect(numberValue(wrapper, 'agent-token-window-ratio')).toBe(2.5)
    wrapper.unmount()
  })

  it('Agent 保存提交完整配置，成功后清除未保存状态', async () => {
    const { wrapper } = await mountView('/admin/settings?tab=runtime')

    await numberInput(wrapper, 'agent-no-progress-threshold').setValue('5')
    expect(wrapper.find('[data-test="settings-unsaved"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="agent-save"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('[data-test="agent-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveAgentSettings).toHaveBeenCalledTimes(1)
    expect(apiMocks.saveAgentSettings).toHaveBeenCalledWith({
      ...AGENT_SETTINGS_DEFAULTS,
      no_progress_threshold: 5,
    })
    expect(wrapper.find('[data-test="settings-unsaved"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="agent-config-dirty"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('切换写回 ?tab=，已访问面板保留且不会重复请求', async () => {
    const { wrapper, router } = await mountView()

    await clickTab(wrapper, 'Agent 运行配置')
    expect(router.currentRoute.value.query.tab).toBe('runtime')
    expect(apiMocks.getAgentSettings).toHaveBeenCalledTimes(1)

    await clickTab(wrapper, '通用设置')
    await clickTab(wrapper, 'Agent 运行配置')

    expect(apiMocks.getSettings).toHaveBeenCalledTimes(1)
    expect(apiMocks.getAgentSettings).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('未保存修改跨 Tab 保留，丢弃后恢复为已加载值', async () => {
    const { wrapper } = await mountView()
    const globalInput = wrapper.find('[data-test="rate-limit-config"] input')

    await globalInput.setValue('1200')
    expect(wrapper.find('[data-test="tab-dirty-general"]').exists()).toBe(true)
    await clickTab(wrapper, '邮件通道')
    await clickTab(wrapper, '通用设置')

    expect((wrapper.find('[data-test="rate-limit-config"] input').element as HTMLInputElement).value).toBe('1200')
    expect(wrapper.find('[data-test="tab-dirty-general"]').exists()).toBe(true)

    await wrapper.find('[data-test="rate-limit-config-discard"]').trigger('click')
    await flushPromises()

    expect((wrapper.find('[data-test="rate-limit-config"] input').element as HTMLInputElement).value).toBe('1000')
    expect(wrapper.find('[data-test="tab-dirty-general"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('一个 Tab 加载失败不阻塞其它 Tab，重试只重读失败面板', async () => {
    apiMocks.getSettings.mockRejectedValueOnce(new Error('rate limit unavailable'))

    const { wrapper } = await mountView()
    expect(wrapper.find('[data-test="general-load-error"]').text()).toContain('rate limit unavailable')

    await clickTab(wrapper, '邮件通道')
    expect(wrapper.find('[data-test="mail-config"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="mail-load-error"]').exists()).toBe(false)
    expect(apiMocks.getMailConfig).toHaveBeenCalledTimes(1)

    await clickTab(wrapper, '通用设置')
    expect(wrapper.find('[data-test="general-load-error"]').exists()).toBe(true)
    expect(apiMocks.getSettings).toHaveBeenCalledTimes(1)

    await wrapper.find('[data-test="general-load-error"] button').trigger('click')
    await flushPromises()

    expect(apiMocks.getSettings).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="general-load-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('模型 Tab 按原分类读取，模型限流仍保存到 python 分类', async () => {
    apiMocks.getSettings.mockImplementation(async (category: string) =>
      category === 'python' ? { gateway_model_rpm: 60 } : {},
    )

    const { wrapper } = await mountView('/admin/settings?tab=models')

    expect(apiMocks.getSettings.mock.calls.map(([category]) => category).sort()).toEqual([
      'api_key',
      'python',
      'quota',
    ])

    await wrapper.find('[data-test="model-rate-config"] input').setValue('120')
    await wrapper.find('[data-test="model-rate-config-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveSettings).toHaveBeenCalledWith(
      'python',
      expect.objectContaining({ gateway_model_rpm: 120 }),
    )
    wrapper.unmount()
  })

  it('邮件密钥留空不提交，填写后提交并在保存后清空输入', async () => {
    const { wrapper } = await mountView('/admin/settings?tab=mail')

    await wrapper.find('[data-test="mail-from"]').setValue('sender@example.com')
    await wrapper.find('[data-test="mail-config-save"]').trigger('click')
    await flushPromises()

    const blankPatch = apiMocks.saveMailConfig.mock.calls[0]![0]
    expect(blankPatch).not.toHaveProperty('password')
    expect(blankPatch).not.toHaveProperty('api_key')
    expect(blankPatch.from).toBe('sender@example.com')

    await wrapper.find('[data-test="mail-password"]').setValue('new-secret')
    await wrapper.find('[data-test="mail-config-save"]').trigger('click')
    await flushPromises()

    const secretPatch = apiMocks.saveMailConfig.mock.calls[1]![0]
    expect(secretPatch.password).toBe('new-secret')
    expect((wrapper.find('[data-test="mail-password"]').element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('没有未保存改动时可直接通过真实 Router 离开', async () => {
    const confirm = mockLeaveConfirm()
    const { wrapper, router } = await mountView()

    await router.push('/admin/users')
    await flushPromises()

    expect(confirm).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/admin/users')
    expect(wrapper.find('.destination-page').exists()).toBe(true)
    wrapper.unmount()
  })

  it('有未保存改动时阻止 Router 离开，取消后保留路由和草稿', async () => {
    const confirm = mockLeaveConfirm()
    const { wrapper, router } = await mountView()
    await wrapper.find('[data-test="rate-limit-config"] input').setValue('1200')

    const navigation = router.push('/admin/users')
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe('/admin/settings')
    expect(wrapper.find('.destination-page').exists()).toBe(false)

    confirm.mock.calls[0]![0].onCancel?.()
    await navigation
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/admin/settings')
    expect((wrapper.find('[data-test="rate-limit-config"] input').element as HTMLInputElement).value).toBe('1200')
    wrapper.unmount()
  })

  it('确认放弃后 Router 完成离页', async () => {
    const confirm = mockLeaveConfirm()
    const { wrapper, router } = await mountView()
    await wrapper.find('[data-test="rate-limit-config"] input').setValue('1200')

    const navigation = router.push('/admin/users')
    await flushPromises()
    confirm.mock.calls[0]![0].onOk?.()
    await navigation
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/admin/users')
    expect(wrapper.find('.destination-page').exists()).toBe(true)
    wrapper.unmount()
  })

  it('beforeunload 仅在 dirty 时阻止，保存和丢弃后解除', async () => {
    const { wrapper } = await mountView()
    const input = wrapper.find('[data-test="rate-limit-config"] input')
    const beforeUnload = () => {
      const event = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(event)
      return event.defaultPrevented
    }

    expect(beforeUnload()).toBe(false)
    await input.setValue('1200')
    expect(beforeUnload()).toBe(true)

    await wrapper.find('[data-test="rate-limit-config-save"]').trigger('click')
    await flushPromises()
    expect(beforeUnload()).toBe(false)

    await input.setValue('1300')
    expect(beforeUnload()).toBe(true)
    await wrapper.find('[data-test="rate-limit-config-discard"]').trigger('click')
    await flushPromises()
    expect(beforeUnload()).toBe(false)
    wrapper.unmount()
  })

  it('dirty 时同页 Tab query 替换不触发离页确认', async () => {
    const confirm = mockLeaveConfirm()
    const { wrapper, router } = await mountView()
    await wrapper.find('[data-test="rate-limit-config"] input').setValue('1200')

    await clickTab(wrapper, '邮件通道')

    expect(router.currentRoute.value.path).toBe('/admin/settings')
    expect(router.currentRoute.value.query.tab).toBe('mail')
    expect(confirm).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="settings-unsaved"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
