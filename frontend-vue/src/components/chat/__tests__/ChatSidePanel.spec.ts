import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ChatSidePanel from '../ChatSidePanel.vue'

const api = vi.hoisted(() => ({
  api: { get: vi.fn() },
  listTools: vi.fn(),
  quickExecute: vi.fn(),
}))
vi.mock('../../../api', () => api)

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

// 侧栏底部新增的用户入口引入 authStore；本测试不注入 pinia，
// 故沿用既有 mock 风格把它 mock 掉。
vi.mock('../../../stores/auth', () => ({
  useAuthStore: () => ({ user: null, logout: vi.fn() }),
}))

enableAutoUnmount(afterEach)

beforeAll(() => {
  // 组件顶层就读 matchMedia 判定抽屉模式，jsdom 默认没有实现
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      // ant-design-vue 的 responsiveObserve 仍走旧 API（会话视图会触发），
      // 缺这两个方法会抛 `mql.addListener is not a function`
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

beforeEach(() => {
  vi.clearAllMocks()
})

/** open: false —— 活动轮询会直接返回，测试只关心「可用工具」区块 */
function mountPanel(
  sessions: any[] = [],
  view: 'trajectory' | 'sessions' = 'trajectory',
  contextChips: Array<{ type: 'kb' | 'agent' | 'skill' | 'plugin' | 'memory'; label: string; value: string; weight?: number }> = [],
) {
  return mount(ChatSidePanel, {
    props: {
      items: [],
      selectedIndex: null,
      open: false,
      view,
      sessions,
      activeSessionId: '',
      contextChips,
    },
    // 底部主题 / 用户入口的两个子组件各自依赖 pinia（themeStore / authStore）。
    // 本测试只关心「可用工具」区块，故 stub 掉它们 —— 保持测试不引入 pinia，
    // 与上面 mock api / vue-router 的风格一致。
    global: {
      stubs: { ThemeSwitcher: true, SettingsPanel: true },
    },
  })
}

describe('ChatSidePanel · 分支会话标记（P0：让"分支"在会话列表里可辨）', () => {
  const branchSession = {
    id: 'b1',
    title: '分支会话',
    created_at: '2026-09-23T10:00:00Z',
    updated_at: '2026-09-23T10:00:00Z',
    parent_session_id: '11111111-1111-1111-1111-111111111111',
    parent_title: '父会话',
    branch_from_seq: 12,
  }
  const plainSession = {
    id: 's1',
    title: '普通会话',
    created_at: '2026-09-23T11:00:00Z',
    updated_at: '2026-09-23T11:00:00Z',
  }

  it('分支会话显示「分支」徽标，悬浮说明给出父会话与分叉点', async () => {
    const w = mountPanel([branchSession, plainSession], 'sessions')
    await flushPromises()

    const badges = w.findAll('.session-branch')
    expect(badges).toHaveLength(1)
    expect(badges[0].text()).toBe('分支')
    expect(badges[0].attributes('title')).toContain('分支自《父会话》')
    expect(badges[0].attributes('title')).toContain('第 12 条起')
  })

  it('父会话已删（后端查不到展示名）时不编造来源，只说明分叉点', async () => {
    const w = mountPanel([{ ...branchSession, parent_title: '' }], 'sessions')
    await flushPromises()

    const tip = w.find('.session-branch').attributes('title') || ''
    expect(tip).toContain('已删除')
    expect(tip).toContain('第 12 条起')
  })

  it('普通会话没有分支徽标（否则所有会话都被误判为分支）', async () => {
    const w = mountPanel([plainSession], 'sessions')
    await flushPromises()

    expect(w.find('.session-branch').exists()).toBe(false)
  })
})

/**
 * 多库检索权重（P1-8）：选中多个知识库时，侧栏可展开滑块调整各自权重。
 *
 * 三条界面契约：单库不出现（权重对单库无意义）、≥2 库可展开、滑块发出
 * `kb-weight` 事件且值已被收敛到 0.1–3.0（父级只负责写回，不做二次校验）。
 */
describe('ChatSidePanel · 知识库权重（P1-8）', () => {
  const kb = (value: string, weight?: number) => ({
    type: 'kb' as const,
    label: `知识库 ${value}`,
    value,
    ...(weight === undefined ? {} : { weight }),
  })

  it('只有一个知识库时不出现权重入口（避免噪音）', () => {
    const w = mountPanel([], 'trajectory', [kb('kb-1')])
    expect(w.find('[data-testid="kb-weights-toggle"]').exists()).toBe(false)
  })

  it('★ 两个知识库时可展开，滑块初始值来自 chip 权重（缺省 1.0）', async () => {
    const w = mountPanel([], 'trajectory', [kb('kb-1', 2.5), kb('kb-2')])
    const toggle = w.find('[data-testid="kb-weights-toggle"]')
    expect(toggle.exists()).toBe(true)

    expect(w.find('[data-testid="kb-weights-body"]').exists()).toBe(false)
    await toggle.trigger('click')

    const rows = w.findAll('.weight-row')
    expect(rows).toHaveLength(2)
    const sliders = w.findAll('.weight-slider')
    expect((sliders[0]!.element as HTMLInputElement).value).toBe('2.5')
    expect((sliders[1]!.element as HTMLInputElement).value).toBe('1')
    expect(w.findAll('.weight-value').map(e => e.text())).toEqual(['2.5', '1.0'])
  })

  it('★ 拖动滑块发出 kb-weight（越界值被收敛到 [0.1, 3.0]）', async () => {
    const w = mountPanel([], 'trajectory', [kb('kb-1'), kb('kb-2')])
    await w.find('[data-testid="kb-weights-toggle"]').trigger('click')

    const slider = w.findAll('.weight-slider')[0]!
    await slider.setValue('2.4')
    expect(w.emitted('kb-weight')?.[0]).toEqual(['kb-1', 2.4])

    // 手写/粘贴越界值：组件自己收敛，父级拿到的始终合法
    await slider.setValue('9')
    expect(w.emitted('kb-weight')?.[1]).toEqual(['kb-1', 3])
  })
})
