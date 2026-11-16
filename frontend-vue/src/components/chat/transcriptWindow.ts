/**
 * 窗口化的几何与区间计算（纯逻辑，无 DOM 访问）。
 *
 * 渲染区间是**两段的并集**，这是关键：
 * - 主窗口 [start, end)：覆盖视口 + 上下预渲染，随滚动移动。
 * - 常驻尾部 [tailStart, tailEnd)：活跃轮与最近若干轮，永远挂载，不随滚动卸载。
 *
 * 若把两段当成一个连续区间（`end = rowCount`），滚动到历史中段时会把中间所有行
 * 一并渲染 —— 窗口化形同失效；若只渲染主窗口，活跃轮又会在上滚时被卸载。
 *
 * 另两条不变量：
 * - **覆盖优先**：主窗口必须覆盖视口，否则调用方必须退回全量渲染（`coversViewport`）。
 * - **身份稳定**：区间用下标表达、由 key 集合重新计算，不就地改写已挂载的行。
 *
 * 行尺寸优先取实测值，未测量时用 kind 估计值——估计只影响窗口边界裕度，不影响正确性。
 */

export interface RowExtent {
  key: string
  size: number
}

export interface WindowGeometry {
  /** offsets[i] = 第 i 行顶部的 y；长度为 rows+1（末项即总高度）。 */
  offsets: number[]
  total: number
}

export interface WindowRange {
  /** 主窗口 [start, end)。 */
  start: number
  end: number
  /** 常驻尾部 [tailStart, tailEnd)，与主窗口不重叠；相等时表示没有尾部。 */
  tailStart: number
  tailEnd: number
}

export interface RenderSegment {
  start: number
  end: number
}

export interface WindowOptions {
  /** 视口上下各预渲染的像素量（滚动时避免先白后填）。 */
  overscanPx?: number
  /** 常驻尾部的**按行**兜底值（投影不可用时才用）。 */
  residentTail?: number
  /** 投影给出的尾部起点（按回合对齐）：常驻语义以回合为单位，给定时优先于 residentTail。 */
  residentTailStart?: number
  /** 常驻行数上限：单个巨型回合不得把常驻预算撑爆（超出的部分正常参与卸载）。 */
  residentMaxRows?: number
}

/** 未测量行的尺寸估计：按 kind 给不同默认值，避免初值偏差过大。 */
export function estimateRowSize(kind: string): number {
  switch (kind) {
    case 'date_divider':
      return 36
    case 'turn_stats':
      return 28
    // 折叠头是 28px 行 + 上下 2px 行距，估计值略大以免窗口边界低估
    case 'turn_header':
    case 'tool_group_header':
      return 32
    case 'tool_call':
      return 56
    case 'tool_result':
      return 72
    case 'reasoning':
      return 80
    default:
      return 96
  }
}

/** 用实测尺寸覆盖估计值，得到几何输入。 */
export function applyMeasurements(
  rows: readonly { key: string; kind: string }[],
  sizes: ReadonlyMap<string, number>,
): RowExtent[] {
  return rows.map(r => {
    const measured = sizes.get(r.key)
    return { key: r.key, size: measured ?? estimateRowSize(r.kind) }
  })
}

export function buildGeometry(rows: readonly RowExtent[]): WindowGeometry {
  const offsets = new Array<number>(rows.length + 1)
  let y = 0
  for (let i = 0; i < rows.length; i++) {
    offsets[i] = y
    y += Math.max(0, rows[i].size)
  }
  offsets[rows.length] = y
  return { offsets, total: y }
}

/** 首个底部越过 y 的行（即视口上沿所在的行）。 */
function firstRowBelow(geo: WindowGeometry, rowCount: number, y: number): number {
  let lo = 0
  let hi = rowCount
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (geo.offsets[mid + 1] > y) hi = mid
    else lo = mid + 1
  }
  return lo
}

/** 首个顶部不早于 y 的行（即视口下沿之后的第一行）。 */
function firstRowAtOrBelow(geo: WindowGeometry, rowCount: number, y: number): number {
  let lo = 0
  let hi = rowCount
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (geo.offsets[mid] >= y) hi = mid
    else lo = mid + 1
  }
  return lo
}

export function computeWindowRange(
  geo: WindowGeometry,
  rowCount: number,
  scrollTop: number,
  viewportHeight: number,
  opts: WindowOptions = {},
): WindowRange {
  if (rowCount <= 0) return { start: 0, end: 0, tailStart: 0, tailEnd: 0 }

  const overscan = Math.max(0, opts.overscanPx ?? 400)
  // 尾部起点 = max(投影给的回合起点 / 按行兜底, 按行上限截断点)，再夹到 [0, rowCount]。
  // 上限防止单个巨型回合把常驻预算撑爆（常驻行不参与卸载）。
  const byRows = rowCount - Math.max(0, Math.min(opts.residentTail ?? 0, rowCount))
  const byCap = opts.residentMaxRows ? rowCount - Math.max(0, opts.residentMaxRows) : 0
  const tailStart = Math.max(0, Math.min(rowCount, Math.max(opts.residentTailStart ?? byRows, byCap)))

  const from = Math.max(0, scrollTop - overscan)
  const to = scrollTop + Math.max(0, viewportHeight) + overscan

  const start = firstRowBelow(geo, rowCount, from)
  let end = firstRowAtOrBelow(geo, rowCount, to)
  if (end <= start) end = Math.min(rowCount, start + 1)

  // 常驻尾部独立于主窗口（二者可能重叠），重叠与相邻由 toSegments 负责合并
  const tailEnd = rowCount

  return { start, end, tailStart, tailEnd }
}

/**
 * 展开为可渲染的连续段落（重叠/相邻则合并）。主窗口与常驻尾部是两段独立区间：
 * 合并必须按起点排序后再做，否则"尾部起点早于主窗口起点"的场景会丢掉尾部前半段。
 */
export function toSegments(range: WindowRange): RenderSegment[] {
  const raw = [
    { start: range.start, end: range.end },
    { start: range.tailStart, end: range.tailEnd },
  ]
    .filter(s => s.end > s.start)
    .sort((a, b) => a.start - b.start)

  const segs: RenderSegment[] = []
  for (const s of raw) {
    const last = segs[segs.length - 1]
    if (last && s.start <= last.end) last.end = Math.max(last.end, s.end)
    else segs.push({ ...s })
  }
  return segs
}

/** 本次实际挂载的行数（诊断与断言用）。 */
export function rangeRowCount(range: WindowRange): number {
  return (range.end - range.start) + Math.max(0, range.tailEnd - range.tailStart)
}

/**
 * 主窗口是否覆盖视口。返回 false 时调用方**必须**退回全量渲染（fail-closed），
 * 不允许带着未覆盖的窗口去绘制。常驻尾部不参与该判定（它是额外保障，不是覆盖来源）。
 */
export function coversViewport(
  geo: WindowGeometry,
  range: WindowRange,
  scrollTop: number,
  viewportHeight: number,
): boolean {
  // 空主窗口没有内容需要覆盖：不应据此触发 fail-closed 降级
  if (range.end <= range.start) return true
  const top = geo.offsets[range.start] ?? 0
  const bottom = geo.offsets[range.end] ?? geo.total
  return top <= scrollTop + 1 && bottom >= scrollTop + Math.max(0, viewportHeight) - 1
}

/** 区间内应有的行 key（供渲染层做 key 校验，不依赖下标）。 */
export function keysInRange(keys: readonly string[], range: WindowRange): string[] {
  const out: string[] = []
  for (const seg of toSegments(range)) out.push(...keys.slice(seg.start, seg.end))
  return out
}
