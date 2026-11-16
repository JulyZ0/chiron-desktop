import { describe, it, expect } from 'vitest'
import {
  applyMeasurements,
  buildGeometry,
  computeWindowRange,
  coversViewport,
  estimateRowSize,
  keysInRange,
  rangeRowCount,
  toSegments,
  type RowExtent,
} from '../transcriptWindow'

/** 造等高的行，便于手算。 */
function rows(n: number, size = 100): RowExtent[] {
  return Array.from({ length: n }, (_, i) => ({ key: `r${i}`, size }))
}

describe('transcriptWindow（窗口化几何与区间）', () => {
  it('按 kind 给不同的未测量估计值', () => {
    expect(estimateRowSize('date_divider')).toBe(36)
    expect(estimateRowSize('turn_stats')).toBe(28)
    expect(estimateRowSize('tool_result')).toBe(72)
    expect(estimateRowSize('text')).toBe(96)
    expect(estimateRowSize('未知kind')).toBe(96)
  })

  it('实测尺寸优先于估计值', () => {
    const rows = [{ key: 'a', kind: 'text' }, { key: 'b', kind: 'text' }]
    const sizes = new Map([['a', 300]])
    expect(applyMeasurements(rows, sizes)).toEqual([
      { key: 'a', size: 300 },
      { key: 'b', size: 96 },
    ])
  })

  it('前缀偏移与总高度', () => {
    const geo = buildGeometry(rows(3, 100))
    expect(geo.offsets).toEqual([0, 100, 200, 300])
    expect(geo.total).toBe(300)
  })

  it('主窗口覆盖当前视口（含上下预渲染），无尾部时尾部为空', () => {
    const geo = buildGeometry(rows(20, 100))   // 总高 2000
    const range = computeWindowRange(geo, 20, 900, 400, { overscanPx: 200 })
    // from=700 → start=7；to=1500 → end=15；residentTail=0 → 空尾
    expect(range).toEqual({ start: 7, end: 15, tailStart: 20, tailEnd: 20 })
    expect(rangeRowCount(range)).toBe(8)
    expect(coversViewport(geo, range, 900, 400)).toBe(true)
  })

  it('常驻尾部与主窗口是两段：上滚到顶部时尾部仍挂载，且不牵连中间行', () => {
    const geo = buildGeometry(rows(300, 100))   // 总高 30000
    const range = computeWindowRange(geo, 300, 0, 400, { overscanPx: 800, residentTail: 20 })
    expect(range.start).toBe(0)
    expect(range.end).toBe(12)                  // 视口 + 预渲染
    expect(range.tailStart).toBe(280)
    expect(range.tailEnd).toBe(300)
    // 关键：只挂载 12 + 20 行，而不是把中间 268 行一起渲染
    expect(rangeRowCount(range)).toBe(32)
    expect(toSegments(range)).toEqual([{ start: 0, end: 12 }, { start: 280, end: 300 }])
  })

  it('主窗口与尾部相邻时合并为一段（避免多余 spacer）', () => {
    const geo = buildGeometry(rows(20, 100))
    const range = computeWindowRange(geo, 20, 1400, 400, { overscanPx: 0, residentTail: 5 })
    // 尾部是「最后 5 行」= [15, 20)，与主窗口 [14, 18) 相邻 → 合并为一段
    expect(range).toEqual({ start: 14, end: 18, tailStart: 15, tailEnd: 20 })
    expect(toSegments(range)).toEqual([{ start: 14, end: 20 }])
  })

  it('投影给出的按回合尾部起点优先于按行兜底', () => {
    const geo = buildGeometry(rows(20, 100))
    const range = computeWindowRange(geo, 20, 1400, 400, {
      overscanPx: 0,
      residentTail: 5,
      residentTailStart: 10,
    })
    expect(range.tailStart).toBe(10)
    expect(range.tailEnd).toBe(20)
  })

  it('常驻行数上限截断单个巨型回合', () => {
    const geo = buildGeometry(rows(300, 100))
    const range = computeWindowRange(geo, 300, 0, 400, {
      overscanPx: 0,
      residentTailStart: 0,
      residentMaxRows: 120,
    })
    expect(range.tailStart).toBe(180)
    expect(rangeRowCount(range)).toBeLessThanOrEqual(132)
  })

  it('尾部起点不会被主窗口越过（长尾部场景）', () => {
    const geo = buildGeometry(rows(10, 100))
    const range = computeWindowRange(geo, 10, 900, 100, { overscanPx: 0, residentTail: 9 })
    expect(range.tailStart).toBe(1)
    expect(range.tailEnd).toBe(10)
    expect(toSegments(range)).toEqual([{ start: 1, end: 10 }])
  })

  it('主窗口未覆盖视口时被发现（fail-closed 的依据）', () => {
    const geo = buildGeometry(rows(20, 100))
    const tooSmall = { start: 0, end: 3, tailStart: 20, tailEnd: 20 }
    expect(coversViewport(geo, tooSmall, 900, 400)).toBe(false)
  })

  it('空列表与越界滚动不崩溃', () => {
    const geo = buildGeometry([])
    expect(computeWindowRange(geo, 0, 0, 400)).toEqual({ start: 0, end: 0, tailStart: 0, tailEnd: 0 })
    expect(coversViewport(geo, { start: 0, end: 0, tailStart: 0, tailEnd: 0 }, 0, 400)).toBe(true)
    expect(toSegments({ start: 0, end: 0, tailStart: 0, tailEnd: 0 })).toEqual([])

    const g2 = buildGeometry(rows(3, 100))
    const far = computeWindowRange(g2, 3, 99999, 400, { overscanPx: 0 })
    expect(far.end).toBeLessThanOrEqual(3)
    expect(far.start).toBeLessThanOrEqual(3)
  })

  it('区间到行 key 的映射跨两段（渲染层按 key 校验身份）', () => {
    const keys = ['a', 'b', 'c', 'd', 'e']
    const range = { start: 0, end: 2, tailStart: 4, tailEnd: 5 }
    expect(keysInRange(keys, range)).toEqual(['a', 'b', 'e'])
  })
})
