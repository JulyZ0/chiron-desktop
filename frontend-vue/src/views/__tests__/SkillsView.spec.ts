import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `SkillsView` 的冒烟网 + **`collectSkillUsage` 的接线**（2026-10-09）。
 *
 * 六视图补网第 4 个（`RegisterView` 487 → `KnowledgeView` 589 → `PluginsView` 702 → 本轮 854）。
 *
 * **比前两个多一半**：知识库/插件只有**一个**来源（Agent 的 `kb_id` / `plugins` 列），
 * 而技能有**两个**（`utils/skillUsage.ts:1-10`）：**Agent 的 `skills` 列** 与
 * **工作流图里 `node_type === 'skill'` 节点的 `config.skill_name`** ⇒ 接线**两条都要测** ✓，
 * 少测一条就会出现"技能明明被工作流在用，界面却说没人用" ✓。
 *
 * **为什么对用户重要**：`:452-456` 的"被哪些 Agent / 工作流使用"是**删技能前**要看的信息 ——
 * 接线断了不会崩，只会**静默不显示** ⇒ 用户会在不知情的情况下删掉别人在用的技能 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const SKILL = { name: 'summarize', description: '摘要', category: 'text' }

const apiMocks = vi.hoisted(() => ({
  // 显式 `Promise<unknown>`：否则推断成 `never[]`，`mockResolvedValue([...])` 会被 tsc 拒掉
  listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
  // 形状按源码：`/v1/skills` → `response.data?.data?.skills`（`:65`）
  //             `/v1/graphs` → `r.data?.data ?? []`（`:83`）
  get: vi.fn((url?: string): Promise<unknown> => {
    if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: [] } } })
    return Promise.resolve({ data: { data: [] } })
  }),
  post: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  put: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  // P1-2：元数据开关走 PATCH（不整份回写，避免正文被旧副本覆盖）
  patch: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
  delete: vi.fn((): Promise<unknown> => Promise.resolve({ data: {} })),
}))

vi.mock('../../api', () => ({
  api: {
    get: apiMocks.get,
    post: apiMocks.post,
    put: apiMocks.put,
    patch: apiMocks.patch,
    delete: apiMocks.delete,
  },
  listAgents: apiMocks.listAgents,
}))

import SkillsView from '../SkillsView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/', component: { template: '<div />' } }],
})

function mountView() {
  return mount(SkillsView, { global: { plugins: [router] } })
}

const titlesOf = (wrapper: ReturnType<typeof mountView>) =>
  wrapper.findAll('[title]').map(e => e.attributes('title') || '')

describe('SkillsView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.get.mockImplementation((url?: string): Promise<unknown> => {
      if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: [SKILL] } } })
      return Promise.resolve({ data: { data: [] } })
    })
    apiMocks.listAgents.mockResolvedValue([])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/v1/skills')
    expect(apiMocks.get).toHaveBeenCalledWith('/v1/graphs')   // 反向引用要拉工作流图
    expect(apiMocks.listAgents).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('页面上没有安装/发现入口：装技能统一走市场（避免两条安装路径并存）', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 页签全部撤下（两栏工作台直接展示技能列表）；安装端点也不该再被这个页面调起
    expect(wrapper.findAll('.ant-tabs-tab')).toHaveLength(0)
    expect(apiMocks.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('★ 来源一：Agent 的 skills 列 ⇒ 悬浮提示里出现该 Agent', async () => {
    apiMocks.listAgents.mockResolvedValue([{ name: '归档助手', skills: ['summarize'] }])

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('summarize')
    expect(titlesOf(wrapper).some(t => t.includes('归档助手'))).toBe(true)
    wrapper.unmount()
  })

  it('★ 来源二：工作流图里的 skill 节点 ⇒ 悬浮提示里出现该工作流（这一半只有技能有）', async () => {
    apiMocks.get.mockImplementation((url?: string): Promise<unknown> => {
      if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: [SKILL] } } })
      if (url === '/v1/graphs') {
        return Promise.resolve({
          data: {
            data: [{
              name: '日报流程',
              graph_json: { nodes: [{ node_type: 'skill', config: { skill_name: 'summarize' } }] },
            }],
          },
        })
      }
      return Promise.resolve({ data: { data: [] } })
    })

    const wrapper = mountView()
    await flushPromises()

    expect(titlesOf(wrapper).some(t => t.includes('日报流程'))).toBe(true)
    wrapper.unmount()
  })

  it('★ 两个来源都没有时不显示使用方标记（避免噪音）', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('summarize')
    expect(titlesOf(wrapper).some(t => t.includes('归档助手') || t.includes('日报流程'))).toBe(false)
    wrapper.unmount()
  })
})

// ── P1-2：触发策略 / 来源分组 / 升级 ──────────────────────────────────────

const MARKET_SKILL = {
  name: 'market-skill',
  description: '市场装的',
  version: '1.0.0',
  exec: { type: 'prompt', source: 'body' },
  origin: 'market:builtin/skill:demo',
  auto_inject: true,
  enabled: true,
}
const LOCAL_SKILL = { name: 'local-skill', description: '本地', version: '0.1.0', exec: { type: 'prompt', source: 'b' } }
const GEN_SKILL = {
  name: 'gen-skill',
  description: '生成的',
  version: '0.2.0',
  exec: { type: 'prompt', source: 'b' },
  origin: 'generated',
}

function seedSkills(list: unknown[]) {
  apiMocks.get.mockImplementation((url?: string): Promise<unknown> => {
    if (url === '/v1/skills') return Promise.resolve({ data: { data: { skills: list } } })
    return Promise.resolve({ data: { data: [] } })
  })
}

/** 芯片按可见文案取（`group-chip` 里还有计数，用 includes 匹配） */
function chip(wrapper: ReturnType<typeof mountView>, label: string) {
  return wrapper.findAll('.group-chip').find(c => (c.text() || '').includes(label))
}

describe('SkillsView · P1-2 触发策略与来源', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listAgents.mockResolvedValue([])
    seedSkills([MARKET_SKILL, LOCAL_SKILL, GEN_SKILL])
  })

  it('按 origin 分组过滤：市场安装 / 本地（含空 origin）/ 生成', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 缺省"全部"：三个都在
    expect(wrapper.text()).toContain('market-skill')
    expect(wrapper.text()).toContain('local-skill')
    expect(wrapper.text()).toContain('gen-skill')

    await chip(wrapper, '市场安装')!.trigger('click')
    expect(wrapper.text()).toContain('market-skill')
    expect(wrapper.text()).not.toContain('local-skill')
    expect(wrapper.text()).not.toContain('gen-skill')

    // origin 为空串的老数据必须归入"本地"，而不是从所有分组里消失
    await chip(wrapper, '本地')!.trigger('click')
    expect(wrapper.text()).toContain('local-skill')
    expect(wrapper.text()).not.toContain('market-skill')

    await chip(wrapper, '生成')!.trigger('click')
    expect(wrapper.text()).toContain('gen-skill')
    expect(wrapper.text()).not.toContain('local-skill')

    wrapper.unmount()
  })

  it('详情里的"自动注入"开关写元数据（PATCH 而不是 PUT）', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('.item').trigger('click')
    await flushPromises()

    const injectSwitch = wrapper.find('[aria-label="auto-inject-market-skill"]')
    expect(injectSwitch.exists()).toBe(true)
    expect(injectSwitch.classes()).toContain('ant-switch-checked')

    await injectSwitch.trigger('click')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith(
      '/v1/skills/market-skill/meta',
      { auto_inject: false },
    )
    // 用 PUT 会整份回写技能定义（正文可能被旧副本覆盖），这里不该发生
    expect(apiMocks.put).not.toHaveBeenCalled()
    expect(injectSwitch.classes()).not.toContain('ant-switch-checked')

    wrapper.unmount()
  })

  it('市场来源才显示"检查更新/升级"；点击后就地显示"已是最新"', async () => {
    apiMocks.post.mockResolvedValue({
      data: { data: { upgraded: false, reason: 'already_latest', latest_version: '1.0.0' } },
    })

    const wrapper = mountView()
    await flushPromises()

    // 本地技能不该出现升级入口（没有上游可查）
    await wrapper.findAll('.item')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('local-skill')
    expect(wrapper.find('.upgrade-row').exists()).toBe(false)

    await wrapper.findAll('.item')[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.upgrade-row').exists()).toBe(true)

    const upgradeBtn = wrapper.findAll('button').find(b => (b.text() || '').includes('检查更新'))
    expect(upgradeBtn).toBeTruthy()
    await upgradeBtn!.trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/v1/skills/market-skill/upgrade', {})
    expect(wrapper.text()).toContain('已是最新版本')

    wrapper.unmount()
  })

  it('升级成功后就地提示新版本', async () => {
    apiMocks.post.mockResolvedValue({
      data: { data: { upgraded: true, from_version: '1.0.0', to_version: '1.2.0' } },
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.findAll('.item')[0].trigger('click')
    const upgradeBtn = wrapper.findAll('button').find(b => (b.text() || '').includes('检查更新'))
    await upgradeBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('已升级')
    wrapper.unmount()
  })
})
