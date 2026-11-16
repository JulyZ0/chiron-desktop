/**
 * prepend 锚点保持的纯几何计算（不触碰 DOM，便于确定性测试）。
 *
 * 与"高度差补偿"（scrollTop += scrollHeight 增量）的区别：
 * 高度差法把视口位置绑在**总高度**上，只要锚点下方的内容异步变化
 * （图片加载、markdown 增高、折叠展开），补偿值就错了 —— 表现为上滚加载后跳动。
 * 这里改为把视口位置绑在**锚点行自身**上：只依赖该行的位置，对其它行免疫。
 */

export interface RowRect {
  key: string
  /** 该行顶部相对滚动容器视口顶部的偏移（= rect.top - containerRect.top）。 */
  top: number
  /** 该行底部相对滚动容器视口顶部的偏移。 */
  bottom: number
}

export interface CapturedAnchor {
  key: string
  /** 锚点行顶部相对容器视口顶部的偏移；恢复时保持不变。 */
  delta: number
}

/**
 * 捕获首个与容器视口相交的行作为锚点。
 * 完全滚过视口上沿的行（bottom <= 0）会被跳过。
 */
export function captureAnchor(rows: RowRect[], tolerance = 1): CapturedAnchor | null {
  for (const r of rows) {
    if (r.bottom > tolerance) return { key: r.key, delta: r.top }
  }
  return null
}

/**
 * 计算把锚点行恢复到原偏移所需的新 scrollTop。
 * 返回 null 表示锚点行已不在当前渲染集合中（例如被移除），调用方应保持原位。
 */
export function resolveRestoreOffset(
  anchor: CapturedAnchor,
  rows: RowRect[],
  currentScrollTop: number,
): number | null {
  const row = rows.find(r => r.key === anchor.key)
  if (!row) return null
  const shift = row.top - anchor.delta
  if (Math.abs(shift) <= 1) return null   // 位置未变：不产生冗余写入
  return currentScrollTop + shift
}
