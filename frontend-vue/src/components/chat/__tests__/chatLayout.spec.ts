import { describe, expect, it } from 'vitest'
import {
  CHAT_AUX_MAX_RATIO,
  CHAT_MAIN_MIN_WIDTH,
  computeDockLayout,
} from '../chatLayout'

describe('computeDockLayout', () => {
  it('两块辅助栏同时打开时仍保留主区最小宽度，且总宽不超预算', () => {
    const layout = computeDockLayout(1440, [
      { id: 'workbench', side: 'left', minWidth: 300, preferredWidth: 380, priority: 1 },
      { id: 'reference', side: 'right', minWidth: 260, preferredWidth: 420, priority: 2 },
    ])

    expect(layout.main).toBeGreaterThanOrEqual(CHAT_MAIN_MIN_WIDTH)
    const total = Object.values(layout.widths).reduce((n, w) => n + w, 0)
    expect(total).toBeLessThanOrEqual(Math.floor(1440 * CHAT_AUX_MAX_RATIO))
    expect(layout.collapsed).toEqual([])
  })

  it('空间不足时按优先级折叠低优先级面板', () => {
    const layout = computeDockLayout(1100, [
      { id: 'workbench', side: 'left', minWidth: 360, preferredWidth: 420, priority: 1 },
      { id: 'reference', side: 'right', minWidth: 360, preferredWidth: 420, priority: 2 },
      { id: 'subagent', side: 'right', minWidth: 360, preferredWidth: 420, priority: 3 },
    ])

    expect(layout.main).toBeGreaterThanOrEqual(CHAT_MAIN_MIN_WIDTH)
    expect(layout.collapsed).toContain('subagent')
    expect(layout.widths.workbench).toBeGreaterThanOrEqual(360)
  })

  it('极窄容器不返回负主区', () => {
    const layout = computeDockLayout(700, [
      { id: 'workbench', side: 'left', minWidth: 300, preferredWidth: 300 },
    ])
    expect(layout.main).toBeGreaterThanOrEqual(0)
    expect(layout.collapsed).toContain('workbench')
  })
})
