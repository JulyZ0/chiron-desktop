import { describe, it, expect } from 'vitest'
import { createTranscriptViewport, type ViewportHost } from '../transcriptViewport'

/** 可观测写入次数的假滚动容器（用于断言"不产生冗余物理写入"）。 */
function makeHost(scrollTop: number, scrollHeight: number, clientHeight: number) {
  const host = { scrollHeight, clientHeight, writes: 0 } as ViewportHost & { writes: number }
  let st = scrollTop
  Object.defineProperty(host, 'scrollTop', {
    get: () => st,
    set: (v: number) => {
      host.writes++
      st = v
    },
    configurable: true,
  })
  return host
}

describe('transcriptViewport（滚动单写者契约）', () => {
  it('程序写入被原生 scroll 消费后，不夺取用户租约', () => {
    const clock = 0
    const vp = createTranscriptViewport({ now: () => clock, leaseMs: 1000 })
    const el = makeHost(0, 5000, 800)

    expect(vp.follow(el)).toBe(true)      // 写入可达底部偏移 scrollHeight - clientHeight
    expect(el.scrollTop).toBe(4200)
    vp.handleScroll(el)                   // 原生 scroll 事件确认了这次程序写入
    expect(vp.isUserOwned()).toBe(false)  // 程序滚动不应被误判为用户输入
  })

  it('用户滚动期间拒绝程序跟随（不抢阅读位置）', () => {
    const clock = 0
    const vp = createTranscriptViewport({ now: () => clock, leaseMs: 1000 })
    const el = makeHost(1200, 5000, 800)

    vp.noteUserInput()                    // 用户 wheel
    expect(vp.isUserOwned()).toBe(true)
    expect(vp.follow(el)).toBe(false)     // 流式追加时的自动跟随被拒绝
    expect(el.scrollTop).toBe(1200)       // 位置未被改动
    expect(el.writes).toBe(0)
  })

  it('落点与程序写入不符 → 判定为用户滚动并立刻夺取租约', () => {
    const clock = 0
    const vp = createTranscriptViewport({ now: () => clock, leaseMs: 1000 })
    const el = makeHost(0, 5000, 800)

    vp.follow(el)                         // pending = 4200（可达底部偏移）
    el.scrollTop = 2000                   // 用户抢在 scroll 事件前滚走
    vp.handleScroll(el)
    expect(vp.isUserOwned()).toBe(true)
    expect(vp.follow(el)).toBe(false)     // 随后的自动跟随必须让位
  })

  it('租约到期后恢复自动跟随', () => {
    let clock = 0
    const vp = createTranscriptViewport({ now: () => clock, leaseMs: 1000 })
    const el = makeHost(1000, 6000, 800)

    vp.noteUserInput()
    expect(vp.isUserOwned()).toBe(true)
    clock = 1500                          // 租约过期
    expect(vp.isUserOwned()).toBe(false)
    expect(vp.follow(el)).toBe(true)
    expect(el.scrollTop).toBe(5200)
  })

  it('已贴底时不产生冗余物理写入', () => {
    const vp = createTranscriptViewport({ now: () => 0 })
    const el = makeHost(4200, 5000, 800)  // 恰好贴底

    expect(vp.isAtBottom(el)).toBe(true)
    expect(vp.follow(el)).toBe(false)
    expect(el.writes).toBe(0)
  })

  it('锚点恢复优先于租约（分页 prepend 保持视口）', () => {
    const clock = 0
    const vp = createTranscriptViewport({ now: () => clock, leaseMs: 1000 })
    const el = makeHost(3000, 6000, 800)

    vp.noteUserInput()                    // 用户正在阅读
    vp.restore(el, 3500)                  // prepend 后恢复锚点
    expect(el.scrollTop).toBe(3500)       // 恢复不被租约拒绝
  })

  it('触顶跳转让位后登记 pending（不会被误判为用户滚动）', () => {
    const vp = createTranscriptViewport({ now: () => 0 })
    const el = makeHost(2000, 6000, 800)

    vp.toTop(el)
    expect(el.scrollTop).toBe(0)
    vp.handleScroll(el)                   // 原生确认
    expect(vp.isUserOwned()).toBe(false)
  })

  it('贴底阈值判定', () => {
    const vp = createTranscriptViewport({ bottomThreshold: 4 })
    expect(vp.isAtBottom(makeHost(4197, 5000, 800))).toBe(true)   // 差 3px
    expect(vp.isAtBottom(makeHost(4100, 5000, 800))).toBe(false)  // 差 100px
  })

  it('折叠等结构性写入被浏览器钳制时仍归因于程序（不误判为用户滚动）', () => {
    const bottom = 1200 - 800   // 折叠后总高变小，可达底部偏移只有 400
    let st = 1000
    const el = {
      scrollHeight: 1200,
      clientHeight: 800,
      get scrollTop() { return st },
      set scrollTop(v: number) { st = Math.max(0, Math.min(v, bottom)) },   // 浏览器式钳制
    }
    const vp = createTranscriptViewport({ now: () => 0, leaseMs: 1000 })

    vp.afterStructuralChange(el, 900)
    expect(el.scrollTop).toBe(400)        // 请求值被钳到新底部
    vp.handleScroll(el)                   // 原生 scroll 带实际落点
    expect(vp.isUserOwned()).toBe(false)  // 仍归因于程序写入，不阻断折叠后的自动跟随
  })
})
