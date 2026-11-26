/**
 * `ChatView` 的**会话隔离**接线（H59）+ 提交失败不丢票（H61）。
 *
 * 与 `ChatView.interaction.spec.ts`（走一遍"提交 → 帧 → transcript"）互补：这里钉的是
 * **多会话并行**下的归属，以及"提交没成功"这条分支 —— 两者都是全局状态时代的盲区。
 *
 * 为什么必须在视图层测：`interactionScope` / `sseBySession` 本身在各自单测里已被钉住，
 * 但"ChatView 真的把事件写进了正确的桶里、真的只关来源流"只能在这里证明 ——
 * 单测证明不了接线（上一轮 H58/H66/H68 的教训）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

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
  })) as unknown as typeof window.matchMedia
}

if (typeof globalThis.crypto?.randomUUID !== 'function') {
  Object.defineProperty(globalThis.crypto ?? (globalThis.crypto = {} as Crypto), 'randomUUID', {
    value: () => '00000000-0000-4000-8000-000000000000',
    configurable: true,
  })
}

/**
 * 建流替身。
 *
 * 判据：**带 `onLastEventId` 的是本轮的会话流**（ChatView 要持久化续传游标），
 * 会话级子 Agent 流不带它 —— 两条流指向同一个 `session_id`，只能靠这个区分。
 */
const net = vi.hoisted(() => {
  type Entry = {
    sid: string
    onMessage: (raw: unknown, meta?: { id: string }) => void
    onError: () => void
    close: ReturnType<typeof vi.fn>
    closed: boolean
  }
  const turn = new Map<string, Entry>()
  const subagent: Entry[] = []
  return { turn, subagent }
})

const apiMocks = vi.hoisted(() => {
  const emptyList = () => Promise.resolve({ data: { data: [] } })
  return {
    emptyList,
    api: {
      get: vi.fn(emptyList),
      post: vi.fn((url?: string, body?: unknown): Promise<unknown> => {
        void url; void body
        return Promise.resolve({ data: { data: [] } })
      }),
      put: vi.fn(emptyList),
      delete: vi.fn(emptyList),
    },
    createSSEConnection: vi.fn((
      sessionId: string,
      onMessage: (raw: unknown, meta?: { id: string }) => void,
      onError: () => void,
      opts?: { onLastEventId?: (id: string) => void },
    ) => {
      const entry = {
        sid: sessionId,
        onMessage,
        onError,
        closed: false,
        close: vi.fn(() => { entry.closed = true }),
      }
      if (opts?.onLastEventId) net.turn.set(sessionId, entry)
      else net.subagent.push(entry)
      return { close: entry.close }
    }),
    submitApproval: vi.fn(() => Promise.resolve(true)),
    submitAnswer: vi.fn(() => Promise.resolve(true)),
    updateConversation: vi.fn(emptyList),
    createShare: vi.fn(emptyList),
    getActiveShare: vi.fn(emptyList),
    revokeShare: vi.fn(emptyList),
    getChatSessionMessages: vi.fn(emptyList),
    resolveMediaUrl: vi.fn((u: string) => u),
    listModels: vi.fn(() => Promise.resolve([
      { provider: 'test-provider', name: 'test-model', display_name: 'Test Model', context_window: 8192 },
    ])),
    createAgent: vi.fn(emptyList),
    interruptAgentRun: vi.fn(() => Promise.resolve(true)),
  }
})

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, ...apiMocks }
})

vi.mock('../../api/sessionRuntime', () => ({
  getSessionRuntime: vi.fn(() => Promise.resolve(null)),
  putSessionRuntime: vi.fn(() => Promise.resolve()),
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 'u-1', name: 'Tester', tenant_id: 't-1' }, token: 'tk' }),
}))
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ isDark: false, toggle: vi.fn() }),
}))

import ChatView from '../ChatView.vue'
import ChatInput from '../../components/chat/ChatInput.vue'
import { message } from 'ant-design-vue'

const STUBS = {
  // 这两个**不能**用 `true` 打桩：ChatView 会调用它们的实例方法
  // （`scrollToBottom` / `refresh`），布尔桩没有方法 ⇒ TypeError。
  MessageList: { template: '<div />', methods: { scrollToBottom: () => {} } },
  InteractionInbox: { template: '<div />', methods: { refresh: () => {} } },
  MessageItem: true,
  ChatInput: false,
  ChatSidePanel: true,
  SubAgentPanel: true,
  SessionPreviewPane: true,
  SessionStatsPanel: true,
  CallChainTimeline: true,
  FloatingPanel: true,
  SaveToKnowledgeDialog: true,
  SaveToMemoryDialog: true,
  ChatDisplaySettings: true,
  ChatStatusBar: true,
  AskCard: true,
  ChatSetupTour: true,
  TodoPanel: true,
  WorkbenchPanel: true,
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

interface Ticket {
  id: string
  toolName?: string
  arguments?: string
  sessionId?: string
  error?: string
  busy?: boolean
}

type Vm = {
  loading: boolean
  pendingApprovals: Ticket[]
  interactionScope: { approvalsOf: (sid: string) => Ticket[] }
  resolveApproval: (a: Ticket, approved: boolean) => Promise<boolean>
  switchSession: (id: string) => Promise<void>
}

/** 挂载 + 选定 s-1（会话列表给两个会话，切换不必再拉列表）。
 *  `running` 里的会话，其详情响应带 `running: true` —— 用来模拟"刷新时有 run 在跑"。 */
async function mountChat(running: string[] = []) {
  const runningSet = new Set(running)
  apiMocks.api.get.mockImplementation((url?: string) => {
    if (url === '/v1/conversations') {
      return Promise.resolve({
        data: {
          data: [
            { id: 's-1', title: '会话 1', updated_at: '2026-10-08T00:00:00Z' },
            { id: 's-2', title: '会话 2', updated_at: '2026-10-08T00:00:00Z' },
          ],
        },
      }) as never
    }
    if (runningSet.size && url?.startsWith('/v1/conversations/') && url.includes('limit=')) {
      const id = decodeURIComponent(url.split('/')[3].split('?')[0])
      return Promise.resolve({
        data: {
          data: {
            id,
            messages: [],
            tool_calls: [],
            running: runningSet.has(id),
          },
        },
      }) as never
    }
    return apiMocks.emptyList() as never
  })
  const wrapper = mount(ChatView, { global: { plugins: [router, createPinia()], stubs: STUBS } })
  await flushPromises()
  await flushPromises()
  return { wrapper, vm: wrapper.vm as unknown as Vm }
}

async function send(wrapper: Awaited<ReturnType<typeof mountChat>>['wrapper'], text = '你好') {
  wrapper.findComponent(ChatInput).vm.$emit('send', text, [])
  await flushPromises()
  await flushPromises()
}

/** 往某个会话的**本轮会话流**里喂一帧（只喂那一条，才测得出归属） */
function feedTurn(sid: string, type: string, data: Record<string, unknown> = {}) {
  const entry = net.turn.get(sid)
  if (!entry) throw new Error(`no turn stream for ${sid}`)
  entry.onMessage({ type, data }, { id: `frame-${type}` })
}

describe('ChatView 会话隔离（H59）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    net.turn.clear()
    net.subagent.length = 0
    apiMocks.listModels.mockImplementation(() => Promise.resolve([
      { provider: 'test-provider', name: 'test-model', display_name: 'Test Model', context_window: 8192 },
    ]))
    apiMocks.submitApproval.mockImplementation(() => Promise.resolve(true))
  })

  it('★ 终态事件只关**来源流**：s-1 收尾不会掐断 s-2 仍在跑的流', async () => {
    const { wrapper, vm } = await mountChat()
    await send(wrapper)                 // s-1 起一轮
    // /submit 必须带 __retainAcrossNavigation：路由切换的 cancelAll 不能把它 abort 掉，
    // 否则客户端判失败入 outbox，服务端其实已受理 ⇒ 重发执行两次。
    expect(apiMocks.api.post).toHaveBeenCalledWith(
      '/submit',
      expect.anything(),
      expect.objectContaining({ __retainAcrossNavigation: true }),
    )
    await vm.switchSession('s-2')
    await send(wrapper, '你好，B')       // s-2 起一轮

    const a = net.turn.get('s-1')!
    const b = net.turn.get('s-2')!
    expect(a.closed).toBe(false)
    expect(b.closed).toBe(false)

    // s-1 这一轮收尾（用户此刻正看着 s-2）
    feedTurn('s-1', 'turn_done', { session_id: 's-1' })
    await flushPromises()

    expect(a.closed).toBe(true)   // 来源流关掉
    expect(b.closed).toBe(false)  // 别人的流一根汗毛都不动 —— 旧实现这里会关掉 b
    expect(vm.loading).toBe(true) // s-2 仍在跑
  })

  it('★ 刷新/切回时服务端 running=true 会自动重挂流（不是"已结束"）', async () => {
    const { vm } = await mountChat(['s-1'])
    // 初始加载会 switchSession(s-1)；running=true ⇒ 必须建流且 loading 为真
    expect(net.turn.has('s-1')).toBe(true)
    expect(vm.loading).toBe(true)

    // 切到未运行的 s-2：s-1 的流保留（后台仍在跑），但当前视图 loading=false
    await vm.switchSession('s-2')
    expect(vm.loading).toBe(false)
    expect(net.turn.get('s-1')!.closed).toBe(false)
  })

  it('★ approval 帧落进**发事件的那个会话**，不落进当前视图会话', async () => {
    const { wrapper, vm } = await mountChat()
    await send(wrapper) // s-1 起一轮
    await vm.switchSession('s-2')

    feedTurn('s-1', 'approval', { id: 'call_a', name: 'run_command', arguments: '{"cmd":"ls"}' })
    await flushPromises()

    // 当前视图是 s-2：不该出现"在等我批准"的卡片（旧实现会画在这里）
    expect(vm.pendingApprovals).toHaveLength(0)
    // 票在 s-1 的桶里，归属被冻结
    const a = vm.interactionScope.approvalsOf('s-1')
    expect(a).toHaveLength(1)
    expect(a[0]!.sessionId).toBe('s-1')

    // 切回 s-1，卡片回来了（旧实现：切走时票已被清空，永远回不来）
    await vm.switchSession('s-1')
    await flushPromises()
    expect(vm.pendingApprovals.map((t) => t.id)).toEqual(['call_a'])
  })

  it('★ 错配会话的审批被拒绝提交：票留下，请求不发出', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const { wrapper, vm } = await mountChat()
      await send(wrapper)
      feedTurn('s-1', 'approval', { id: 'call_a', name: 'run_command', arguments: '{}' })
      await flushPromises()
      const ticket = vm.interactionScope.approvalsOf('s-1')[0]!

      // 用户已经切到 s-2，界面上这张票属于 s-1
      await vm.switchSession('s-2')
      await flushPromises()

      const accepted = await vm.resolveApproval(ticket, true)
      await flushPromises()

      expect(accepted).toBe(false)
      expect(apiMocks.submitApproval).not.toHaveBeenCalled() // 没有把批准发到 s-2
      expect(vm.interactionScope.approvalsOf('s-1')).toHaveLength(1) // 票还在
    } finally {
      warn.mockRestore()
    }
  })
})

describe('ChatView 审批失败不丢票（H61）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    net.turn.clear()
    net.subagent.length = 0
    apiMocks.listModels.mockImplementation(() => Promise.resolve([
      { provider: 'test-provider', name: 'test-model', display_name: 'Test Model', context_window: 8192 },
    ]))
  })

  it('★ 提交失败 ⇒ 票仍在、错误可见、可以重试；成功才摘票', async () => {
    const errSpy = vi.spyOn(message, 'error').mockImplementation((() => undefined) as never)
    try {
      const { wrapper, vm } = await mountChat()
      await send(wrapper)
      feedTurn('s-1', 'approval', { id: 'call_a', name: 'run_command', arguments: '{}' })
      await flushPromises()
      const ticket = vm.interactionScope.approvalsOf('s-1')[0]!
      expect(ticket).toBeTruthy()

      // 第一次提交失败（网关 502 / 网络断）—— 旧实现吞掉异常后照样删卡片
      apiMocks.submitApproval.mockImplementationOnce(() => Promise.reject(new Error('gateway down')))
      const first = await vm.resolveApproval(ticket, true)
      await flushPromises()

      expect(first).toBe(false)
      const kept = vm.interactionScope.approvalsOf('s-1')
      expect(kept).toHaveLength(1)             // 票没丢
      // 失败原因写在票上（`describeApiError` 会把非 HTTP 错误归一成一句可行动文案）
      expect(String(kept[0]!.error || '')).not.toBe('')
      expect(kept[0]!.busy).toBe(false)        // 按钮可再点

      // 重试成功 ⇒ 这时才摘票
      const second = await vm.resolveApproval(kept[0]!, true)
      await flushPromises()
      expect(second).toBe(true)
      expect(apiMocks.submitApproval).toHaveBeenCalledTimes(2)
      // 提交用的是**票自己的**归属会话
      // `submitApproval` 在桩里是零参 mock，`calls` 的元素类型因此是空元组；
      // 这里按实际传参取值（只修类型，断言不变）。
      const calls = apiMocks.submitApproval.mock.calls as unknown as Array<[{ session_id?: string }?]>
      const call = calls[1]?.[0]
      expect(call?.session_id).toBe('s-1')
      expect(vm.interactionScope.approvalsOf('s-1')).toHaveLength(0)
    } finally {
      errSpy.mockRestore()
    }
  })

  it('★ 服务端答了"这个决定不生效"（ok=false）⇒ 也不算确认：票留着、可重试', async () => {
    const { wrapper, vm } = await mountChat()
    await send(wrapper)
    feedTurn('s-1', 'approval', { id: 'call_b', name: 'run_command', arguments: '{}' })
    await flushPromises()
    const ticket = vm.interactionScope.approvalsOf('s-1')[0]!

    // HTTP 200 但引擎说"票已超时 / 该调用已不在等待"：`submitApproval` 解析出 false
    apiMocks.submitApproval.mockImplementationOnce(() => Promise.resolve(false))
    const accepted = await vm.resolveApproval(ticket, true)
    await flushPromises()

    expect(accepted).toBe(false)
    const kept = vm.interactionScope.approvalsOf('s-1')
    expect(kept).toHaveLength(1)              // 假成功会让这张票凭空消失
    expect(String(kept[0]!.error || '')).not.toBe('')
    expect(kept[0]!.busy).toBe(false)
  })
})
