import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick, reactive, ref } from 'vue'
import MailPanel from '../MailPanel.vue'
import type { MailConfigDraft, SettingsSectionState } from '../types'

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

const createModel = (): MailConfigDraft => ({
  provider: 'smtp',
  host: 'smtp.example.com',
  port: 587,
  username: 'mailer',
  password: '',
  security: 'starttls',
  from: 'noreply@example.com',
  from_name: 'Chiron',
  reply_to: '',
  base_url: '',
  api_key: '',
  timeout_seconds: 30,
})

const mountPanel = (overrides: Partial<{
  state: SettingsSectionState
  hasPassword: boolean
  hasApiKey: boolean
  mailReady: boolean
}> = {}) => {
  const model = ref(createModel())
  const wrapper = mount(MailPanel, {
    props: {
      modelValue: model.value,
      state: overrides.state ?? { ready: true, dirty: true, saving: false },
      hasPassword: overrides.hasPassword ?? true,
      hasApiKey: overrides.hasApiKey ?? false,
      mailReady: overrides.mailReady ?? true,
      problem: '',
      testing: false,
    },
  })
  return { wrapper, model }
}

describe('MailPanel', () => {
  it('已保存密钥只显示“留空保持”占位，不回显秘密', () => {
    const { wrapper } = mountPanel({ hasPassword: true })
    const password = wrapper.find('[data-test="mail-password"]')

    expect((password.element as HTMLInputElement).value).toBe('')
    expect(password.attributes('placeholder')).toContain('留空保持不变')
    expect(wrapper.text()).not.toContain('smtp-password')
    wrapper.unmount()
  })

  it('切换晴辰云邮后只显示 API Key 输入，不遗留 SMTP 密码框', async () => {
    const { wrapper, model } = mountPanel({ hasApiKey: true })

    model.value.provider = 'qingchen'
    await nextTick()

    expect(wrapper.find('[data-test="mail-password"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="mail-api-key"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="mail-api-key"]').attributes('placeholder')).toContain('留空保持不变')
    wrapper.unmount()
  })

  it('未加载完成时保存和测试都禁用，并就绪且脏状态才允许保存', async () => {
    const state = reactive<SettingsSectionState>({ ready: false, dirty: true, saving: false })
    const { wrapper } = mountPanel({ state })

    expect(wrapper.find('[data-test="mail-config-save"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="mail-test"]').attributes('disabled')).toBeDefined()

    state.ready = true
    await nextTick()
    expect(wrapper.find('[data-test="mail-config-save"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('[data-test="mail-test"]').attributes('disabled')).toBeUndefined()

    state.dirty = false
    await nextTick()
    expect(wrapper.find('[data-test="mail-config-save"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('保存、丢弃和测试动作分别冒泡给协调层', async () => {
    const { wrapper } = mountPanel()

    await wrapper.find('[data-test="mail-config-save"]').trigger('click')
    await wrapper.find('[data-test="mail-config-discard"]').trigger('click')
    await wrapper.find('[data-test="mail-test"]').trigger('click')

    expect(wrapper.emitted('save')).toHaveLength(1)
    expect(wrapper.emitted('discard')).toHaveLength(1)
    expect(wrapper.emitted('test')).toHaveLength(1)
    wrapper.unmount()
  })
})
