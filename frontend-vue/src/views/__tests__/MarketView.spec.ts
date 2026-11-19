import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `MarketView` 的行为网 —— 五条断言对应五个**产品口径**，不是"能渲染就行"的冒烟。
 *
 * 这些口径是用户明确拍下来的，任何一条回归都会让市场页变成"看起来能用但不能用"：
 *   1) **内置源恒排第一且默认选中** —— 后端返回顺序可能被运维配置改动，前端必须自己兜底；
 *      内置源随二进制发布、不依赖网络，是外部源全挂时页面仍有内容的底线。
 *   2) **未签名只提醒、不阻止安装** —— 但内置条目 `signed === false` 是正常状态，不能挂徽标；
 *      首屏每个条目都带警告会造成警报疲劳，真未签名条目反而被忽略。
 *   3) **外部源装完默认禁用** —— 装成功 ≠ 可用。不告诉用户"去对应页面启用"，
 *      就会被当成装坏了（这条是 `enabled:false` 与 `message.warning` 的接线）。
 *   4) **非致命告警要渲染成告警条** —— 单个源挂掉不能让整页变红，但也不能静默；
 *      而且 `reason` 是稳定机器码，必须本地化，不能把英文码甩给用户。
 *   5) **切换源 Tab 要带 `source` 参数** —— `(source, id)` 才是条目主键，
 *      漏传 source 就会把多个源的条目混在一起，用户不知道自己装的是哪一个。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

// 纯函数（排序/键/错误翻译）用**真实实现**：它们本身就是被测口径的一部分，
// mock 掉等于把"内置源排第一"这条断言变成自证。
const marketMocks = vi.hoisted(() => ({
  listMarketCatalog: vi.fn(),
  getMarketItem: vi.fn(),
  listInstalledMarketItems: vi.fn(),
  installMarketItem: vi.fn(),
  uninstallMarketItem: vi.fn(),
}))

vi.mock('../../api/market', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/market')>()
  return { ...actual, ...marketMocks }
})

import { message, Modal } from 'ant-design-vue'
import { i18n } from '../../i18n'
import MarketView from '../MarketView.vue'
import type { MarketCatalog, MarketItem, MarketSource } from '../../api/market'

const tr = (key: string, named?: Record<string, unknown>): string =>
  (i18n.global as unknown as { t: (k: string, n?: Record<string, unknown>) => string }).t(key, named)

// ── 夹具 ──

const BUILTIN: MarketSource = {
  id: 'builtin', name: '内置源', type: 'builtin', enabled: true, count: 1,
}

const EXT: MarketSource = {
  id: 'ext1', name: '团队源', url: 'https://market.example.com/catalog.json',
  type: 'operator', enabled: true, count: 1,
}

const builtinItem: MarketItem = {
  kind: 'skill', id: 'builtin.summarize', name: '摘要技能', description: '内置的摘要技能',
  source: 'builtin', signed: false, // 内置条目未签名 = 正常状态，不该出徽标
}

const externalItem: MarketItem = {
  kind: 'skill', id: 'ext.review', name: '评审技能', description: '外部源提供的评审技能',
  source: 'ext1', signed: false, manifest_url: 'https://market.example.com/ext/review.json',
}

function catalogOf(over: Partial<MarketCatalog> = {}): MarketCatalog {
  return {
    items: [builtinItem],
    total: 1,
    page: 1,
    page_size: 12,
    // 故意把 ext1 放前面：前端必须自己把内置源提到第一
    sources: [EXT, BUILTIN],
    warnings: [],
    ...over,
  }
}

function mountView() {
  return mount(MarketView)
}

const tabLabels = (wrapper: ReturnType<typeof mountView>): string[] =>
  wrapper.findAll('.ant-tabs-tab').map(e => (e.text() || '').trim())

describe('MarketView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    marketMocks.listMarketCatalog.mockResolvedValue(catalogOf())
    marketMocks.listInstalledMarketItems.mockResolvedValue([])
    marketMocks.getMarketItem.mockResolvedValue({ item: externalItem, warnings: [] })
    marketMocks.uninstallMarketItem.mockResolvedValue(undefined)
  })

  it('★ 内置源恒排第一且默认选中（后端返回顺序反了也要纠正）', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(tabLabels(wrapper)[0]).toBe(tr('market.source.builtin'))
    // 首次请求就带 builtin：默认选中不能只是 UI 高亮，必须真的以它取数
    expect(marketMocks.listMarketCatalog).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'builtin' }),
    )
    wrapper.unmount()
  })

  it('★ 未签名徽标只挂外部条目，内置条目不挂（避免警报疲劳）', async () => {
    marketMocks.listMarketCatalog.mockResolvedValue(
      catalogOf({ items: [builtinItem, externalItem], total: 2 }),
    )

    const wrapper = mountView()
    await flushPromises()

    const cards = wrapper.findAll('[data-testid="market-card"]')
    expect(cards).toHaveLength(2)

    // 内置条目：没有未签名徽标
    expect(cards[0].find('[data-testid="market-unsigned-badge"]').exists()).toBe(false)
    // 外部条目：徽标 + 一句提醒都在，但**安装按钮照样可用**
    expect(cards[1].find('[data-testid="market-unsigned-badge"]').exists()).toBe(true)
    expect(cards[1].text()).toContain(tr('market.unsigned_hint'))
    expect(cards[1].find('[data-testid="market-install"]').attributes('disabled')).toBeUndefined()

    wrapper.unmount()
  })

  it('★ 外部源装成功但 enabled=false ⇒ 提示「已安装但禁用」', async () => {
    marketMocks.listMarketCatalog.mockResolvedValue(
      catalogOf({ items: [externalItem], total: 1 }),
    )
    marketMocks.installMarketItem.mockResolvedValue({
      installed: true, source: 'ext1', kind: 'skill', id: 'ext.review',
      enabled: false, requires_approval: false,
    })

    // Modal.confirm 在 jsdom 里渲染到 body，直接点 OK 很脆；这里让确认框"即刻确认"，
    // 断言的重点是**装完之后的提示**，不是确认框本身。
    const confirmSpy = vi
      .spyOn(Modal, 'confirm')
      .mockImplementation(((cfg: { onOk?: () => unknown }) => {
        void cfg.onOk?.()
        return {} as never
      }) as never)
    const warnSpy = vi.spyOn(message, 'warning').mockImplementation((() => undefined) as never)
    const successSpy = vi.spyOn(message, 'success').mockImplementation((() => undefined) as never)

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="market-install"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalled()
    expect(marketMocks.installMarketItem).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'ext1', kind: 'skill', id: 'ext.review' }),
    )
    expect(successSpy).toHaveBeenCalled()
    expect(warnSpy).toHaveBeenCalledWith(tr('market.installed_disabled_hint'))

    wrapper.unmount()
    confirmSpy.mockRestore()
    warnSpy.mockRestore()
    successSpy.mockRestore()
  })

  it('★ warnings 渲染成告警条，且 reason 被本地化（不显示英文原因码）', async () => {
    marketMocks.listMarketCatalog.mockResolvedValue(
      catalogOf({
        warnings: [
          { source: 'ext1', reason: 'source_unreachable', detail: 'dial tcp: timeout' },
          { source: 'ext1', reason: 'unsigned_items', detail: '3 items without signature' },
        ],
      }),
    )

    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain(tr('market.warn.source_unreachable', { source: EXT.name }))
    // `{n}` 从 detail 里取，取不到才退回通用文案 —— 这里能取到 3
    expect(text).toContain(tr('market.warn.unsigned_items', { source: EXT.name, n: '3' }))
    // 原始机器码与内部异常串都不能出现在界面上
    expect(text).not.toContain('source_unreachable')
    expect(text).not.toContain('dial tcp')

    wrapper.unmount()
  })

  it('★ 合集条目在详情里列出会导入的文档（装之前就能看清）', async () => {
    const collection: MarketItem = {
      kind: 'knowledge',
      id: 'kb.collection',
      name: '文档合集',
      description: '一批文档',
      source: 'builtin',
      signed: false,
      payload: {
        documents: [
          { name: 'a.pdf', url: 'https://example.com/a.pdf' },
          { name: 'b.docx', url: 'https://example.com/b.docx' },
        ],
      },
    }
    marketMocks.listMarketCatalog.mockResolvedValue(
      catalogOf({ items: [collection], total: 1 }),
    )
    marketMocks.getMarketItem.mockResolvedValue({ item: collection, warnings: [] })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="market-card"]').trigger('click')
    await flushPromises()

    // Drawer 渲染到 body 上，不能只看组件树
    const drawer = document.body.textContent ?? ''
    expect(drawer).toContain(tr('market.detail.collection_documents', { n: 2 }))
    expect(drawer).toContain('a.pdf')
    expect(drawer).toContain('https://example.com/b.docx')
    // 必须同时说明"不会自动建索引"：否则用户会以为装完就能检索
    expect(drawer).toContain(tr('market.detail.collection_hint'))

    wrapper.unmount()
  })

  it('★ 外部条目不展示合集清单（payload 只是目录自称，装什么以 manifest 为准）', async () => {
    const externalKb: MarketItem = {
      ...externalItem,
      kind: 'knowledge',
      id: 'ext.kb',
      payload: { documents: [{ name: 'claimed.pdf', url: 'https://example.com/claimed.pdf' }] },
    }
    marketMocks.listMarketCatalog.mockResolvedValue(
      catalogOf({ items: [externalKb], total: 1 }),
    )
    marketMocks.getMarketItem.mockResolvedValue({ item: externalKb, warnings: [] })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="market-card"]').trigger('click')
    await flushPromises()

    // 展示目录自称的清单会造成"看到 3 份、实际装了 2 份"的不可自证不一致
    const drawer = document.body.textContent ?? ''
    expect(drawer).not.toContain('claimed.pdf')
    expect(drawer).not.toContain(tr('market.detail.collection_documents', { n: 1 }))

    wrapper.unmount()
  })

  it('★ 切换源 Tab 会用新的 source 重新取数', async () => {
    const wrapper = mountView()
    await flushPromises()

    const tabs = wrapper.findAll('.ant-tabs-tab')
    expect(tabs).toHaveLength(2)

    await tabs[1].trigger('click')
    await flushPromises()

    expect(marketMocks.listMarketCatalog).toHaveBeenLastCalledWith(
      expect.objectContaining({ source: EXT.id }),
    )

    wrapper.unmount()
  })
})
