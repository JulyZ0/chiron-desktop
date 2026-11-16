import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ContextLayers from '../ContextLayers.vue'

/**
 * 用户反馈"上下文信息没有详细内容（系统提示/工具定义/输入/回复/工具输出）"。
 * 根因是这些数只拼进 `ContextRing` 的 `title` 属性，必须悬停才看得到。
 * 本组件是它们真正的可见载体，所以这里断言的是**渲染出来的文本**。
 */
const full = { system: 4000, tools: 5000, user: 6000, assistant: 7000, tool_results: 8000 }

describe('ContextLayers（上下文明细）', () => {
  it('五类分项各自成行，标签与数值都渲染出来', () => {
    const wrapper = mount(ContextLayers, { props: { breakdown: full } })
    const labels = wrapper.findAll('.ctx-layers__label').map(n => n.text())

    expect(labels).toContain('系统提示')
    expect(labels).toContain('工具定义')
    expect(labels).toContain('你的输入')
    expect(labels).toContain('模型回复')
    expect(labels).toContain('工具输出')
    // 数值用完整数字，便于对着账单核对
    for (const n of ['4,000', '5,000', '6,000', '7,000', '8,000']) {
      expect(wrapper.text()).toContain(n)
    }
  })

  it('给出合计，且等于各项之和', () => {
    const wrapper = mount(ContextLayers, { props: { breakdown: full } })
    const totalRow = wrapper.find('.ctx-layers__row--total')
    expect(totalRow.text()).toContain('合计')
    expect(totalRow.text()).toContain('30,000')
  })

  it('条形宽度按**分项之和**的份额，而不是按模型窗口', () => {
    const wrapper = mount(ContextLayers, { props: { breakdown: full } })
    const widths = wrapper.findAll('.ctx-layers__bar i').map(n => n.attributes('style') ?? '')
    // 8000/30000 ≈ 26.67%
    expect(widths[4]).toContain('26.6')
    expect(widths[0]).toContain('13.3')
  })

  it('没有分项数据时如实说没有，不渲染空的条形图', () => {
    for (const breakdown of [null, undefined, {}]) {
      const wrapper = mount(ContextLayers, { props: { breakdown } })
      expect(wrapper.find('.ctx-layers__empty').exists()).toBe(true)
      expect(wrapper.findAll('.ctx-layers__bar')).toHaveLength(0)
    }
  })

  it('最大项单独列出，只有角色/大小', () => {
    const wrapper = mount(ContextLayers, {
      props: {
        breakdown: full,
        largest: [{ role: 'tool_results', tokens: 8000 }, { role: 'user', tokens: 6000 }],
      },
    })
    const largest = wrapper.find('.ctx-layers__largest')
    expect(largest.text()).toContain('最大的几条')
    expect(largest.text()).toContain('tool_results')
    expect(largest.text()).toContain('8.0k')
  })

  it('估算值必须自报家门', () => {
    const flagged = mount(ContextLayers, { props: { breakdown: full, estimated: true } })
    expect(flagged.text()).toContain('估算')

    const plain = mount(ContextLayers, { props: { breakdown: full } })
    expect(plain.text()).not.toContain('估算')
  })
})
