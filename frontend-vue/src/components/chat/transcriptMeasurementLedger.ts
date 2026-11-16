/**
 * 测量台账：DOM 实测尺寸先进暂存区，再一次性发布为不可变快照。
 *
 * 为什么不能边测边改：前缀偏移（`transcriptWindow.buildGeometry` 的输入）是所有
 * 窗口与锚点计算的共同输入，逐条写入会让同一帧内的不同计算读到"半更新"的几何
 * （参照项目称之为测量代际混用），表现为滚动中偶发跳动或空白。
 * 因此：`stage()` 只暂存，`publish()` 原子提交；无实际变更时返回 null，
 * 让调用方跳过那次重渲染。
 */

export interface Ledger {
  /** 暂存一次 DOM 实测；非法值（空 key / 非有限 / 负数）被忽略。 */
  stage(key: string, size: number): void
  /** 原子发布暂存批次；无实际变更时返回 null。返回的新 Map 可直接作为渲染输入。 */
  publish(): ReadonlyMap<string, number> | null
  /** 当前已发布尺寸的只读视图。 */
  sizes(): ReadonlyMap<string, number>
  committedCount(): number
  pendingCount(): number
  /** 丢弃暂存批次（整批测量作废 / 切换会话时使用）。 */
  discard(): void
}

export function createLedger(): Ledger {
  const committed = new Map<string, number>()
  let staged: Map<string, number> | null = null

  return {
    stage(key, size) {
      if (!key || !Number.isFinite(size) || size < 0) return
      if (!staged) staged = new Map()
      staged.set(key, size)
    },

    publish() {
      const batch = staged
      staged = null
      if (!batch || batch.size === 0) return null

      // 与本批实际不同的条目才算变更；DOM 抖动造成的重复上报不应触发重渲染
      let changed = false
      for (const [k, v] of batch) {
        if (committed.get(k) !== v) { changed = true; break }
      }
      if (!changed) return null

      const next = new Map(committed)
      for (const [k, v] of batch) next.set(k, v)
      committed.clear()
      for (const [k, v] of next) committed.set(k, v)
      // 返回独立快照：调用方拿到的是不可变的一代几何，后续发布不会改动它
      return new Map(next)
    },

    sizes() {
      return committed
    },

    committedCount() {
      return committed.size
    },

    pendingCount() {
      return staged ? staged.size : 0
    },

    discard() {
      staged = null
    },
  }
}
