import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SubagentRunCard from '../SubagentRunCard.vue'
import type { SubagentRunView } from '../../../api/subagent'

/**
 * 失败 run 的摘要可见性（2026-11 子 Agent 全 failed 回归）。
 *
 * 现场：卡片显示 `↑0 · ↓0` + "（还没有摘要，点开看事件流）"，而真实原因
 * （`400 … isn't included in OpenCode Go`）只在 DB 的 `error` 列里 —— 卡片不读它。
 * 引擎侧已把原因写进 `summary`（`app/subagent/reporting.py::failure_summary`）；
 * 这里钉住前端的两条兜底：
 *   1. 有 summary 时直接显示（不再显示"还没有摘要"）；
 *   2. 历史/缓存行只有 `error` 时，failed 卡片回落到 error。
 */
const EMPTY_SUMMARY = '（还没有摘要，点开看事件流）'

function makeRun(overrides: Partial<SubagentRunView> = {}): SubagentRunView {
  return {
    run_id: 'rs_1',
    depth: 1,
    status: 'failed',
    ...overrides,
  }
}

function mountCard(run: SubagentRunView) {
  return mount(SubagentRunCard, { props: { run } })
}

describe('SubagentRunCard 的失败可见性', () => {
  it('failed + summary：直接显示上游原因，不显示"还没有摘要"', () => {
    const wrapper = mountCard(
      makeRun({ summary: "失败原因: 400 claude-sonnet-4-20250514 isn't included in OpenCode Go" }),
    )
    const text = wrapper.text()
    expect(text).toContain("isn't included in OpenCode Go")
    expect(text).not.toContain(EMPTY_SUMMARY)
  })

  it('failed + 只有 error（历史行/缓存）：回落到 error，而不是空摘要', () => {
    const wrapper = mountCard(
      makeRun({ summary: '', error: 'no_usable_key provider=opencode-go-anthropic cooling=1' }),
    )
    const text = wrapper.text()
    expect(text).toContain('no_usable_key')
    expect(text).not.toContain(EMPTY_SUMMARY)
  })

  it('running 且无摘要：保留占位文案（这不是失败，没必要编一个摘要）', () => {
    const wrapper = mountCard(makeRun({ status: 'running', summary: '' }))
    expect(wrapper.text()).toContain(EMPTY_SUMMARY)
  })
})
