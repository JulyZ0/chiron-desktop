import { beforeEach, describe, expect, it, vi } from 'vitest'

/** F10：全局快捷键信号的跨页面桥（挂载前置位、挂载后消费）。 */
describe('quickAsk 信号桥', () => {
  beforeEach(async () => {
    const mod = await import('../quickAsk')
    mod.consumeQuickAsk() // 清掉上一个用例的 pending
  })

  it('挂载前到达的信号会被消费一次', async () => {
    const { requestQuickAsk, consumeQuickAsk } = await import('../quickAsk')
    requestQuickAsk()
    expect(consumeQuickAsk()).toBe(true)
    expect(consumeQuickAsk()).toBe(false)
  })

  it('已挂载时监听器立即收到，并清掉 pending（不会二次聚焦）', async () => {
    const { requestQuickAsk, consumeQuickAsk, onQuickAsk } = await import('../quickAsk')
    const cb = vi.fn()
    const off = onQuickAsk(cb)
    requestQuickAsk()
    expect(cb).toHaveBeenCalledTimes(1)
    expect(consumeQuickAsk()).toBe(false)
    off()
    requestQuickAsk()
    expect(cb).toHaveBeenCalledTimes(1)
  })
})
