import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CallChainTimeline from '../CallChainTimeline.vue'
import { DEFAULT_VISIBLE_TURNS } from '../chat/traceWindow'

/**
 * G10：调用链上限与折叠。
 *
 * 修前：整份 trace 一次性 `v-for` 渲染（无上限、无折叠），40 轮任务要渲染上千行；
 * 且 `totalDurationMs` 把父子 span 时长直接相加 —— 父 span 已包含子 span，导致重复计数。
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

/** 造一个 span；span_id 默认按调用序生成，需要父子关系时显式传入。 */
let seq = 0
function makeSpan(name: string, overrides: Partial<FixtureSpan> = {}): FixtureSpan {
  seq += 1
  return {
    trace_id: 'trace-caps',
    span_name: name,
    duration_ms: 10,
    timestamp: '2026-01-01T00:00:00Z',
    tenant_id: 1,
    span_id: `s${seq}`,
    ...overrides,
  }
}

/** 一轮 = 一个 `turn` 根 span + 一次工具调用（子 span 挂在根下）。 */
function makeTurn(index: number): FixtureSpan[] {
  const rootId = `turn-${index}-root`
  return [
    makeSpan('turn', { span_id: rootId, duration_ms: 100 }),
    makeSpan(`tool:read_file`, { span_id: `turn-${index}-tool`, parent_span_id: rootId, duration_ms: 60 }),
  ]
}

function makeTurns(turns: number): FixtureSpan[] {
  return Array.from({ length: turns }, (_, i) => makeTurn(i)).flat()
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

describe('CallChainTimeline 轮次上限与折叠（G10）', () => {
  it('轮数超过上限时只渲染最近 N 轮，并给出折叠提示', async () => {
    // 修前：5 轮共 10 行全部渲染，一个上限提示都没有
    const turns = DEFAULT_VISIBLE_TURNS + 2
    const wrapper = await mountExpanded(makeTurns(turns))

    expect(rows(wrapper).length).toBe(DEFAULT_VISIBLE_TURNS * 2)
    expect(wrapper.find('[data-testid="trace-cap-notice"]').exists()).toBe(true)
    expect(wrapper.text()).toContain(`只显示最近 ${DEFAULT_VISIBLE_TURNS} 轮`)
  })

  it('点击“展开全部”后能看到全部轮次，且能再折回最近 N 轮', async () => {
    // 修前：没有"展开"入口 —— 要么全渲染，要么什么都没有
    const turns = DEFAULT_VISIBLE_TURNS + 2
    const wrapper = await mountExpanded(makeTurns(turns))

    await wrapper.find('[data-testid="trace-show-all"]').trigger('click')
    await settle()
    expect(rows(wrapper).length).toBe(turns * 2)
    expect(wrapper.find('[data-testid="trace-cap-notice"]').exists()).toBe(false)

    await wrapper.find('[data-testid="trace-collapse-recent"]').trigger('click')
    await settle()
    expect(rows(wrapper).length).toBe(DEFAULT_VISIBLE_TURNS * 2)
  })

  it('总时长不重复计入父子 span（父 100ms / 子 60ms → 100ms）', async () => {
    // 修前：reduce 逐项求和 → 160ms（父已含子，多算了子）
    const spans = [
      makeSpan('turn', { span_id: 'p', duration_ms: 100 }),
      makeSpan('tool:read_file', { span_id: 'c', parent_span_id: 'p', duration_ms: 60 }),
    ]
    const wrapper = await mountExpanded(spans)

    const header = wrapper.find('.timeline-header').text()
    expect(header).toContain('100ms')
    expect(header).not.toContain('160ms')
    expect(wrapper.text()).toContain('推理完成 (总耗时 100ms)')
  })

  it('多个顶层 span 才相加（两轮各 100ms → 200ms）', async () => {
    const spans = [
      makeSpan('turn', { span_id: 'r1', duration_ms: 100 }),
      makeSpan('tool:a', { span_id: 'c1', parent_span_id: 'r1', duration_ms: 60 }),
      makeSpan('turn', { span_id: 'r2', duration_ms: 100 }),
      makeSpan('tool:b', { span_id: 'c2', parent_span_id: 'r2', duration_ms: 70 }),
    ]
    const wrapper = await mountExpanded(spans)
    expect(wrapper.find('.timeline-header').text()).toContain('200ms')
  })

  it('空 trace 不崩：无行、给空态提示、总时长 0', async () => {
    // 修前：空数组同样能渲染（这条是回归护栏，防止折叠/窗口化引入空列表崩溃）
    const wrapper = await mountExpanded([])
    expect(rows(wrapper).length).toBe(0)
    expect(wrapper.find('[data-testid="trace-empty"]').exists()).toBe(true)
    expect(wrapper.find('.timeline-header').text()).toContain('0ms')
  })
})
