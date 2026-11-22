import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

// jsdom 未实现 matchMedia，ant-design-vue 的 Tabs/Grid 等组件（useBreakpoint）依赖它
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
  })) as any
}

vi.mock('../../api/memory', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/memory')>()
  return {
    ...actual,
    listMemory: vi.fn(),
    upsertMemory: vi.fn(),
    updateMemory: vi.fn(),
    deleteMemory: vi.fn(),
    clearMemory: vi.fn(),
    searchMemory: vi.fn(),
    moveMemoryScope: vi.fn(),
    startOrganize: vi.fn(),
    getOrganizeStatus: vi.fn(),
    listConflicts: vi.fn(),
    listSummaries: vi.fn(),
  }
})

// 项目选择器从 session-map 拉真实项目列表；测试里不需要它，返回空即可。
vi.mock('../sessionmap/projectsApi', () => ({
  listProjects: vi.fn(async () => []),
}))

import MemoryView from '../MemoryView.vue'
import * as memoryApi from '../../api/memory'
import { setProject } from '../../composables/useProject'
import type { MemorySearchHit } from '../../api/memory'

function makeEntry(overrides: Partial<MemorySearchHit> = {}): MemorySearchHit {
  return {
    id: 'mem-1',
    slot: 'preference',
    slot_label: '偏好',
    key: 'editor',
    value: 'VSCode',
    confidence: 80,
    source: 'user_confirmed',
    source_label: '用户确认',
    has_embedding: true,
    access_count: 3,
    last_accessed_at: null,
    status: 'active',
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    similarity: 0.8,
    score: 0.7,
    ...overrides,
  }
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

/** 组件用 useRouter() 跳到对话，因此 mount 时必须提供 router 插件 */
function mountView() {
  return mount(MemoryView, { global: { plugins: [router] } })
}

describe('项目选择器（C3）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [],
      counts: { identity: 0, preference: 0, decision: 0, fact: 0 },
      total: 0,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({ conflicts: [], count: 0 })
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({ summaries: [], count: 0 })
    vi.mocked(memoryApi.getOrganizeStatus).mockResolvedValue({
      running: false,
      started_at: null,
      finished_at: null,
      result: null,
      error: null,
    })
  })

  it('切换项目会重新加载记忆并把项目持久化', async () => {
    const wrapper = mountView()
    await flushPromises()
    const before = vi.mocked(memoryApi.listMemory).mock.calls.length

    const select = wrapper.find('[data-test="project-input"]')
    // ant-design-vue 的 Select 把 class/data-test 落在选择器容器（DIV）上，
    // setValue 只对原生 input 有效 —— 直接触发组件事件才是它真实的交互面。
    await select.trigger('click')
    const vm = wrapper.findComponent({ name: 'ASelect' })
    vm.vm.$emit('change', 'proj-a')
    await flushPromises()

    expect(vi.mocked(memoryApi.listMemory).mock.calls.length).toBeGreaterThan(before)
    expect(localStorage.getItem('chiron.memory.project')).toBe('proj-a')
  })
})

describe('MemoryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [],
      counts: { identity: 0, preference: 0, decision: 0, fact: 0 },
      total: 0,
      slots: [
        { slot: 'identity', label: '身份' },
        { slot: 'preference', label: '偏好' },
        { slot: 'decision', label: '关键决策' },
        { slot: 'fact', label: '长期事实' },
      ],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({ conflicts: [], count: 0 })
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({ summaries: [], count: 0 })
  })

  /** 点开某个顶部 tab（ant-design-vue 的 Tabs 用 .ant-tabs-tab 承载标题） */
  async function openTab(wrapper: ReturnType<typeof mount>, label: string) {
    const tab = wrapper.findAll('.ant-tabs-tab').find((t) => t.text().includes(label))
    expect(tab, `tab "${label}" not found`).toBeTruthy()
    await tab!.trigger('click')
    await flushPromises()
  }

  it('渲染标题、检索提示与记忆操作骨架', async () => {
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('长期记忆')
    // L2 档案卡设计意图（跨会话留存 · 语义检索 · 自动整理）
    expect(text).toContain('语义检索')
    expect(text).toContain('自动整理')
    // 三个核心操作按钮
    for (const label of ['智能整理', '清空记忆', '新建记忆']) {
      expect(text).toContain(label)
    }
    // 语义检索输入框（用 placeholder 定位：顶部还有一个「项目」输入框，
    // `find('input')` 这种位置选择器不再可靠）
    const input = wrapper.find('input[placeholder*="语义检索"]')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).placeholder).toContain('语义检索')
  })

  it('挂载即加载记忆列表并展示条目', async () => {
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [makeEntry(), makeEntry({ id: 'mem-2', slot: 'fact', slot_label: '长期事实', key: 'stack', value: 'Go + Python' })],
      counts: { identity: 0, preference: 1, decision: 0, fact: 1 },
      total: 2,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()
    expect(memoryApi.listMemory).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('editor')
    expect(wrapper.text()).toContain('VSCode')
    expect(wrapper.text()).toContain('Go + Python')
  })

  it('点击「智能检索」调用 searchMemory 并展示结果', async () => {
    vi.mocked(memoryApi.searchMemory).mockResolvedValue({
      query: '编辑器',
      mode: 'semantic',
      count: 1,
      results: [makeEntry({ value: 'VSCode', similarity: 0.92, score: 0.88 })],
      summaries: [],
      summary_count: 0,
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="语义检索"]').setValue('编辑器')
    // 找到「智能检索」按钮并点击
    const buttons = wrapper.findAll('button')
    const searchBtn = buttons.find((b) => b.text().includes('智能检索'))
    expect(searchBtn).toBeTruthy()
    await searchBtn!.trigger('click')
    await flushPromises()

    expect(memoryApi.searchMemory).toHaveBeenCalledWith('编辑器', { top_k: 10 })
    expect(wrapper.text()).toContain('语义模式')
    expect(wrapper.text()).toContain('返回列表')
  })

  it('摘要区显示 L3 自己的检索方式，且与 L2 的模式互不覆盖（M22）', async () => {
    vi.mocked(memoryApi.searchMemory).mockResolvedValue({
      query: '部署方案',
      // L2 走关键词降级……
      mode: 'keyword',
      count: 0,
      results: [],
      // ……而 L3 只做了"取最近"（没有查询向量）。两个模式必须各说各话。
      summaries: [
        {
          id: 's1',
          session_id: 'sess-1',
          content: '上次讨论的部署方案',
          topics: [],
          turn_range: [0, 3],
          access_count: 1,
          created_at: null,
          has_embedding: true,
          status: 'active',
        },
      ],
      summary_count: 1,
      summary_mode: 'recent',
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="语义检索"]').setValue('部署方案')
    const searchBtn = wrapper.findAll('button').find((b) => b.text().includes('智能检索'))
    await searchBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('关键词模式')
    expect(wrapper.find('.summary-mode-tag').exists()).toBe(true)
    expect(wrapper.find('.summary-mode-tag').text()).toContain('最近')
  })

  it('L3 报 empty 时不显示模式标签（那是"没拿到"，不是一种检索方式）', async () => {
    vi.mocked(memoryApi.searchMemory).mockResolvedValue({
      query: '部署方案',
      mode: 'keyword',
      count: 0,
      results: [],
      summaries: [],
      summary_count: 0,
      summary_mode: 'empty',
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="语义检索"]').setValue('部署方案')
    const searchBtn = wrapper.findAll('button').find((b) => b.text().includes('智能检索'))
    await searchBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.summary-mode-tag').exists()).toBe(false)
  })

  it('后端未带 summary_mode 时（旧版本）也不显示标签', async () => {
    // 这一条**独立于上一条**：上一条没有摘要，标题整块不渲染，标签"不存在"是顺带的；
    // 这里摘要非空、只有 mode 缺失 —— 它才是 `default` 分支真正的覆盖点。
    vi.mocked(memoryApi.searchMemory).mockResolvedValue({
      query: '部署方案',
      mode: 'keyword',
      count: 0,
      results: [],
      summaries: [
        {
          id: 's1',
          session_id: 'sess-1',
          content: '上次讨论的部署方案',
          topics: [],
          turn_range: [0, 3],
          access_count: 1,
          created_at: null,
          has_embedding: true,
          status: 'active',
        },
      ],
      summary_count: 1,
      // summary_mode 缺席 = 后端还没升级到双区契约
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('input[placeholder*="语义检索"]').setValue('部署方案')
    const searchBtn = wrapper.findAll('button').find((b) => b.text().includes('智能检索'))
    await searchBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('上次讨论的部署方案')
    expect(wrapper.find('.summary-mode-tag').exists()).toBe(false)
  })

  it('点击「智能整理」触发 startOrganize', async () => {
    vi.mocked(memoryApi.startOrganize).mockResolvedValue({
      started: true,
      status: { running: true, started_at: 1, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()
    const buttons = wrapper.findAll('button')
    const orgBtn = buttons.find((b) => b.text().includes('智能整理'))
    expect(orgBtn).toBeTruthy()
    await orgBtn!.trigger('click')
    await flushPromises()
    expect(memoryApi.startOrganize).toHaveBeenCalledOnce()
  })

  it('「待裁决」分区展示冲突卡片（旧值 / 新值可读）', async () => {
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({
      conflicts: [
        {
          conflict_id: 'cfl-1',
          slot: 'fact',
          item_key: 'city',
          old_value: '上海',
          new_value: '北京',
          source: 'derived',
          created_at: Date.now() / 1000,
        },
      ],
      count: 1,
    })
    const wrapper = mountView()
    await flushPromises()
    await openTab(wrapper, '待裁决')

    const text = wrapper.text()
    expect(text).toContain('记忆冲突')
    expect(text).toContain('上海')
    expect(text).toContain('北京')
    // 三条裁决路径必须都在，否则冲突只能被忽略
    for (const label of ['保留当前值', '采用新值', '手动修改']) {
      expect(text).toContain(label)
    }
  })

  it('「摘要」分区展示历史对话摘要', async () => {
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({
      summaries: [
        {
          id: 'sms-1',
          session_id: 'sess-1',
          content: '讨论了 Go 微服务拆分方案',
          topics: ['Go'],
          turn_range: [0, 5],
          access_count: 0,
          created_at: null,
          has_embedding: false,
          status: 'active',
        },
      ],
      count: 1,
    })
    const wrapper = mountView()
    await flushPromises()
    await openTab(wrapper, '摘要')
    expect(wrapper.text()).toContain('讨论了 Go 微服务拆分方案')
  })

  it('「在对话中使用」跳到对话并按分类收窄注入范围', async () => {
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries: [makeEntry({ slot: 'preference' })],
      counts: { identity: 0, preference: 1, decision: 0, fact: 0 },
      total: 1,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
    const wrapper = mountView()
    await flushPromises()

    await router.push('/')
    const btn = wrapper
      .findAll('button')
      .find((b) => (b.attributes('title') || '').includes('在对话中使用'))
    expect(btn, '未找到「在对话中使用」按钮').toBeTruthy()
    await btn!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/chat')
    expect(router.currentRoute.value.query.memory).toBe('preference')
  })

  it('切换「含归档」会带 includeArchived 重新加载列表', async () => {
    const wrapper = mountView()
    await flushPromises()
    vi.mocked(memoryApi.listMemory).mockClear()

    const sw = wrapper.find('.ant-switch')
    expect(sw.exists()).toBe(true)
    await sw.trigger('click')
    await flushPromises()

    expect(memoryApi.listMemory).toHaveBeenCalledWith(true)
  })
})

describe('记忆范围（全局 / 项目）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    // 模块级单例：上一条用例可能把当前项目改成了 proj-a，这里必须显式归零
    setProject('')
    vi.mocked(memoryApi.listConflicts).mockResolvedValue({ conflicts: [], count: 0 })
    vi.mocked(memoryApi.listSummaries).mockResolvedValue({ summaries: [], count: 0 })
    vi.mocked(memoryApi.getOrganizeStatus).mockResolvedValue({
      running: false,
      started_at: null,
      finished_at: null,
      result: null,
      error: null,
    })
  })

  function mockList(entries: MemorySearchHit[]) {
    vi.mocked(memoryApi.listMemory).mockResolvedValue({
      entries,
      counts: { identity: 0, preference: entries.length, decision: 0, fact: 0 },
      total: entries.length,
      slots: [],
      organize: { running: false, started_at: null, finished_at: null, result: null, error: null },
    })
  }

  it('全局视图里全局条目可编辑（只读只发生在项目视图）', async () => {
    mockList([makeEntry({ scope: 'global', project: '' })])
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-test="edit-disabled"]').exists()).toBe(false)
    // 全局视图里没有"改为全局"的入口 —— 它已经是全局了
    expect(wrapper.find('[data-test="toggle-scope"]').exists()).toBe(false)
  })

  it('项目视图里全局条目只读：编辑变成禁用态，并给出原因', async () => {
    setProject('proj-a')
    mockList([
      makeEntry({ id: 'g', key: 'lang', scope: 'global', project: '' }),
      makeEntry({ id: 'p', key: 'editor', scope: 'project', project: 'proj-a' }),
    ])
    const wrapper = mountView()
    await flushPromises()

    const disabled = wrapper.findAll('[data-test="edit-disabled"]')
    expect(disabled.length, '全局条目应当渲染成禁用的编辑按钮').toBe(1)
    // 条目自身的范围徽标把两类区分开
    const scopes = wrapper.findAll('[data-test="entry-scope"]').map((n) => n.text())
    expect(scopes).toContain('全局')
    expect(scopes).toContain('项目')
  })

  it('项目视图里把项目专属记忆提升为全局', async () => {
    setProject('proj-a')
    mockList([makeEntry({ id: 'p', key: 'editor', scope: 'project', project: 'proj-a' })])
    vi.mocked(memoryApi.moveMemoryScope).mockResolvedValue({
      entry: makeEntry({ id: 'p', scope: 'global', project: '' }),
    })
    const wrapper = mountView()
    await flushPromises()

    const btn = wrapper.find('[data-test="toggle-scope"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()

    // 目标范围空串 = 全局；入口只对项目专属条目开放，所以不必再判断方向
    expect(memoryApi.moveMemoryScope).toHaveBeenCalledWith('p', '')
    expect(memoryApi.listMemory).toHaveBeenCalledTimes(2)
  })

  it('提升为全局撞上同名 key（409）时提示用户先处理', async () => {
    setProject('proj-a')
    mockList([makeEntry({ id: 'p', key: 'editor', scope: 'project', project: 'proj-a' })])
    vi.mocked(memoryApi.moveMemoryScope).mockRejectedValue({
      response: { status: 409, data: { error: 'target scope already has a memory with key' } },
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-test="toggle-scope"]').trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('目标范围已有同名记忆')
  })
})
