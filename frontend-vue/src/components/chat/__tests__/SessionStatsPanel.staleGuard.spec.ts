import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

// G5：SessionStatsPanel 的代次保护 —— 快速切会话时旧响应不得覆盖新数据。
// 修前：`watch(sessionId) → load()` 没有代次判断，先发后到的响应用**另一个会话**的
// token/费用覆盖面板（这两个数字恰恰最容易被当成"精确账"相信）。

const getSessionMetrics = vi.fn()

vi.mock('../../../api/sessionRuntime', () => ({
  getSessionMetrics: (...args: unknown[]) => getSessionMetrics(...args),
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

import SessionStatsPanel from '../SessionStatsPanel.vue'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  // 未处理的 rejection 会让 vitest 直接报错，这里先挂一个空处理器
  promise.catch(() => {})
  return { promise, resolve, reject }
}

const metricsOf = (turns: number) => ({
  totals: { turns, input_tokens: turns * 10, output_tokens: turns, cached_tokens: 0, cost_cents: 0 },
  throughput: undefined,
  source: 'runtime',
})

describe('SessionStatsPanel 代次保护', () => {
  beforeEach(() => {
    getSessionMetrics.mockReset()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('慢的旧请求不会覆盖新会话的数据', async () => {
    const first = deferred<unknown>()
    const second = deferred<unknown>()

    getSessionMetrics.mockImplementationOnce(() => first.promise)
    getSessionMetrics.mockImplementationOnce(() => second.promise)

    const wrapper = mount(SessionStatsPanel, { props: { sessionId: 's1' } })
    await nextTick()

    await wrapper.setProps({ sessionId: 's2' })
    await nextTick()

    // 新会话先返回
    second.resolve(metricsOf(2))
    await nextTick()
    await nextTick()

    // 旧会话后返回：必须被丢弃
    first.resolve(metricsOf(1))
    await nextTick()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('2') // 新会话的 turns
    expect(wrapper.text()).not.toContain('1,000') // 旧会话的 input_tokens 量级
  })

  it('切到空会话会作废在飞请求', async () => {
    const first = deferred<unknown>()
    getSessionMetrics.mockImplementationOnce(() => first.promise)

    const wrapper = mount(SessionStatsPanel, { props: { sessionId: 's1' } })
    await nextTick()

    await wrapper.setProps({ sessionId: '' })
    await nextTick()

    first.resolve(metricsOf(9))
    await nextTick()
    await nextTick()

    // 没有会话时不该出现任何统计数字
    expect(getSessionMetrics).toHaveBeenCalledTimes(1)
  })

  it('失败的旧请求不会把新会话标成失败', async () => {
    const first = deferred<unknown>()
    const second = deferred<unknown>()
    getSessionMetrics.mockImplementationOnce(() => first.promise)
    getSessionMetrics.mockImplementationOnce(() => second.promise)

    const wrapper = mount(SessionStatsPanel, { props: { sessionId: 's1' } })
    await nextTick()
    await wrapper.setProps({ sessionId: 's2' })
    await nextTick()

    second.resolve(metricsOf(2))
    await nextTick()
    await nextTick()

    first.reject(new Error('boom'))
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('2')
  })
})
