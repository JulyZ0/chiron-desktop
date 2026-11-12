import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SessionStatsPanel from '../SessionStatsPanel.vue'
import * as sessionRuntime from '../../../api/sessionRuntime'

vi.mock('../../../api/sessionRuntime', () => ({
  getSessionMetrics: vi.fn(),
}))

const METRICS = {
  source: 'redis',
  totals: {
    turns: 3,
    input_tokens: 12345,
    output_tokens: 6789,
    cached_tokens: 4096,
    cache_hits: 2,
    cache_hit_rate: 0.666,
    cost_cents: 42,
  },
  throughput: { ttft_ms_p50: 812, output_tps_p50: 33.4, output_tps_p95: 51.2, sample_turns: 3 },
}

/**
 * 不 mock `$t`：走 test-setup 注册的真实 i18n（zh-CN）。
 * 早先组件用中文原文当 key，spec 才用「回显 key」的桩直接断言中文；
 * L1-4 语义化 key 后回显的是 `chat.stats.turns` 这类键名，断言只能改走真实译文。
 */
const global = {}

describe('SessionStatsPanel（会话统计：问题 5）', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('没有会话时提示先选会话，且不发请求', async () => {
    const w = mount(SessionStatsPanel, { props: { sessionId: '' }, global })
    await flushPromises()

    expect(w.text()).toContain('先选择一个会话')
    expect(sessionRuntime.getSessionMetrics).not.toHaveBeenCalled()
  })

  it('渲染回合数 / tokens / 缓存命中率 / 费用，并标注数据来源', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockResolvedValue(METRICS as never)
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()

    const text = w.text()
    expect(text).toContain('12,345') // 输入 tokens 千分位
    expect(text).toContain('6,789') // 输出 tokens
    expect(text).toContain('66.6%') // 缓存命中率
    expect(text).toContain('$0.4200') // 费用：分 → 美元（4 位小数，避免"看着像 0"）
    expect(text).toContain('实时层') // source=redis 的来源标注
  })

  it('展示吞吐（首字延迟 / tok/s）', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockResolvedValue(METRICS as never)
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()

    expect(w.text()).toContain('812 ms')
    expect(w.text()).toContain('33.4 tok/s')
  })

  it('接口失败时降级成一行提示，不把阅读打断成报错', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockRejectedValue(new Error('engine down'))
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()

    expect(w.text()).toContain('统计不可用')
  })

  it('切换会话会重新拉取该会话的统计', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockResolvedValue(METRICS as never)
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()
    await w.setProps({ sessionId: 's2' })
    await flushPromises()

    expect(sessionRuntime.getSessionMetrics).toHaveBeenCalledTimes(2)
    expect(sessionRuntime.getSessionMetrics).toHaveBeenLastCalledWith('s2')
  })

  // 「缺数据」与「真的是 0」必须长得不一样。
  // 早先缺字段时面板打印 0，于是"输入 0 / 输出 0 / 费用 $0.0000"看起来像一份
  // 真实账单，用户无法分辨是真没花钱还是根本没收到数据。
  it('后端没给某个数字时显示 —，不伪装成 0', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockResolvedValue({
      source: 'db',
      totals: { turns: 9 },
    } as never)
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()

    const text = w.text()
    expect(text).toContain('9')      // 拿到的照常显示
    expect(text).not.toContain('$0.0000') // 费用未知 ≠ 免费
    expect(text).toContain('—')      // 未知统一显示破折号
    // 命中率未知时也不能显示成 0.0%
    expect(text).not.toContain('0.0%')
  })

  it('数字确实是 0 时照常显示 0（不与"未知"混同）', async () => {
    vi.mocked(sessionRuntime.getSessionMetrics).mockResolvedValue({
      source: 'db',
      totals: { turns: 1, input_tokens: 0, output_tokens: 0, cost_cents: 0 },
    } as never)
    const w = mount(SessionStatsPanel, { props: { sessionId: 's1' }, global })
    await flushPromises()

    expect(w.text()).toContain('$0.0000')
  })
})
