import { describe, it, expect } from 'vitest'
import { captureAnchor, resolveRestoreOffset, type RowRect } from '../transcriptAnchor'

/** 构造行：top/bottom 相对容器视口顶部。 */
function row(key: string, top: number, height = 100): RowRect {
  return { key, top, bottom: top + height }
}

describe('transcriptAnchor（prepend 锚点保持）', () => {
  it('捕获首个与视口相交的行，跳过已滚过视口上沿的行', () => {
    const rows = [row('a', -300), row('b', -50), row('c', 40), row('d', 200)]
    // 'b' 顶部在视口上方 50px，但仍有一半可见 —— 它才是首个相交行，锚点允许被截断
    expect(captureAnchor(rows)).toEqual({ key: 'b', delta: -50 })
  })

  it('视口内无行时返回 null', () => {
    expect(captureAnchor([row('a', -500), row('b', -200)])).toBeNull()
    expect(captureAnchor([])).toBeNull()
  })

  it('prepend 后把锚点行恢复到原偏移（当前视口仍在锚点上）', () => {
    const anchor = { key: 'c', delta: 40 }
    // prepend 了 300px 内容：锚点被推到 340px，视口未动
    const rows = [row('new1', -300), row('new2', -100), row('c', 340), row('d', 500)]
    expect(resolveRestoreOffset(anchor, rows, 800)).toBe(1100)
  })

  it('对锚点下方内容的异步变化免疫（高度差法会错，本方法正确）', () => {
    const anchor = { key: 'c', delta: 40 }
    // 下方行 'd' 长高了 800px（图片加载/markdown 增高），锚点行自身位置不变
    const rows = [row('new1', -300), row('c', 340, 100), { key: 'd', top: 440, bottom: 1640 }]
    // 视口位置只由锚点行决定 → 与"下方是否变化"无关
    expect(resolveRestoreOffset(anchor, rows, 800)).toBe(1100)
  })

  it('锚点行已不在渲染集合中时返回 null（保持原位，不猜）', () => {
    const anchor = { key: 'gone', delta: 40 }
    expect(resolveRestoreOffset(anchor, [row('a', 0)], 800)).toBeNull()
  })

  it('位置未变时不产生冗余写入', () => {
    const anchor = { key: 'c', delta: 40 }
    expect(resolveRestoreOffset(anchor, [row('c', 40)], 800)).toBeNull()
  })

  it('整体上移（视口已滑动）时同样按锚点还原', () => {
    const anchor = { key: 'c', delta: 40 }
    const rows = [row('c', -60), row('d', 100)]   // 锚点已在视口上方 60px
    expect(resolveRestoreOffset(anchor, rows, 800)).toBe(700)
  })
})
