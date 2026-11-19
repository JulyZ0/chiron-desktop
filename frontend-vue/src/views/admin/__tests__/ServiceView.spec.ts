import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `ServiceView`（后台「服务」页）。
 *
 * 这一页最容易撒的谎是**把"已保存"显示成"已生效"**：监听地址绑在套接字上，
 * 进程内换不掉，保存后必须重启网关才真正改变谁能连上来。所以这里钉住三件事：
 *
 *   1. 后端报 `restart_required` 时，页面必须明说要重启，而不是弹一句"已保存"就完了；
 *   2. 「仅本机 / 局域网」两个档位落到 `bind_host` 的正确取值（127.0.0.1 / 0.0.0.0）；
 *   3. 保存只提交**改动过的字段** —— 后端把"字段缺席"和"显式清空"区别对待。
 */

const apiMocks = vi.hoisted(() => ({
  getServiceInfo: vi.fn(),
  saveServiceInfo: vi.fn(),
}))

vi.mock('@/api/admin', () => ({
  getServiceInfo: apiMocks.getServiceInfo,
  saveServiceInfo: apiMocks.saveServiceInfo,
}))

import ServiceView from '../ServiceView.vue'

/** 与服务端 AdminServiceInfo 形状一致的最小响应。 */
function info(overrides: Record<string, unknown> = {}) {
  return {
    listen_addr: ':8080',
    port: '8080',
    bind_host: '0.0.0.0',
    public_base_url: '',
    cookie_secure: false,
    restart_required: false,
    saved_bind_host: '0.0.0.0',
    saved_public_base_url: '',
    local_addresses: ['192.168.1.10'],
    warning: '',
    ...overrides,
  }
}

function mountView() {
  return mount(ServiceView)
}

const bindHostInput = (w: ReturnType<typeof mountView>) =>
  w.find('[data-test="service-bind-host"]')

describe('ServiceView 的「保存 ≠ 生效」呈现', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getServiceInfo.mockResolvedValue(info())
  })

  it('后端报需要重启时明说要重启，而不是只显示已保存', async () => {
    // 保存的是回环地址，但进程仍绑在全部网卡上（重启前的事实）。
    apiMocks.saveServiceInfo.mockResolvedValue(
      info({ saved_bind_host: '127.0.0.1', restart_required: true }),
    )

    const wrapper = mountView()
    await flushPromises()

    await bindHostInput(wrapper).setValue('127.0.0.1')
    await wrapper.find('[data-test="service-save"]').trigger('click')
    await flushPromises()

    const restart = wrapper.find('[data-test="service-restart"]')
    expect(restart.exists()).toBe(true)
    expect(restart.text()).toContain('重启')
    wrapper.unmount()
  })

  it('监听地址已生效时不误报需要重启', async () => {
    apiMocks.saveServiceInfo.mockResolvedValue(info({ saved_bind_host: '127.0.0.1' }))

    const wrapper = mountView()
    await flushPromises()

    await bindHostInput(wrapper).setValue('127.0.0.1')
    await wrapper.find('[data-test="service-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="service-restart"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('选「仅本机」把监听地址填成 127.0.0.1，选「局域网」填成 0.0.0.0', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 按 data-test 定位，不依赖模板里的渲染顺序。
    await wrapper.find('[data-test="scope-loopback"] input').setValue(true)
    await flushPromises()
    expect((bindHostInput(wrapper).element as HTMLInputElement).value).toBe('127.0.0.1')

    await wrapper.find('[data-test="scope-lan"] input').setValue(true)
    await flushPromises()
    expect((bindHostInput(wrapper).element as HTMLInputElement).value).toBe('0.0.0.0')
    wrapper.unmount()
  })

  it('只改对外地址时，只提交 public_base_url', async () => {
    // 与监听地址对称：后端把"字段缺席"与"显式清空"区别对待，
    // 全量提交会把没动的那一项也当成一次写入。
    apiMocks.saveServiceInfo.mockResolvedValue(
      info({ saved_public_base_url: 'https://chat.example.com', restart_required: true }),
    )

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="service-public-url"]').setValue('https://chat.example.com')
    await wrapper.find('[data-test="service-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveServiceInfo).toHaveBeenCalledWith({
      public_base_url: 'https://chat.example.com',
    })
    wrapper.unmount()
  })

  it('只提交改动过的字段：没动对外地址就不带 public_base_url', async () => {
    apiMocks.saveServiceInfo.mockResolvedValue(info({ saved_bind_host: '127.0.0.1' }))

    const wrapper = mountView()
    await flushPromises()

    await bindHostInput(wrapper).setValue('127.0.0.1')
    await wrapper.find('[data-test="service-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveServiceInfo).toHaveBeenCalledWith({ bind_host: '127.0.0.1' })
    wrapper.unmount()
  })

  it('无改动时保存按钮禁用，避免一次"什么都没变"的写入', async () => {
    const wrapper = mountView()
    await flushPromises()

    const save = wrapper.find('[data-test="service-save"]')
    expect(save.attributes('disabled')).toBeDefined()

    await bindHostInput(wrapper).setValue('127.0.0.1')
    await flushPromises()
    expect(save.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('列出本机可用地址并带上端口', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('192.168.1.10:8080')
    wrapper.unmount()
  })
})
