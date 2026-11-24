import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { Modal } from 'ant-design-vue'
import type { ApiKey } from '@/api/admin'

const apiMocks = vi.hoisted(() => ({
  listApiKeys: vi.fn(),
  addApiKey: vi.fn(),
  updateApiKey: vi.fn(),
  deleteApiKey: vi.fn(),
  listLlmProviders: vi.fn(),
  saveLlmProviderBaseURL: vi.fn(),
  updateApiKeyWeight: vi.fn(),
}))

vi.mock('@/api/admin', () => apiMocks)

import ProvidersView from '../ProvidersView.vue'

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

const provider = {
  id: 'openai',
  label: 'OpenAI',
  category: 'international',
  kind: 'openai',
  vendor: 'OpenAI',
  base_url: 'https://api.openai.com/v1',
  effective_base_url: 'https://api.openai.com/v1',
  api_key_prefix: 'sk-',
  requires_key: true,
  configured: true,
  key_count: 1,
  docs_url: '',
}

// 与 Go ListLLMKeys 经过真实响应脱敏中间件后的契约一致：id 是 12 位哈希前缀，
// key_preview 是五个掩码字符 + 八位哈希，响应不含明文 key/key_hash。
const key: ApiKey = {
  id: '0123456789ab',
  provider: 'openai',
  key_preview: '•••••01234567',
  status: 'active',
  weight: 10,
  remark: '',
}

describe('ProvidersView 服务商管理', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listApiKeys.mockResolvedValue([key])
    apiMocks.listLlmProviders.mockResolvedValue([provider])
  })

  it('使用统一页头、middle 表格和服务商文案', async () => {
    const wrapper = mount(ProvidersView)
    await flushPromises()

    expect(wrapper.find('.admin-page-title').text()).toBe('服务商管理')
    expect(wrapper.find('.admin-table .ant-table').classes()).toContain('ant-table-middle')
    expect(wrapper.text()).toContain('OpenAI')
    expect(wrapper.text()).toContain('•••••01234567')
    expect(wrapper.text()).toContain('0123456789ab')
    expect(wrapper.text()).not.toContain('API Key 管理')
    expect(apiMocks.listApiKeys).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('主按钮保持默认大小，点击后打开服务商目录', async () => {
    const wrapper = mount(ProvidersView)
    await flushPromises()

    const add = wrapper.find('.admin-page-actions button')
    expect(add.classes()).not.toContain('ant-btn-lg')
    expect(add.classes()).not.toContain('ant-btn-sm')
    await add.trigger('click')
    await flushPromises()

    expect(wrapper.findComponent(Modal).props('visible')).toBe(true)
    expect(document.body.textContent).toContain('服务商目录')
    expect(document.body.textContent).toContain('OpenAI')
    wrapper.unmount()
  })

  it('密钥加载失败显示持久错误态，重试成功后恢复表格', async () => {
    apiMocks.listApiKeys.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([key])
    const wrapper = mount(ProvidersView)
    await flushPromises()

    const error = wrapper.find('.admin-error-state')
    expect(error.text()).toContain('获取 API Key 失败')
    expect(wrapper.find('.admin-table').exists()).toBe(false)

    await error.find('button').trigger('click')
    await flushPromises()

    expect(apiMocks.listApiKeys).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.admin-error-state').exists()).toBe(false)
    expect(wrapper.find('.admin-table').text()).toContain('OpenAI')
    wrapper.unmount()
  })

  it('空密钥表显示操作说明且不渲染分页', async () => {
    apiMocks.listApiKeys.mockResolvedValue([])
    const wrapper = mount(ProvidersView)
    await flushPromises()

    expect(wrapper.find('.admin-table-empty-hint').text()).toContain('暂无服务商密钥')
    expect(wrapper.find('.admin-table-empty-hint').text()).toContain('添加服务商密钥后')
    expect(wrapper.find('.ant-pagination').exists()).toBe(false)
    wrapper.unmount()
  })

  it('服务商目录失败后可重试，不迫使页面切换到自定义模式', async () => {
    apiMocks.listLlmProviders
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce([provider])
    const wrapper = mount(ProvidersView)
    await flushPromises()
    await wrapper.find('.admin-page-actions button').trigger('click')
    await flushPromises()

    const modalError = document.body.querySelector('.ant-modal-body .admin-error-state')
    expect(modalError?.textContent).toContain('服务提供商目录加载失败')
    ;(modalError?.querySelector('button') as HTMLButtonElement).click()
    await flushPromises()

    expect(apiMocks.listLlmProviders).toHaveBeenCalledTimes(3)
    expect(document.body.querySelector('.ant-modal-body .admin-error-state')).toBeNull()
    expect(document.body.textContent).toContain('OpenAI')
    wrapper.unmount()
  })
})
