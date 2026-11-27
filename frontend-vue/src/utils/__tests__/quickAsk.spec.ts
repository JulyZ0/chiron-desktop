import { beforeEach, describe, expect, it, vi } from 'vitest'

/** F10：全局快捷键信号的跨页面桥（挂载前置位、挂载后消费）。 */
describe('quickAsk 信号桥', () => {
  beforeEach(async () => {
    const mod = await import('../quickAsk')
    mod.consumeQuickAsk() // 清掉上一个用例的 pending
    mod.consumeQuickAskText()
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

  it('迷你输入框文本：挂载前到达的只消费一次', async () => {
    const { submitQuickAskText, consumeQuickAskText } = await import('../quickAsk')
    submitQuickAskText('帮我总结一下这个仓库')
    expect(consumeQuickAskText()).toBe('帮我总结一下这个仓库')
    expect(consumeQuickAskText()).toBe('')
  })

  it('迷你输入框文本：已挂载时监听器立即收到，且不会重复发送', async () => {
    const { submitQuickAskText, consumeQuickAskText, onQuickAskText } = await import('../quickAsk')
    const cb = vi.fn()
    const off = onQuickAskText(cb)
    submitQuickAskText('解释这段报错')
    expect(cb).toHaveBeenCalledTimes(1)
    expect(cb).toHaveBeenCalledWith('解释这段报错')
    // 监听器消费后 pending 必须清空，否则 ChatView 重新挂载会再发一次
    expect(consumeQuickAskText()).toBe('')
    off()
    submitQuickAskText('第二次')
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('空文本不触发信号', async () => {
    const { submitQuickAskText, consumeQuickAskText, onQuickAskText } = await import('../quickAsk')
    const cb = vi.fn()
    const off = onQuickAskText(cb)
    submitQuickAskText('   ')
    expect(cb).not.toHaveBeenCalled()
    off()
  })
})
