import { describe, it, expect } from 'vitest'
import {
  DEFAULT_PAGE_SIZE,
  DEFAULT_VISIBLE_TURNS,
  capToRecentTurns,
  groupSpansByTurn,
  isInProgressSpan,
  sumTopLevelDurationMs,
  tailWindow,
} from '../traceWindow'

/**
 * 轨迹窗格纯逻辑（G10/G12 的算术部分）。
 *
 * 这些用例把"轮次折叠 / 尾部窗口 / 顶层时长"从组件里单独钉住：
 * 组件测试关注渲染结果，这里关注边界（空数据、缺父指针、页长非法）。
 */

interface Span {
  span_name: string
  duration_ms?: number | null
  span_id?: string
  parent_span_id?: string
  turn_id?: string
  turn_index?: number
  status?: string
}

describe('traceWindow（轮次折叠 / 尾部窗口 / 顶层时长）', () => {
  it('按 turn 根 span 切分轮次；每轮时长不含子 span', () => {
    // 修前：没有"轮"的概念，整份 trace 就是一条扁平列表
    const spans: Span[] = [
      { span_name: 'turn', span_id: 'r1', duration_ms: 100 },
      { span_name: 'tool:a', span_id: 'c1', parent_span_id: 'r1', duration_ms: 60 },
      { span_name: 'turn', span_id: 'r2', duration_ms: 50 },
      { span_name: 'tool:b', span_id: 'c2', parent_span_id: 'r2', duration_ms: 30 },
    ]
    const groups = groupSpansByTurn(spans)
    expect(groups.map(g => g.spans.length)).toEqual([2, 2])
    expect(groups.map(g => g.durationMs)).toEqual([100, 50])
  })

  it('有 turn_id 时按 turn_id 分组（一次会话的多轮 trace）', () => {
    const spans: Span[] = [
      { span_name: 'llm_call', turn_id: 'a', span_id: 'x', duration_ms: 10 },
      { span_name: 'tool:a', turn_id: 'a', span_id: 'y', parent_span_id: 'x', duration_ms: 5 },
      { span_name: 'llm_call', turn_id: 'b', span_id: 'z', duration_ms: 20 },
    ]
    const groups = groupSpansByTurn(spans)
    expect(groups.length).toBe(2)
    expect(groups[0].spans.map(s => s.span_id)).toEqual(['x', 'y'])
    expect(groups[1].spans.map(s => s.span_id)).toEqual(['z'])
  })

  it('没有轮标记时整份 trace 视为一轮（退化为修前的扁平顺序）', () => {
    const spans: Span[] = [{ span_name: 'tool:a' }, { span_name: 'tool:b' }]
    const groups = groupSpansByTurn(spans)
    expect(groups.length).toBe(1)
    expect(groups[0].spans.map(s => s.span_name)).toEqual(['tool:a', 'tool:b'])
  })

  it('只保留最近 N 轮，并报出被折叠的轮数', () => {
    const groups = Array.from({ length: 6 }, (_, i) => ({
      key: `turn:${i}`,
      spans: [{ span_name: 'turn' } as Span],
      durationMs: 1,
    }))
    const capped = capToRecentTurns(groups, DEFAULT_VISIBLE_TURNS)
    expect(capped.groups.map(g => g.key)).toEqual(['turn:3', 'turn:4', 'turn:5'])
    expect(capped.hiddenTurns).toBe(6 - DEFAULT_VISIBLE_TURNS)
    // 上限为 0 / 负数 / 轮数不足时不折叠（组件把 0 当"不限"）
    expect(capToRecentTurns(groups, 0).hiddenTurns).toBe(0)
    expect(capToRecentTurns(groups.slice(0, 2), DEFAULT_VISIBLE_TURNS).groups.length).toBe(2)
  })

  it('尾部窗口只取最后一页，翻页后向前追加且下标可校验', () => {
    const rows = Array.from({ length: DEFAULT_PAGE_SIZE * 3 + 5 }, (_, i) => i)
    const first = tailWindow(rows, DEFAULT_PAGE_SIZE, 1)
    expect(first.rows.length).toBe(DEFAULT_PAGE_SIZE)
    expect(first.start).toBe(rows.length - DEFAULT_PAGE_SIZE)
    expect(first.hasMore).toBe(true)

    const second = tailWindow(rows, DEFAULT_PAGE_SIZE, 2)
    expect(second.rows.length).toBe(DEFAULT_PAGE_SIZE * 2)
    expect(second.rows[0]).toBe(rows.length - DEFAULT_PAGE_SIZE * 2)
    expect(second.hasMore).toBe(true)

    const all = tailWindow(rows, DEFAULT_PAGE_SIZE, 99)
    expect(all.rows.length).toBe(rows.length)
    expect(all.hasMore).toBe(false)
  })

  it('非法页长 / 空列表不产生负数或越界切片', () => {
    expect(tailWindow([], DEFAULT_PAGE_SIZE, 1)).toEqual({ rows: [], start: 0, hiddenCount: 0, hasMore: false })
    const rows = [1, 2, 3]
    expect(tailWindow(rows, 0, 1).rows).toEqual(rows)
    expect(tailWindow(rows, DEFAULT_PAGE_SIZE, 0).rows).toEqual(rows)
  })

  it('顶层时长：父 100ms / 子 60ms 只算 100ms，进行中的 span 不计入', () => {
    // 修前：逐项 reduce → 160ms
    const spans: Span[] = [
      { span_name: 'turn', span_id: 'p', duration_ms: 100 },
      { span_name: 'tool:a', span_id: 'c', parent_span_id: 'p', duration_ms: 60 },
    ]
    expect(sumTopLevelDurationMs(spans)).toBe(100)
    expect(sumTopLevelDurationMs([...spans, { span_name: 'tool:b', duration_ms: null, status: 'running' }])).toBe(100)
    expect(sumTopLevelDurationMs([])).toBe(0)
  })

  it('父指针指向不存在的 span 时按顶层处理（宁少算不重复算）', () => {
    const spans: Span[] = [
      { span_name: 'tool:orphan', span_id: 'o', parent_span_id: 'missing', duration_ms: 40 },
    ]
    expect(sumTopLevelDurationMs(spans)).toBe(40)
  })

  it('进行中的判定：缺时长或状态为 running/started/pending', () => {
    expect(isInProgressSpan({ span_name: 'tool:a' })).toBe(true)
    expect(isInProgressSpan({ span_name: 'tool:a', duration_ms: null })).toBe(true)
    expect(isInProgressSpan({ span_name: 'tool:a', duration_ms: 0, status: 'running' })).toBe(true)
    expect(isInProgressSpan({ span_name: 'tool:a', duration_ms: 0, status: 'ok' })).toBe(false)
    expect(isInProgressSpan({ span_name: 'tool:a', duration_ms: 12 })).toBe(false)
  })
})
