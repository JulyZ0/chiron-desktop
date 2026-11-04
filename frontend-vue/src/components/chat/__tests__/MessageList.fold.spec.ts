import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MessageList from '../MessageList.vue'
import { itemKey } from '../transcriptProjection'
import type { ChatItem } from '../chat-types'

const user = (id: string, turnId: string): ChatItem => ({ kind: 'text', role: 'user', content: `q-${id}`, id, turnId })
const assistant = (id: string, turnId: string): ChatItem => ({ kind: 'text', role: 'assistant', content: `a-${id}`, id, turnId })
const reasoning = (id: string, turnId: string): ChatItem => ({ kind: 'reasoning', content: 'think', id, turnId })
const call = (id: string, name: string, turnId: string): ChatItem => ({ kind: 'tool_call', id, name, arguments: '{"path":"a"}', status: 'done', turnId })
const result = (toolCallId: string, turnId: string): ChatItem => ({ kind: 'tool_result', toolCallId, content: 'out', isError: false, turnId })

/**
 * 两个回合：第一个是**已完成**回合（含思考 + 一次读取 → 工具组默认收起），
 * 第二个是活跃回合（纯正文，不产生折叠入口）。
 */
function items(): ChatItem[] {
  return [
    user('u1', 'turn-1'),
    reasoning('r1', 'turn-1'),
    call('c1', 'read_file', 'turn-1'),
    result('c1', 'turn-1'),
    assistant('a1', 'turn-1'),
    user('u2', 'turn-2'),
    assistant('a2', 'turn-2'),
  ]
}

async function settle(times = 6) {
  for (let i = 0; i < times; i++) await nextTick()
}

async function mountList(sessionKey: string) {
  const wrapper = mount(MessageList, { props: { items: items(), loading: false, sessionKey } })
  await settle()
  return wrapper
}

const rowOf = (wrapper: ReturnType<typeof mount>, index: number) =>
  wrapper.find(`[data-item-key="${itemKey(items()[index]!, index)}"]`)

beforeEach(() => {
  // 折叠态按会话持久化：每个用例用独立会话号并清空存储，避免互相污染
  localStorage.clear()
})

describe('MessageList 折叠（grouped 投影）', () => {
  it('渲染回合头与工具组头，完成的工具组默认收起', async () => {
    const wrapper = await mountList('fold-a')
    expect(wrapper.findAll('.fold-header').length).toBe(2)
    expect(wrapper.text()).toContain('读取与检索')
    expect(rowOf(wrapper, 2).exists()).toBe(false)   // 工具行被收起
    expect(rowOf(wrapper, 3).exists()).toBe(false)   // 与之配对的结果一起收起
  })

  it('展开工具组后工具行与配对结果一起出现', async () => {
    const wrapper = await mountList('fold-b')
    await wrapper.findAll('.fold-header button')[1]!.trigger('click')
    await settle()
    expect(rowOf(wrapper, 2).exists()).toBe(true)
    expect(rowOf(wrapper, 3).exists()).toBe(true)
  })

  it('收起整个回合后提问仍在，思考与正文被收起', async () => {
    const wrapper = await mountList('fold-c')
    await wrapper.findAll('.fold-header button')[0]!.trigger('click')
    await settle()
    expect(rowOf(wrapper, 0).exists()).toBe(true)    // 用户消息永不隐藏
    expect(rowOf(wrapper, 1).exists()).toBe(false)   // 思考收起
    expect(rowOf(wrapper, 4).exists()).toBe(false)   // 正文收起
    expect(wrapper.find(`[data-item-key="${itemKey(items()[0]!, 0)}"]`).exists()).toBe(true)
  })

  it('折叠状态按会话持久化：重新挂载同一会话仍保持收起', async () => {
    const first = await mountList('fold-d')
    await first.findAll('.fold-header button')[1]!.trigger('click')   // 展开工具组
    await settle()
    expect(rowOf(first, 2).exists()).toBe(true)

    const second = await mountList('fold-d')
    expect(rowOf(second, 2).exists()).toBe(true)     // 展开态被记住
  })

  it('不同会话不继承彼此的展开态', async () => {
    const first = await mountList('fold-e')
    await first.findAll('.fold-header button')[1]!.trigger('click')
    await settle()
    const other = await mountList('fold-f')
    expect(rowOf(other, 2).exists()).toBe(false)     // 新会话仍是默认收起
  })
})
