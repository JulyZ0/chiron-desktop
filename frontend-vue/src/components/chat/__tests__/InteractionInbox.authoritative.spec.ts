import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import InteractionInbox from '../InteractionInbox.vue'
import { INTERACTIONS_CHANGED_EVENT, type LiveTicketRef } from '../pendingReconcile'

/**
 * 收件箱的**权威字段**与**实时/历史对账**（H61）。
 *
 * 旧的四处失真（都能在界面上看到）：
 *   1. 提问卡的 `expired` 传的是 `!!submitting[id]` —— **提交中**被画成"已超时"
 *      （用户在等回执时看到"超时了、你可以继续对话"）；真正的过期反而没人判；
 *   2. `expires_at` 从未被读过：服务端说这张票一小时前就失效了，界面还是可点的；
 *   3. `request.allow_free_text === false` 从未被读过：后端说"只能选"，前端照样给输入框，
 *      用户打完字才被拒；
 *   4. 同一张票既在上方实时卡片里、又在收件箱里 ⇒ 两个答复入口，点哪个都说不清。
 */
const interactionsApi = vi.hoisted(() => ({
  listPendingInteractions: vi.fn(),
  resolveInteraction: vi.fn(),
}))
vi.mock('../../../api/interactions', () => interactionsApi)

const describeApiError = vi.hoisted(() => vi.fn(() => '服务器拒绝了这个答复'))
vi.mock('../../../utils/apiError', () => ({ describeApiError }))

enableAutoUnmount(afterEach)

const questionItem = () => ({
  id: 'i2',
  kind: 'question' as const,
  status: 'pending' as const,
  tool_call_id: 'call_2',
  tool_name: 'ask_user',
  request: { question: '选哪个环境？', options: ['dev', 'prod'] },
})

const approvalItem = () => ({
  id: 'i1',
  kind: 'approval' as const,
  status: 'pending' as const,
  tool_call_id: 'call_1',
  tool_name: 'shell_exec',
  request: { question: '请求执行 shell_exec', tool_name: 'shell_exec', arguments: { command: 'ls' } },
})

function mountInbox(props: { sessionId: string; liveTickets?: LiveTicketRef[] }) {
  return mount(InteractionInbox, { props })
}

beforeEach(() => {
  vi.clearAllMocks()
  interactionsApi.resolveInteraction.mockResolvedValue(true)
})

describe('权威到期（expires_at）', () => {
  it('服务端说过期 ⇒ 卡片进入过期态（不可作答），但**保留**问题上下文', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([
      { ...questionItem(), expires_at: '2020-01-01T00:00:00Z' },
    ])
    const wrapper = mountInbox({ sessionId: 's1' })
    await flushPromises()

    const card = wrapper.find('.ask-card')
    expect(card.exists()).toBe(true)
    expect(card.classes()).toContain('expired')
    expect(card.text()).toContain('选哪个环境？')
    // 过期 ⇒ 选项按钮禁用（不再让用户点了才失败）
    expect(wrapper.find('.ask-option').attributes('disabled')).toBeDefined()
  })

  it('到期还早 ⇒ 正常可答（不因为解析不了/没到点就禁掉）', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([
      { ...questionItem(), expires_at: new Date(Date.now() + 300_000).toISOString() },
    ])
    const wrapper = mountInbox({ sessionId: 's1' })
    await flushPromises()

    expect(wrapper.find('.ask-card').classes()).not.toContain('expired')
    expect(wrapper.find('.ask-option').attributes('disabled')).toBeUndefined()
  })

  it('「提交中」不再被画成「已超时」', async () => {
    // 旧实现 `:expired="!!submitting[item.id]"`：提交期间卡片变成过期态
    let release: (ok: boolean) => void = () => {}
    interactionsApi.resolveInteraction.mockImplementation(
      () => new Promise<boolean>((resolve) => { release = resolve }),
    )
    interactionsApi.listPendingInteractions.mockResolvedValue([questionItem()])
    const wrapper = mountInbox({ sessionId: 's1' })
    await flushPromises()

    await wrapper.find('.ask-option').trigger('click')
    await flushPromises()

    // 回执还没回来：卡片**不该**显示成过期
    expect(wrapper.find('.ask-card').classes()).not.toContain('expired')

    release(true)
    await flushPromises()
  })
})

describe('权威 free-text（allow_free_text）', () => {
  it('服务端说"只许选" ⇒ 不给自由输入框，选项仍在', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([
      { ...questionItem(), request: { question: '选哪个环境？', options: ['dev', 'prod'], allow_free_text: false } },
    ])
    const wrapper = mountInbox({ sessionId: 's1' })
    await flushPromises()

    expect(wrapper.findAll('.ask-option')).toHaveLength(2)
    expect(wrapper.find('.ask-input').exists()).toBe(false)
  })

  it('服务端允许（或缺省）⇒ 自由输入框在', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([questionItem()])
    const wrapper = mountInbox({ sessionId: 's1' })
    await flushPromises()

    expect(wrapper.find('.ask-input').exists()).toBe(true)
  })
})

describe('历史 ↔ 实时对账', () => {
  it('实时已有这张票 ⇒ 收件箱不再给第二个答复入口（票在上方卡片里）', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem()])
    const wrapper = mountInbox({
      sessionId: 's1',
      // 实时通道里同一张票（键是 tool_call_id）
      liveTickets: [{ id: 'call_1', kind: 'approval', toolCallId: 'call_1' }],
    })
    await flushPromises()

    expect(wrapper.find('.approval-card').exists()).toBe(false)
    // 全部行都被实时覆盖 ⇒ 收件箱整块不渲染（上方实时卡片才是入口）
    expect(wrapper.find('.interaction-inbox').exists()).toBe(false)
  })

  it('只有历史独有的票留在收件箱（对账不吞掉真正待答的票）', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem(), questionItem()])
    const wrapper = mountInbox({
      sessionId: 's1',
      liveTickets: [{ id: 'call_1', kind: 'approval', toolCallId: 'call_1' }],
    })
    await flushPromises()

    expect(wrapper.find('.approval-card').exists()).toBe(false) // 这张被实时覆盖
    expect(wrapper.find('.ask-card').exists()).toBe(true)        // 这张只在历史里
  })

  it('答复成功后广播 interactions:changed（角标不必等 30s 轮询）', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem()])
    const onChange = vi.fn()
    window.addEventListener(INTERACTIONS_CHANGED_EVENT, onChange)
    try {
      const wrapper = mountInbox({ sessionId: 's1' })
      await flushPromises()

      await wrapper.find('.approval-btn.allow').trigger('click')
      await flushPromises()

      expect(onChange).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(INTERACTIONS_CHANGED_EVENT, onChange)
    }
  })
})
