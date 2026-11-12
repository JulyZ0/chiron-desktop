import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ContextRing from '../ContextRing.vue'

const ring = (used: number | null, limit: number | null) =>
  mount(ContextRing, { props: { used, limit } })

describe('ContextRing（上下文占用环）', () => {
  it('按已用/上限给出百分比', () => {
    const wrapper = ring(50000, 100000)
    expect(wrapper.find('.ctx-label').text()).toBe('50%')
    expect(wrapper.attributes('data-tone')).toBe('ok')
    expect(wrapper.attributes('title')).toContain('50000 tokens / 上限 100000')
  })

  it('高占用切换色调：70% 起 warn，90% 起 danger', () => {
    expect(ring(75000, 100000).attributes('data-tone')).toBe('warn')
    expect(ring(95000, 100000).attributes('data-tone')).toBe('danger')
  })

  it('超出上限时封顶 100%，不画出超过一圈的弧', () => {
    const wrapper = ring(250000, 100000)
    expect(wrapper.find('.ctx-label').text()).toBe('100%')
    expect(wrapper.attributes('data-tone')).toBe('danger')
  })

  it('没有已用数据或没有上限时只显示占位（不猜）', () => {
    for (const wrapper of [ring(null, 100000), ring(1200, null), ring(1200, 0)]) {
      expect(wrapper.find('.ctx-label').text()).toBe('—')
      expect(wrapper.attributes('data-tone')).toBe('idle')
      expect(wrapper.attributes('title')).toContain('暂无数据')
    }
  })

  it('弧长与比例一致', () => {
    const wrapper = ring(25000, 100000)
    const circ = 2 * Math.PI * 8
    const dasharray = wrapper.find('.ctx-bar').attributes('stroke-dasharray') ?? ''
    const dash = Number(dasharray.split(' ')[0])
    expect(dash).toBeCloseTo(circ * 0.25, 5)
  })

  // 分项按**角色**分五类，而不是按时间位置（history/current）——
  // 后者把用户消息与模型回复混成一桶，还漏掉工具定义本身，用户找不到可削减的地方。
  it('五类分项都进 title：系统/工具定义/你的输入/模型回复/工具输出', () => {
    const wrapper = mount(ContextRing, {
      props: {
        used: 30000,
        limit: 100000,
        breakdown: {
          system: 4000, tools: 5000, user: 6000, assistant: 7000, tool_results: 8000,
        },
      },
    })
    const title = wrapper.attributes('title') ?? ''

    expect(title).toContain('系统提示')
    expect(title).toContain('工具定义')
    expect(title).toContain('你的输入')
    expect(title).toContain('模型回复')
    expect(title).toContain('工具输出')
    // 每类都带上各自的数值，而不是只出现一次总和
    for (const k of ['4.0k', '5.0k', '6.0k', '7.0k', '8.0k']) {
      expect(title).toContain(k)
    }
  })

  // 服务商没回报 usage 时引擎用分项之和兜底。这个数值可信度不同，必须在提示里说清，
  // 否则用户会把估算当成账单上的真实占用。
  it('estimated 时 title 明确标注估算，未标记时不说', () => {
    const props = { used: 30000, limit: 100000 }
    const flagged = mount(ContextRing, { props: { ...props, estimated: true } })
    expect(flagged.attributes('title')).toContain('估算值')

    const plain = mount(ContextRing, { props })
    expect(plain.attributes('title')).not.toContain('估算值')
  })
})
