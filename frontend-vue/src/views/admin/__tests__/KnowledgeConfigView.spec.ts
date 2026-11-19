import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `KnowledgeConfigView`（后台「知识库」页）。
 *
 * 这一页最容易撒的两个谎，正是这里要钉住的：
 *
 *   1. **把「已保存」当成「引擎已采纳」**。配置写进 DB 只是第一步，引擎靠 Redis 广播
 *      或下次启动的 hydrate() 才会真正换地址。后端报 `pending_restart` 时页面必须说
 *      "引擎尚未采纳"，而不是弹一句"已保存"。
 *   2. **让本地模式也能填地址**。本地模式永远还原到进程启动时注入的地址
 *      （runtime.bootstrap_url()），页面允许填一个会被静默忽略的值，用户就再也
 *      说不清自己到底在用谁。
 */

const apiMocks = vi.hoisted(() => ({
  getKnowledgeInfo: vi.fn(),
  saveKnowledgeInfo: vi.fn(),
  testKnowledgeService: vi.fn(),
}))

vi.mock('@/api/admin', () => ({
  getKnowledgeInfo: apiMocks.getKnowledgeInfo,
  saveKnowledgeInfo: apiMocks.saveKnowledgeInfo,
  testKnowledgeService: apiMocks.testKnowledgeService,
}))

import KnowledgeConfigView from '../KnowledgeConfigView.vue'

/** 与服务端 AdminKnowledgeInfo 形状一致的最小响应。 */
function info(overrides: Record<string, unknown> = {}) {
  return {
    saved: { mode: 'local', service_url: '' },
    runtime: {
      mode: 'local',
      service_url: 'http://127.0.0.1:8100',
      bootstrap_url: 'http://127.0.0.1:8100',
      using_bootstrap: true,
    },
    engine_available: true,
    pending_restart: false,
    ...overrides,
  }
}

const mountView = () => mount(KnowledgeConfigView)
const urlInput = (w: ReturnType<typeof mountView>) =>
  w.find('[data-test="knowledge-service-url"]')

describe('KnowledgeConfigView 的「保存 ≠ 生效」呈现', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getKnowledgeInfo.mockResolvedValue(info())
  })

  it('后端报分叉时提示引擎尚未采纳，而不是只说已保存', async () => {
    // 保存的是外部地址，但引擎还在打启动注入的地址（广播没到 / 引擎没起）。
    apiMocks.saveKnowledgeInfo.mockResolvedValue(
      info({
        saved: { mode: 'remote', service_url: 'http://pi:9100' },
        runtime: { mode: 'local', service_url: 'http://127.0.0.1:8100' },
        pending_restart: true,
      }),
    )

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    await urlInput(wrapper).setValue('http://pi:9100')
    await wrapper.find('[data-test="knowledge-save"]').trigger('click')
    await flushPromises()

    const alert = wrapper.find('[data-test="knowledge-diverged"]')
    expect(alert.exists()).toBe(true)
    // 文案要点：这是"未收到变更通知 / 未运行"，不是"配置写错了"。
    expect(alert.text()).toContain('不一致')
    expect(alert.text()).toContain('尚未收到变更通知')
    wrapper.unmount()
  })

  it('引擎采纳后不误报分叉', async () => {
    apiMocks.saveKnowledgeInfo.mockResolvedValue(
      info({
        saved: { mode: 'remote', service_url: 'http://pi:9100' },
        runtime: { mode: 'remote', service_url: 'http://pi:9100' },
      }),
    )

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    await urlInput(wrapper).setValue('http://pi:9100')
    await wrapper.find('[data-test="knowledge-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="knowledge-diverged"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('引擎不可达时明说读不到，而不是渲染成配置错误', async () => {
    apiMocks.getKnowledgeInfo.mockResolvedValue(
      info({ runtime: null, engine_available: false }),
    )

    const wrapper = mountView()
    await flushPromises()

    const hint = wrapper.find('[data-test="knowledge-engine-unavailable"]')
    expect(hint.exists()).toBe(true)
    // 关键区别：这不代表配置有误。
    expect(hint.text()).toContain('不代表配置有误')
    wrapper.unmount()
  })

  it('本地模式下地址输入框置灰：那个值本来就会被引擎忽略', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(urlInput(wrapper).attributes('disabled')).toBeDefined()

    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    expect(urlInput(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('只提交改动过的字段：切到远程且填了地址才带 service_url', async () => {
    apiMocks.saveKnowledgeInfo.mockResolvedValue(
      info({ saved: { mode: 'remote', service_url: 'http://pi:9100' } }),
    )

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    await urlInput(wrapper).setValue('http://pi:9100')
    await wrapper.find('[data-test="knowledge-save"]').trigger('click')
    await flushPromises()

    expect(apiMocks.saveKnowledgeInfo).toHaveBeenCalledWith({
      mode: 'remote',
      service_url: 'http://pi:9100',
    })
    wrapper.unmount()
  })

  it('本地模式下即使地址框里有残留内容，也不提交 service_url', async () => {
    // 后端把"字段缺席"与"显式清空"区别对待：本地模式下多送一个地址，
    // 就会存下一个永远被忽略的值，之后对比反而看起来"没生效"。
    apiMocks.saveKnowledgeInfo.mockResolvedValue(info({ saved: { mode: 'local' } }))

    const wrapper = mountView()
    await flushPromises()

    // 先切远程填地址，再切回本地 —— 模拟用户来回试。
    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    await urlInput(wrapper).setValue('http://pi:9100')
    await wrapper.find('[data-test="mode-local"] input').setValue(true)
    await flushPromises()

    // 保存按钮此时应因"无实际改动"而禁用。
    expect(wrapper.find('[data-test="knowledge-save"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('无改动时保存按钮禁用', async () => {
    const wrapper = mountView()
    await flushPromises()

    const save = wrapper.find('[data-test="knowledge-save"]')
    expect(save.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    expect(save.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('测试连接把草稿里的地址交给后端，空地址表示探当前生效的', async () => {
    apiMocks.testKnowledgeService.mockResolvedValue({
      ok: true,
      engine_available: true,
      detail: '',
    })

    const wrapper = mountView()
    await flushPromises()

    // 本地模式：测当前生效的（空串）。
    await wrapper.find('[data-test="knowledge-test"]').trigger('click')
    await flushPromises()
    expect(apiMocks.testKnowledgeService).toHaveBeenLastCalledWith('')

    // 远程模式：测草稿里的地址（保存前就能验）。
    await wrapper.find('[data-test="mode-remote"] input').setValue(true)
    await flushPromises()
    await urlInput(wrapper).setValue('http://pi:9100')
    await wrapper.find('[data-test="knowledge-test"]').trigger('click')
    await flushPromises()
    expect(apiMocks.testKnowledgeService).toHaveBeenLastCalledWith('http://pi:9100')
    wrapper.unmount()
  })

  it('引擎不可达时测试结果明说无法探测，而不是"连接失败"', async () => {
    apiMocks.testKnowledgeService.mockResolvedValue({
      ok: false,
      engine_available: false,
      detail: '引擎不可达，无法探测知识库服务',
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="knowledge-test"]').trigger('click')
    await flushPromises()

    const result = wrapper.find('[data-test="knowledge-test-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('引擎不可达')
    wrapper.unmount()
  })
})
