import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { Switch } from 'ant-design-vue'

/**
 * `MarketSourcesPanel` —— 后台资源市场「外部源」配置面板。
 *
 * 四条用例对应四个**会让人做出相反判断**的产品决策，任何一条退回去都是缺陷
 * （而且视觉上不一定看得出来）：
 *   1. 初装 = 关闭。空 config 若被读成"开"，首次安装就自动开始访问第三方 URL ——
 *      这是隐私/合规事故，不是显示问题。
 *   2. 20 是硬上限。满 20 仍能点「添加」= 上限形同虚设。
 *   3. 必须 https。http 会让源地址与内容暴露在链路上，行内就得拦下。
 *   4. 提交键名即后端契约。写错一个字符**不报错**，只是静默不生效。
 *
 * 选择器用 `data-test`：断言的是行为开关，不是文案排版。
 */

const apiMocks = vi.hoisted(() => ({
  getMarketSettings: vi.fn(),
  saveMarketSettings: vi.fn(),
}))

vi.mock('../../../api/admin', async () => {
  // 归一逻辑本身是被测行为的一部分（空 config → 全关），所以**不**在这里再造一份，
  // 只把 HTTP 两处换掉，其余（normalize/parse/常量）用真实实现。
  const actual = await vi.importActual<typeof import('../../../api/admin')>('../../../api/admin')
  return {
    ...actual,
    getMarketSettings: apiMocks.getMarketSettings,
    saveMarketSettings: apiMocks.saveMarketSettings,
  }
})

import MarketSourcesPanel from '../MarketSourcesPanel.vue'
import { emptyMarketSettings, normalizeMarketSettings } from '../../../api/admin'

const empty = () => normalizeMarketSettings({})

function mountPanel() {
  return mount(MarketSourcesPanel, {
    // 组件会 inject(routerKey) 判断要不要注册路由守卫；单测里没有 router。
    // antd 的 Switch 用真实实现（v-model + disabled 都能读回）。
    global: { stubs: { Modal: true } },
  })
}

const masterSwitch = (w: ReturnType<typeof mountPanel>) =>
  w.findAllComponents(Switch).at(0)!

describe('MarketSourcesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getMarketSettings.mockResolvedValue(empty())
    apiMocks.saveMarketSettings.mockResolvedValue(undefined)
  })

  it('初装（config 为空）：总开关关闭，源列表整块 disabled', async () => {
    const w = mountPanel()
    await flushPromises()

    // ① 总开关必须是关 —— 初装即开始访问第三方 URL 是事故
    expect(masterSwitch(w).props('checked')).toBe(false)
    // 说明文案要点出"开启后访问第三方 URL"这件事本身
    expect(w.text()).toContain('第三方 URL')

    // ② 关闭时列表区可见但不可编辑（藏起来会让人以为配置丢了）
    const section = w.find('[data-test="market-sources-section"]')
    expect(section.exists()).toBe(true)
    expect(section.attributes('data-disabled')).toBe('true')
    expect(w.find('[data-test="market-add-input"]').attributes('disabled')).toBeDefined()
    // 「添加」按钮同样不可用
    expect(w.find('[data-test="market-add-btn"]').attributes('disabled')).toBeDefined()

    // 两个子开关也默认关闭
    const switches = w.findAllComponents(Switch)
    expect(switches.length).toBe(3)
    expect(switches[1]!.props('checked')).toBe(false)
    expect(switches[2]!.props('checked')).toBe(false)
    expect(switches[1]!.props('disabled')).toBe(true)
    expect(switches[2]!.props('disabled')).toBe(true)

    w.unmount()
  })

  it('已配置 20 条时「添加」按钮禁用，并显示 N / 20 与上限提示', async () => {
    apiMocks.getMarketSettings.mockResolvedValue(
      normalizeMarketSettings({
        remote_enabled: true,
        catalog_sources: Array.from({ length: 20 }, (_, i) => `https://s${i}.example.com/v1/sources.json`),
      }),
    )

    const w = mountPanel()
    await flushPromises()

    expect(w.find('[data-test="market-count"]').text()).toContain('20 / 20')
    // 满额时输入框与按钮都禁用 —— 否则用户填完点一下才发现加不进去
    expect(w.find('[data-test="market-add-input"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-test="market-add-btn"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-test="market-limit"]').text()).toContain('20')

    w.unmount()
  })

  it('非 https 的地址被行内校验拦下，且不会进入列表', async () => {
    apiMocks.getMarketSettings.mockResolvedValue(
      normalizeMarketSettings({ remote_enabled: true, catalog_sources: [] }),
    )

    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="market-add-input"]').setValue('http://insecure.example.com/catalog.json')

    // 行内报错写明"必须 https"
    const err = w.find('[data-test="market-add-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('https')
    // 按钮禁用，点也不会加进去
    expect(w.find('[data-test="market-add-btn"]').attributes('disabled')).toBeDefined()
    await w.find('[data-test="market-add-btn"]').trigger('click')
    expect(w.find('[data-test="market-count"]').text()).toContain('0 / 20')

    // 换成 https 后错误消失、按钮恢复
    await w.find('[data-test="market-add-input"]').setValue('https://ok.example.com/catalog.json')
    expect(w.find('[data-test="market-add-error"]').exists()).toBe(false)
    expect(w.find('[data-test="market-add-btn"]').attributes('disabled')).toBeUndefined()

    w.unmount()
  })

  it('保存时提交的键名与后端契约一致（含 max_sources 恒为 20）', async () => {
    apiMocks.getMarketSettings.mockResolvedValue(
      normalizeMarketSettings({ remote_enabled: true, catalog_sources: [] }),
    )

    const w = mountPanel()
    await flushPromises()

    await w.find('[data-test="market-add-input"]').setValue('https://a.example.com/c.json')
    await w.find('[data-test="market-add-btn"]').trigger('click')

    // 三个子开关都打开，确保所有键都被提交。
    // antd 的 Switch 不是原生 checkbox，setValue() 不会驱动它 —— 必须走 v-model 的
    // update:checked 事件，才是真实点击时的同一路径。
    const switches = w.findAllComponents(Switch)
    switches[1]!.vm.$emit('update:checked', true)
    switches[2]!.vm.$emit('update:checked', true)
    await flushPromises()

    const save = w.find('[data-test="market-save"]')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flushPromises()

    expect(apiMocks.saveMarketSettings).toHaveBeenCalledTimes(1)
    const payload = apiMocks.saveMarketSettings.mock.calls[0]![0]
    // 键集必须与契约逐字一致 —— 多一个少一个都是静默不生效
    expect(Object.keys(payload).sort()).toEqual(
      ['allow_remote_mcp', 'allow_user_sources', 'catalog_sources', 'max_sources', 'remote_enabled'],
    )
    expect(payload.remote_enabled).toBe(true)
    expect(payload.catalog_sources).toEqual(['https://a.example.com/c.json'])
    expect(payload.allow_user_sources).toBe(true)
    expect(payload.allow_remote_mcp).toBe(true)
    // 上限不可通过 UI 调大，提交值恒为产品硬上限
    expect(payload.max_sources).toBe(20)

    w.unmount()
  })

  it('读取失败时保持"全关"并给出重试入口（未知态不能当成开）', async () => {
    apiMocks.getMarketSettings.mockRejectedValue(new Error('403 forbidden'))

    const w = mountPanel()
    await flushPromises()

    // 有重试入口，且状态仍是"关" —— 读不到不能当成开
    expect(w.find('[data-test="market-load-failed"]').exists()).toBe(true)
    expect(masterSwitch(w).props('checked')).toBe(false)
    // 后端原文要露出来，否则运维不知道是 403 还是网络断
    expect(w.text()).toContain('403 forbidden')

    w.unmount()
  })
})

describe('market settings 归一（api 层）', () => {
  it('空对象 → 四个开关全关、无源、上限 20', () => {
    expect(emptyMarketSettings()).toEqual({
      remote_enabled: false,
      catalog_sources: [],
      allow_user_sources: false,
      allow_remote_mcp: false,
      max_sources: 20,
    })
  })

  it('字符串里的 "false" / 缺失 / 无法识别 都归一成 false（安全开关的未知态必须是关）', () => {
    const c = normalizeMarketSettings({
      remote_enabled: 'false',
      allow_user_sources: '0',
      allow_remote_mcp: null,
    })
    expect(c.remote_enabled).toBe(false)
    expect(c.allow_user_sources).toBe(false)
    expect(c.allow_remote_mcp).toBe(false)
  })

  it('catalog_sources 去重并剔除空串；超过 20 条不截断（交给用户自己删）', () => {
    const many = Array.from({ length: 23 }, (_, i) => `https://s${i}.example.com/c.json`)
    const c = normalizeMarketSettings({ catalog_sources: [...many, '  ', many[0]] })
    expect(c.catalog_sources.length).toBe(23)
    expect(c.max_sources).toBe(20)
  })
})
