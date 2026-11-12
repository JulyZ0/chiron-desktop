import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MessageList from '../MessageList.vue'
import type { ChatItem } from '../chat-types'

const user = (id: string, content: string): ChatItem => ({ kind: 'text', role: 'user', content, id })
const assistant = (id: string): ChatItem => ({ kind: 'text', role: 'assistant', content: 'a', id })

function items(): ChatItem[] {
  return [user('u1', '第一个问题'), assistant('a1'), user('u2', '第二个问题'), assistant('a2')]
}

/** 每行固定 100px、视口 400px：行序列偏移可手算 */
const base = { measureRow: () => 100, viewportHeight: 400 }

async function settle(times = 8) {
  for (let i = 0; i < times; i++) await nextTick()
}

async function mountList(props: Record<string, unknown>) {
  const wrapper = mount(MessageList, { props: { loading: false, ...props } as any })
  await settle()
  return wrapper
}

describe('MessageList 提问导航条', () => {
  it('提问少于两个时不出现（一个提问自己跳自己没意义）', async () => {
    const wrapper = await mountList({ items: [user('u1', '只有一个问题'), assistant('a1')] })
    expect(wrapper.findAll('.rail-dot').length).toBe(0)
  })

  it('两个以上提问时每个提问一个标记，预览取自提问内容', async () => {
    const wrapper = await mountList({ items: items(), ...base })
    const dots = wrapper.findAll('.rail-dot')
    expect(dots.length).toBe(2)
    expect(dots[0]!.attributes('title')).toBe('第一个问题')
    expect(dots[1]!.attributes('title')).toBe('第二个问题')
  })

  it('点击标记跳到对应提问：经单写者写入该行的几何偏移', async () => {
    const wrapper = await mountList({ items: items(), ...base })
    const el = wrapper.find('.message-list').element as HTMLElement

    await wrapper.findAll('.rail-dot')[1]!.trigger('click')
    await settle()
    // 小会话不启用窗口化 ⇒ 不做 DOM 实测，几何用 kind 估计值（text = 96px），
    // 因此第 3 行（items 下标 2）的偏移是 192 —— 关键是写入落在该行而不是原地不动
    expect(el.scrollTop).toBe(192)
  })

  it('多行提问的预览压成单行', async () => {
    const wrapper = await mountList({
      items: [user('u1', '第一行\n第二行\t缩进'), assistant('a1'), user('u2', 'x'), assistant('a2')],
      ...base,
    })
    expect(wrapper.findAll('.rail-dot')[0]!.attributes('title')).toBe('第一行 第二行 缩进')
  })

  it('focusToken 也能跳到助手消息（会话内检索需要，不限于用户提问）', async () => {
    const wrapper = await mountList({ items: items(), ...base })
    const el = wrapper.find('.message-list').element as HTMLElement

    await wrapper.setProps({ focusIndex: 1, focusToken: 1 })   // items 下标 1 = 助手回复
    await settle()
    expect(el.scrollTop).toBe(96)                              // 1 × 96px（text 估计值）
  })
})

/** 从行内 style 读宽度（px 数值），用于断言律动。
 *  入参是 DOMWrapper（findAll 的返回值），不是原生元素 —— 前者要取 .element。 */
function widthOf(w: { element: Element }): number {
  return Number.parseFloat((w.element as HTMLElement).style.width || '0')
}

describe('MessageList 锚点律动（近长远短）', () => {
  const threeQuestions = (): ChatItem[] => [
    user('u1', '问一'), assistant('a1'),
    user('u2', '问二'), assistant('a2'),
    user('u3', '问三'), assistant('a3'),
  ]

  it('未悬停时所有锚点等长（激活项略长），不出现律动', async () => {
    const wrapper = await mountList({ items: threeQuestions(), ...base })
    const widths = wrapper.findAll('.rail-dot').map(widthOf)
    expect(widths.length).toBe(3)
    // 等长基值：未激活 10px；第一条在视口内被标 active 时是 16px。
    // 关键断言是"只有两种取值"，即没有被悬停带出的衰减层次。
    expect(new Set(widths).size).toBeLessThanOrEqual(2)
  })

  it('悬停某条时以它为中心向两侧衰减：最近最长，越远越短', async () => {
    const wrapper = await mountList({ items: threeQuestions(), ...base })
    const dots = wrapper.findAll('.rail-dot')

    await dots[1]!.trigger('mouseenter')
    await settle()
    const w = dots.map(widthOf)
    // 中心（下标 1）最长，两侧对称地变短
    expect(w[1]).toBeGreaterThan(w[0])
    expect(w[1]).toBeGreaterThan(w[2])
    expect(w[0]).toBe(w[2])
  })

  it('悬停移出后恢复等长', async () => {
    const wrapper = await mountList({ items: threeQuestions(), ...base })
    const dots = wrapper.findAll('.rail-dot')

    await dots[0]!.trigger('mouseenter')
    await settle()
    const hovered = dots.map(widthOf)
    expect(new Set(hovered).size).toBeGreaterThan(1)

    await wrapper.find('.question-rail').trigger('mouseleave')
    await settle()
    expect(new Set(dots.map(widthOf)).size).toBeLessThanOrEqual(2)
  })
})
