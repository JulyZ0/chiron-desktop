import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CallChainTimeline from '../CallChainTimeline.vue'
import { DEFAULT_PAGE_SIZE } from '../chat/traceWindow'

/**
 * G12：轨迹窗格分页 + 窗口化 + 流式跟随。
 *
 * 修前：长历史（几十轮、每轮多次工具调用）一次性渲染全部行；流式追加时也不会
 * 跟随尾部（用户得自己往下滚）；进行中的 span 会照抄后端的 0 值显示 "0ms"，
 * 看起来像"真的只花了 0 毫秒"。
 */

interface FixtureSpan {
  trace_id: string
  span_name: string
  duration_ms?: number | null
  timestamp: string
  tenant_id: number
  span_id?: string
  parent_span_id?: string
  status?: string
}

/** 单轮的长工具序列：默认页长的一倍多，确保窗口必然生效。 */
const LONG = DEFAULT_PAGE_SIZE * 2 + 20

function makeSpan(name: string, overrides: Partial<FixtureSpan> = {}): FixtureSpan {
  return {
    trace_id: 'trace-window',
    span_name: name,
    duration_ms: 5,
    timestamp: '2026-01-01T00:00:00Z',
    tenant_id: 1,
    ...overrides,
  }
}

/** 造 total 行工具调用（同一轮内，名字带序号便于校验顺序）。 */
function makeRows(total: number): FixtureSpan[] {
  return Array.from({ length: total }, (_, i) =>
    makeSpan(`tool:tool_${i}`, { span_id: `row-${i}`, duration_ms: 5 }),
  )
}

async function settle(times = 4) {
  for (let i = 0; i < times; i++) await nextTick()
}

async function mountExpanded(spans: FixtureSpan[]) {
  const wrapper = mount(CallChainTimeline, { props: { visible: true, spans } })
  await wrapper.find('.timeline-header').trigger('click')
  await settle()
  return wrapper
}

const rows = (wrapper: ReturnType<typeof mount>) => wrapper.findAll('[data-testid="trace-row"]')
const rowNames = (wrapper: ReturnType<typeof mount>) =>
  rows(wrapper).map(r => r.attributes('data-span-name') ?? '')

/** jsdom 不做滚动几何：把三个度量直接钉在元素实例上。 */
function setScrollMetrics(el: Element, metrics: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: metrics.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: metrics.clientHeight })
  Object.defineProperty(el, 'scrollTop', { configurable: true, value: metrics.scrollTop, writable: true })
}

describe('CallChainTimeline 分页与窗口化（G12）', () => {
  it('行数超过一页时只渲染窗口内的行，并给出“加载更早”入口', async () => {
    // 修前：LONG 行全部挂载
    const wrapper = await mountExpanded(makeRows(LONG))

    expect(rows(wrapper).length).toBe(DEFAULT_PAGE_SIZE)
    expect(wrapper.find('[data-testid="trace-load-earlier"]').exists()).toBe(true)
    // 窗口是尾部：最先渲染的那一行是倒数第 DEFAULT_PAGE_SIZE 行
    expect(rowNames(wrapper)[0]).toBe(`tool:tool_${LONG - DEFAULT_PAGE_SIZE}`)
  })

  it('点击“加载更早”追加一页更早的行，顺序保持正确', async () => {
    // 修前：没有分页入口，也就无从"追加"
    const wrapper = await mountExpanded(makeRows(LONG))

    await wrapper.find('[data-testid="trace-load-earlier"]').trigger('click')
    await settle()
    expect(rows(wrapper).length).toBe(DEFAULT_PAGE_SIZE * 2)
    expect(rowNames(wrapper)[0]).toBe(`tool:tool_${LONG - DEFAULT_PAGE_SIZE * 2}`)

    await wrapper.find('[data-testid="trace-load-earlier"]').trigger('click')
    await settle()
    expect(rows(wrapper).length).toBe(LONG)
    expect(rowNames(wrapper)[0]).toBe('tool:tool_0')
    expect(wrapper.find('[data-testid="trace-load-earlier"]').exists()).toBe(false)
  })

  it('默认跟随尾部；用户向上滚动后停止自动跟随，回到底部再恢复', async () => {
    // 修前：既没有 follow 状态，也没有任何自动跟随
    const scrollIntoView = vi.fn()
    Object.defineProperty(window.HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    })

    const spans = makeRows(10)
    const wrapper = await mountExpanded(spans)
    const body = wrapper.find('[data-testid="trace-body"]').element

    // 默认在尾部：新增行会跟着滚
    expect(wrapper.find('[data-testid="trace-body"]').attributes('data-follow')).toBe('true')
    scrollIntoView.mockClear()
    await wrapper.setProps({ spans: [...spans, makeSpan('tool:stream_a', { span_id: 'a' })] })
    await settle()
    expect(scrollIntoView).toHaveBeenCalled()

    // 用户上滑：距离底部很远 → 停止跟随
    setScrollMetrics(body, { scrollTop: 100, scrollHeight: 1000, clientHeight: 300 })
    body.dispatchEvent(new Event('scroll'))
    await settle()
    expect(wrapper.find('[data-testid="trace-body"]').attributes('data-follow')).toBe('false')

    scrollIntoView.mockClear()
    const spans2 = [...spans, makeSpan('tool:stream_a', { span_id: 'a' })]
    await wrapper.setProps({ spans: [...spans2, makeSpan('tool:stream_b', { span_id: 'b' })] })
    await settle()
    expect(scrollIntoView).not.toHaveBeenCalled()

    // 回到底部 → 恢复跟随
    setScrollMetrics(body, { scrollTop: 700, scrollHeight: 1000, clientHeight: 300 })
    body.dispatchEvent(new Event('scroll'))
    await settle()
    expect(wrapper.find('[data-testid="trace-body"]').attributes('data-follow')).toBe('true')
  })

  it('进行中的 span 只显示“已开始”，不显示虚构耗时', async () => {
    // 修前：模板直接打印 `{{ span.duration_ms }}ms`，没有时长时渲染成 "ms"，有 0 时是 "0ms"
    const spans = [
      makeSpan('tool:read_file', { span_id: 'done', duration_ms: 42 }),
      makeSpan('tool:shell_exec', { span_id: 'running', duration_ms: null, status: 'running' }),
    ]
    const wrapper = await mountExpanded(spans)
    const all = rows(wrapper)

    expect(all[0].find('[data-testid="trace-duration"]').text()).toBe('42ms')
    expect(all[1].find('[data-testid="trace-duration"]').text()).toBe('已开始')
    expect(all[1].text()).not.toContain('ms')
  })
})
