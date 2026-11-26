import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'

/**
 * `AgentsView` 的冒烟网 + 两条**接线**（2026-10-09）。
 *
 * **六视图补网的最后一个**（487 → 589 → 702 → 854 → 964 → 本轮 **1190**）——
 * 补完这一轮，第 133 轮扫出的"六个视图零测试"这条洞就**闭合**了 ✓。
 *
 * **测哪两条接线**：
 * ① **列表渲染**：`loadAgents` 把 `listAgents()` 的结果**直接**赋给 `agents`（`:51`）——
 *    注意这是**第三种形状**（前三个视图分别是 `data.data`、`data.data.skills`、
 *    `data.data.knowledge_bases`）⇒ 替身必须直接返回**数组** ✓；
 * ② **绑定资源的四源拉取 + 守卫**（`:165-184`）：`loadBindingOptions` 一次拉齐
 *    知识库/技能/插件/工作流四类，且**已加载就不再拉**（`:166-170` 的提前返回）✓ ——
 *    守卫坏了会让每次打开编辑器都打四个接口 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const AGENT = { id: 'a-1', name: '归档助手', description: '整理归档', enabled: true, visibility: 'private' }

const apiMocks = vi.hoisted(() => {
  const arr = (): Promise<unknown> => Promise.resolve([])
  return {
    // 显式 `Promise<unknown>`：否则推断成 `never[]`，`mockResolvedValue([...])` 会被 tsc 拒掉
    listAgents: vi.fn((): Promise<unknown> => Promise.resolve([])),
    listAgentSessions: vi.fn(arr),
    listKnowledgeBases: vi.fn(arr),
    listSkillResources: vi.fn(arr),
    listPlugins: vi.fn(arr),
    createAgent: vi.fn(arr),
    updateAgent: vi.fn(arr),
    deleteAgent: vi.fn(arr),
    runAgent: vi.fn(arr),
    getAgentSession: vi.fn(arr),
    setAgentVisibility: vi.fn(arr),
    // `vi.mock` 是整模块替换：`stores/auth` 在 store 建立时读 `currentAuthGeneration`，
    // 不补齐这些导出就会在 `useAuthStore()` 处抛 "No export is defined"。
    currentAuthGeneration: () => 0,
    bumpAuthGeneration: () => 0,
    awaitAuthGeneration: async () => 0,
    desktopDeviceToken: () => '',
    loadDesktopToken: async () => null,
    setAuthToken: () => {},
  }
})

vi.mock('../../api', () => apiMocks)

import AgentsView from '../AgentsView.vue'
import { useAuthStore } from '../../stores/auth'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/agents', component: { template: '<div />' } },
  ],
})

/**
 * 挂载视图。
 *
 * `currentUserId` 传 `undefined` 表示「不注入登录用户」——用于既有三条用例，
 * 保持它们原本的行为（此时 `canManage` 一律放行，见下方属主判定用例）。
 */
function mountView(currentUserId?: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  if (currentUserId !== undefined) {
    // 直接写 store 的 ref：不进 login 流程，避免多打一次接口
    useAuthStore().user = {
      id: currentUserId, email: '', name: 'me', role: 'user', tenant_id: 1,
      password_set: true, must_change_password: false,
    }
  }
  return mount(AgentsView, { global: { plugins: [router, pinia] } })
}

describe('AgentsView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listAgents.mockResolvedValue([])
    apiMocks.listAgentSessions.mockResolvedValue([])
    // 三类资源都返回**非空**，守卫才会认为"已加载过"
    apiMocks.listKnowledgeBases.mockResolvedValue([{ id: 'kb-1', name: '手册' }])
    apiMocks.listSkillResources.mockResolvedValue([{ id: 'sk-1', name: '摘要' }])
    apiMocks.listPlugins.mockResolvedValue([{ name: 'fs', command: 'mcp-fs', status: 'enabled' }])
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.listAgents).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 列表渲染：listAgents 的返回值直接用（第三种响应形状）', async () => {
    apiMocks.listAgents.mockResolvedValue([AGENT])

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('归档助手')
    wrapper.unmount()
  })

  it('★ 绑定资源四源拉齐，且已加载就不再拉（loadBindingOptions 的守卫）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as { loadBindingOptions: () => Promise<void> }

    await vm.loadBindingOptions()
    expect(apiMocks.listKnowledgeBases).toHaveBeenCalledTimes(1)
    expect(apiMocks.listSkillResources).toHaveBeenCalledTimes(1)
    expect(apiMocks.listPlugins).toHaveBeenCalledTimes(1)

    // 第二次：提前返回应拦住（三类都非空）
    await vm.loadBindingOptions()
    expect(apiMocks.listKnowledgeBases).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  /**
   * 属主判定（2026-10-31）。
   *
   * 动因：列表接口会返回本租户内**别人共享**（`visibility='tenant'`）的 Agent，
   * 而后端 `update` / `delete` / `setAgentVisibility` 三处 SQL 都带
   * `AND user_id = $N`，非属主点下去必定 403。前端此前没有任何判定，等于让用户
   * 点一次才知道不能改。
   *
   * 这里测 `canManage` 而不是去 DOM 里找置灰的 `MenuItem`：antd 组件在
   * `vitest.config.ts` 下未配 `unplugin-vue-components`，`MenuItem` 不会解析成
   * 真实组件，`disabled` 属性无从断言。测函数是这条接线的真实契约。
   */
  type CanManage = (a: { id: string; user_id?: string }) => boolean
  const canManageOf = (w: ReturnType<typeof mountView>) =>
    (w.vm as unknown as { canManage: CanManage }).canManage

  it('★ 属主判定：本人的 Agent 可编辑', async () => {
    apiMocks.listAgents.mockResolvedValue([])
    const wrapper = mountView('u-me')
    await flushPromises()

    expect(canManageOf(wrapper)({ id: 'a-1', user_id: 'u-me' })).toBe(true)
    wrapper.unmount()
  })

  it('★ 属主判定：他人共享的 Agent 不可编辑', async () => {
    const wrapper = mountView('u-me')
    await flushPromises()

    expect(canManageOf(wrapper)({ id: 'a-2', user_id: 'u-other' })).toBe(false)
    wrapper.unmount()
  })

  it('★ 属主判定：user_id 缺失或当前用户未知时放行（宁多一次 403，不误锁属主）', async () => {
    // ① 列表项没带 user_id（老数据 / 后端未回填）
    const withUser = mountView('u-me')
    await flushPromises()
    expect(canManageOf(withUser)({ id: 'a-3' })).toBe(true)
    withUser.unmount()

    // ② 拿不到当前用户 id —— 若这里判 false，属主会被锁在门外
    const anonymous = mountView()
    await flushPromises()
    expect(canManageOf(anonymous)({ id: 'a-4', user_id: 'u-someone' })).toBe(true)
    anonymous.unmount()
  })

  /**
   * 空状态的两种口径（2026-11-19）。
   *
   * 动因：市场装完 Agent 后 Agents 页显示"还没有 Agent"（空状态）。空状态模板用
   * `agents.length === 0` 判定，只要列表里**有任何一条**（哪怕已停用）就不得命中它；
   * "筛选后为空"是另一块文案，不该引导用户去市场重装。
   *
   * 两条用例分别钉：
   * ① 列表里只有 disabled 项时，页面必须列出它（「全部」分组不过滤启用态）+ 标「已停用」；
   * ② 有 Agent 但当前分组筛空时，显示"没有匹配"，且不显示"还没有 Agent"。
   */
  const EMPTY_NO_AGENT = '还没有 Agent'
  const EMPTY_NO_MATCH = '没有匹配的 Agent'

  it('★ 列表只有已停用的 Agent 时不显示空状态，且「全部」分组里可见', async () => {
    apiMocks.listAgents.mockResolvedValue([
      { id: 'a-off', name: '停用的归档助手', description: '历史遗留', enabled: false, visibility: 'private' },
    ])

    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('停用的归档助手')
    expect(text).not.toContain(EMPTY_NO_AGENT)
    expect(text).not.toContain(EMPTY_NO_MATCH)
    expect(text).toContain('已停用')
    wrapper.unmount()
  })

  it('★ 有 Agent 但筛选后为空时显示"筛选空状态"，而不是"无 Agent"', async () => {
    apiMocks.listAgents.mockResolvedValue([
      { id: 'a-other', name: '别人的助手', description: '团队共享', enabled: true, visibility: 'private', user_id: 'u-other' },
    ])

    const wrapper = mountView('u-me')
    await flushPromises()

    // 切到「我的」：唯一的 Agent 不属于当前用户 ⇒ 筛选结果为空
    ;(wrapper.vm as unknown as { groupFilter: string }).groupFilter = 'mine'
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain(EMPTY_NO_MATCH)
    expect(text).not.toContain(EMPTY_NO_AGENT)
    wrapper.unmount()
  })
})
