import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatStatusBar from '../ChatStatusBar.vue'
import type { TurnStatsItem } from '../chat-types'

const stats = (input: number, output: number, durationSec?: number): TurnStatsItem =>
  ({ kind: 'turn_stats', inputTokens: input, outputTokens: output, durationSec })

describe('ChatStatusBar（状态栏）', () => {
  it('展示模型、本轮用量与耗时', () => {
    const wrapper = mount(ChatStatusBar, {
      props: { model: 'gpt-4o', stats: stats(1200, 340, 8), online: true },
    })
    const text = wrapper.text()
    expect(text).toContain('gpt-4o')
    expect(text).toContain('1200 in / 340 out')
    expect(text).toContain('8s')
    expect(text).toContain('已连接')
  })

  it('没有用量数据时隐藏用量段，只留模型与连接状态', () => {
    const wrapper = mount(ChatStatusBar, { props: { model: 'default', stats: null, online: true } })
    expect(wrapper.text()).toContain('default')
    expect(wrapper.text()).not.toContain(' in / ')
  })

  it('未选模型时回退为「默认模型」，离线时给出提示', () => {
    const wrapper = mount(ChatStatusBar, { props: { model: '', stats: null, online: false } })
    expect(wrapper.text()).toContain('默认模型')
    expect(wrapper.text()).toContain('离线')
    expect(wrapper.find('.cs-conn').classes()).toContain('offline')
  })
})
