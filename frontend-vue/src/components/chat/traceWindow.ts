/**
 * 轨迹窗格（CallChainTimeline）的纯逻辑：按轮折叠 + 尾部窗口 + 顶层时长求和。
 *
 * 三个问题各自独立，都是"数据层的算术"，所以抽成纯函数而不是塞进组件的 computed：
 *
 * 1) **轮次折叠**：一次会话的 trace 可能有几十轮（每轮"llm_call + 若干 tool:"）。
 *    修前组件把整份 trace 一次性 `v-for` 出来，40 轮任务要渲染上千行 DOM。
 *    这里按轮分组，交给组件只渲染"最近 N 轮"。
 *
 * 2) **尾部窗口**：即便只有一轮，单轮也可能有上百次工具调用。窗口只取尾部一页，
 *    "加载更早"再向前追加一页 —— 挂载行数始终与页数成正比，不随历史线性增长。
 *
 * 3) **顶层时长**：`duration_ms` 是**父 span 已包含子 span**的语义（`turn` 包含
 *    `llm_call`，`llm_call` 包含其下 `tool:`）。修前把每个 span 的时长直接相加，
 *    父子重复计入 —— 父 100ms / 子 60ms 的 fixture 会算出 160ms。这里只累加
 *    "没有父节点落在本份 trace 里"的 span。
 */

/** 组件用得到的最小 span 形状（后端 trace span 的子集，字段全可选以便兼容老数据）。 */
export interface TraceSpanLike {
  span_name: string
  duration_ms?: number | null
  span_id?: string
  parent_span_id?: string
  turn_id?: string
  turn_index?: number
  status?: string
}

/** 默认只展开最近多少轮。 */
export const DEFAULT_VISIBLE_TURNS = 3
/** 默认一页多少行（一次加载一页）。 */
export const DEFAULT_PAGE_SIZE = 50

export interface TurnGroup<T extends TraceSpanLike> {
  /** 组标识（同一次分组内唯一），用作渲染 key。 */
  key: string
  /** 该轮内的 span（保持原始顺序，父在子前）。 */
  spans: T[]
  /** 该轮顶层时长（父已含子，故不重复计）。 */
  durationMs: number
}

/** span 是否仍在进行中：没有时长，或状态仍是 running/started/pending。 */
export function isInProgressSpan(span: TraceSpanLike): boolean {
  const status = String(span.status ?? '').toLowerCase()
  if (status === 'running' || status === 'started' || status === 'pending' || status === 'in_progress') {
    return true
  }
  return span.duration_ms === null || span.duration_ms === undefined
}

/**
 * 把扁平 span 列表按轮切分。
 *
 * 轮边界来源（按优先级）：
 * - `turn_id`：后端按轮落库时最可靠；
 * - `span_name === 'turn'`：每轮有一个 `turn` 根 span（`app/agent/runtime.py` 里
 *   `span_name="turn"`），遇到它就开新组；
 * - `turn_index` 变化；
 * - 都没有 → 整份 trace 视为一轮（与修前的扁平渲染等价，只是有了外层容器）。
 */
export function groupSpansByTurn<T extends TraceSpanLike>(spans: readonly T[]): TurnGroup<T>[] {
  if (!spans.length) return []

  const groups: TurnGroup<T>[] = []
  const start = (key: string): TurnGroup<T> => {
    const group: TurnGroup<T> = { key, spans: [], durationMs: 0 }
    groups.push(group)
    return group
  }

  const hasTurnId = spans.some(s => typeof s.turn_id === 'string' && s.turn_id !== '')
  if (hasTurnId) {
    let current: TurnGroup<T> | null = null
    for (const span of spans) {
      const tid = typeof span.turn_id === 'string' ? span.turn_id : ''
      const key = tid ? `turn:${tid}` : ''
      // 没有 turn_id 的 span（例如轮外的一次性 span）挂到当前轮，不另开一轮
      if (!current || (key && current.key !== key)) {
        current = start(key || `turn:${groups.length}`)
      }
      current.spans.push(span)
    }
  } else {
    let current: TurnGroup<T> | null = null
    let prevIndex: number | undefined
    for (const span of spans) {
      const idx = typeof span.turn_index === 'number' && Number.isFinite(span.turn_index)
        ? span.turn_index
        : undefined
      const isTurnRoot = span.span_name === 'turn'
      const indexChanged = idx !== undefined && prevIndex !== undefined && idx !== prevIndex
      if (!current || isTurnRoot || indexChanged) {
        current = start(`turn:${groups.length}`)
      }
      if (idx !== undefined) prevIndex = idx
      current.spans.push(span)
    }
  }

  for (const group of groups) group.durationMs = sumTopLevelDurationMs(group.spans)
  return groups
}

export interface CappedTurns<T extends TraceSpanLike> {
  /** 保留的轮（最近的若干轮，顺序不变）。 */
  groups: TurnGroup<T>[]
  /** 被折叠掉的轮数。 */
  hiddenTurns: number
}

/** 只保留最近 `maxTurns` 轮；`maxTurns <= 0` 视为"不折叠"。 */
export function capToRecentTurns<T extends TraceSpanLike>(
  groups: readonly TurnGroup<T>[],
  maxTurns: number,
): CappedTurns<T> {
  const cap = Math.floor(maxTurns)
  if (!Number.isFinite(cap) || cap <= 0 || groups.length <= cap) {
    return { groups: [...groups], hiddenTurns: 0 }
  }
  const kept = groups.slice(groups.length - cap)
  return { groups: kept, hiddenTurns: groups.length - kept.length }
}

/**
 * 总时长：只累加**顶层** span。
 *
 * "顶层"= `parent_span_id` 为空，或指向的 span 不在本份数据里（跨 trace 的引用、
 * 后端截断都会出现后者，此时把它当顶层处理，宁可少算不可重复算）。
 *
 * 老数据可能整体缺 `span_id`（无法判定父子）——那只能退化成逐项求和，与修前一致：
 * 这是数据缺失的后果，不是这里能凭空补出来的信息。
 */
export function sumTopLevelDurationMs(spans: readonly TraceSpanLike[]): number {
  if (!spans.length) return 0

  const ids = new Set<string>()
  for (const span of spans) {
    if (typeof span.span_id === 'string' && span.span_id) ids.add(span.span_id)
  }
  const canResolveParent = ids.size > 0

  let total = 0
  for (const span of spans) {
    if (isInProgressSpan(span)) continue
    const parent = span.parent_span_id
    if (canResolveParent && typeof parent === 'string' && parent && ids.has(parent)) continue
    const ms = Number(span.duration_ms)
    if (!Number.isFinite(ms) || ms <= 0) continue
    total += ms
  }
  return Math.round(total)
}

export interface TailWindow<T> {
  /** 尾部窗口内的行。 */
  rows: T[]
  /** 窗口第一行在原始列表中的下标（加载更早后顺序校验用）。 */
  start: number
  /** 窗口之前还有多少行没加载。 */
  hiddenCount: number
  /** 是否还有更早的行可加载。 */
  hasMore: boolean
}

/** 尾部窗口：只取最后 `pageSize * pages` 行。 */
export function tailWindow<T>(rows: readonly T[], pageSize: number, pages: number): TailWindow<T> {
  const size = Math.floor(pageSize)
  const pageCount = Math.max(1, Math.floor(pages) || 1)
  if (!Number.isFinite(size) || size <= 0 || rows.length <= size * pageCount) {
    return { rows: [...rows], start: 0, hiddenCount: 0, hasMore: false }
  }
  const take = size * pageCount
  const start = rows.length - take
  return { rows: rows.slice(start), start, hiddenCount: start, hasMore: start > 0 }
}
