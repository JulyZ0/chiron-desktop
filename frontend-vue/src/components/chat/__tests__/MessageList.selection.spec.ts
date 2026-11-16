import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MessageList from '../MessageList.vue'
import type { ChatItem } from '../chat-types'

const user = (id: string, content: string): ChatItem => ({ kind: 'text', role: 'user', content, id })
const assistant = (id: string, content: string): ChatItem => ({ kind: 'text', role: 'assistant', content, id })

async function settle(times = 6) {
  for (let i = 0; i < times; i++) await nextTick()
}

async function mountList(items: ChatItem[]) {
  const wrapper = mount(MessageList, { props: { items, loading: false } as any })
  await settle()
  return wrapper
}

/**
 * jsdom 的 Selection 实现不完整（选中未渲染文本时 toString 为空），
 * 这里直接 stub 选区：被测的是「判定选区落在消息区内 → 出菜单 → 派发引用」的逻辑。
 */
function stubSelection(text: string, ancestor: Element) {
  const rect = { left: 100, top: 200, width: 80, bottom: 220 } as DOMRect
  vi.spyOn(window, 'getSelection').mockReturnValue({
    toString: () => text,
    rangeCount: 1,
    getRangeAt: () => ({ commonAncestorContainer: ancestor, getBoundingClientRect: () => rect }),
  } as unknown as Selection)
}

function stubEmptySelection() {
  vi.spyOn(window, 'getSelection').mockReturnValue(
    { toString: () => '', rangeCount: 0, getRangeAt: () => { throw new Error('no range') } } as unknown as Selection,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MessageList 选中文本菜单', () => {
  it('未选中时不出现菜单，选中消息正文后才出现', async () => {
    const wrapper = await mountList([user('u1', '用户的问题'), assistant('a1', '助手的回答')])
    stubEmptySelection()
    await wrapper.find('.message-list').trigger('mouseup')
    expect(wrapper.find('.selection-menu').exists()).toBe(false)

    stubSelection('用户的问题', wrapper.find('.message-list').element)
    await wrapper.find('.message-list').trigger('mouseup')
    expect(wrapper.find('.selection-menu').exists()).toBe(true)
  })

  it('选区落在消息区之外（例如输入框）时不接管', async () => {
    const wrapper = await mountList([user('u1', '用户的问题'), assistant('a1', '助手的回答')])
    stubSelection('别处的文本', document.body)
    await wrapper.find('.message-list').trigger('mouseup')
    expect(wrapper.find('.selection-menu').exists()).toBe(false)
  })

  it('点「引用到输入框」把选中文本抛给父组件，并收起菜单', async () => {
    const wrapper = await mountList([user('u1', '用户的问题'), assistant('a1', '助手的回答')])
    stubSelection('用户的问题', wrapper.find('.message-list').element)
    await wrapper.find('.message-list').trigger('mouseup')

    const buttons = wrapper.findAll('.sel-btn')
    expect(buttons.length).toBe(2)
    await buttons[1]!.trigger('click')

    expect(wrapper.emitted('quote-text')?.[0]?.[0]).toBe('用户的问题')
    expect(wrapper.find('.selection-menu').exists()).toBe(false)
  })

  it('滚动时菜单收起（跟随内容漂移的菜单没有意义）', async () => {
    const wrapper = await mountList([user('u1', '用户的问题'), assistant('a1', '助手的回答')])
    stubSelection('用户的问题', wrapper.find('.message-list').element)
    await wrapper.find('.message-list').trigger('mouseup')
    expect(wrapper.find('.selection-menu').exists()).toBe(true)

    await wrapper.find('.message-list').trigger('scroll')
    expect(wrapper.find('.selection-menu').exists()).toBe(false)
  })
})
