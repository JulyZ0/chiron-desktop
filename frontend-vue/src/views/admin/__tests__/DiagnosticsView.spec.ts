import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { Popconfirm } from 'ant-design-vue'

/**
 * `DiagnosticsView` 的 LLM 熔断区块。
 *
 * 这块补的是**路径**而不是能力：引擎侧熔断与重置早已实现，但网关从未转发、
 * 前端也没有入口，运维只认前端 ⇒ 实际调不到，只能干等 `recovery_timeout` 自己半开。
 *
 * 所以钉住的是三个会让人做出**相反判断**的分支：
 *   1. chat 与 embed 是两张独立表 —— 只看 chat 会把"embedding 已熔断"显示成健康；
 *   2. 读不到 ≠ 没有熔断 —— 空列表会被读成"链路正常"，而实际是看不见；
 *   3. 空串 ≠ closed、0ms ≠ 很快 —— 未采样必须显示未知/占位，不能冒充正常。
 */

const apiMocks = vi.hoisted(() => ({
  getDiagnostics: vi.fn((): Promise<unknown> =>
    Promise.resolve({ generated_at: 0, status: 'ok', counts: { ok: 0, warn: 0, fail: 0, skipped: 0 }, checks: [] })),
  getDiagnosticsSummary: vi.fn((): Promise<unknown> =>
    Promise.resolve({ generated_at: 0, status: 'ok', counts: { ok: 0, warn: 0, error: 0, unknown: 0 }, checks: [] })),
  getGatewayBreakers: vi.fn((): Promise<unknown> => Promise.resolve([])),
  resetGatewayBreaker: vi.fn((): Promise<unknown> => Promise.resolve()),
}))

vi.mock('../../../api/admin', () => ({
  getDiagnostics: apiMocks.getDiagnostics,
  getDiagnosticsSummary: apiMocks.getDiagnosticsSummary,
  getGatewayBreakers: apiMocks.getGatewayBreakers,
  resetGatewayBreaker: apiMocks.resetGatewayBreaker,
}))

import DiagnosticsView from '../DiagnosticsView.vue'

function mountView() {
  return mount(DiagnosticsView)
}

const rows = (wrapper: ReturnType<typeof mountView>) =>
  wrapper.findAll('.breaker-row').filter(r => !r.classes().includes('breaker-row--head'))

describe('DiagnosticsView 的熔断区块', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getGatewayBreakers.mockResolvedValue([])
    apiMocks.getDiagnosticsSummary.mockResolvedValue({
      generated_at: 0, status: 'ok', counts: { ok: 0, warn: 0, error: 0, unknown: 0 }, checks: [],
    })
  })

  it('chat 与 embed 分开显示：embed 熔断不会被 chat 正常掩盖', async () => {
    apiMocks.getGatewayBreakers.mockResolvedValue([
      { provider: 'openai', chat: 'closed', embed: 'open', avg_latency_ms: 120 },
    ])

    const wrapper = mountView()
    await flushPromises()

    const row = rows(wrapper)[0]
    expect(row).toBeTruthy()
    expect(row.text()).toContain('openai')
    // 同一行里必须同时出现"正常"与"熔断"两个不同标签。
    expect(row.text()).toContain('正常')
    expect(row.text()).toContain('熔断')
    wrapper.unmount()
  })

  it('读不到熔断状态时报警告，而不是渲染成"没有熔断"', async () => {
    apiMocks.getGatewayBreakers.mockRejectedValue(new Error('502'))

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('引擎不可达')
    // 关键：不能落进 Empty 分支 —— 那会被读成"链路健康"。
    expect(wrapper.text()).not.toContain('没有服务商熔断信息')
    wrapper.unmount()
  })

  it('空串显示未知、0ms 显示占位，都不冒充正常', async () => {
    apiMocks.getGatewayBreakers.mockResolvedValue([
      { provider: 'legacy', chat: '', embed: '', avg_latency_ms: 0 },
    ])

    const wrapper = mountView()
    await flushPromises()

    const row = rows(wrapper)[0]
    expect(row.text()).toContain('未知')
    expect(row.text()).toContain('—')
    expect(row.text()).not.toContain('正常')
    expect(row.text()).not.toContain('0.0 ms')
    wrapper.unmount()
  })

  it('确认解除熔断后调后端，并重新拉一次读数', async () => {
    apiMocks.getGatewayBreakers.mockResolvedValue([
      { provider: 'openai', chat: 'open', embed: 'closed', avg_latency_ms: 10 },
    ])

    const wrapper = mountView()
    await flushPromises()
    expect(apiMocks.getGatewayBreakers).toHaveBeenCalledTimes(1)

    const pop = wrapper.findComponent(Popconfirm)
    expect(pop.exists()).toBe(true)
    // 走 Popconfirm 的确认路径（不是直接调 handler）：确认这道闸门本身也要在链路上。
    await pop.vm.$emit('confirm')
    await flushPromises()

    expect(apiMocks.resetGatewayBreaker).toHaveBeenCalledWith('openai')
    // 第二次拉取：以引擎回报为准，不就地乐观改本地状态。
    expect(apiMocks.getGatewayBreakers).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('解除失败不清空已有读数（失败不该让面板变成空白）', async () => {
    apiMocks.getGatewayBreakers.mockResolvedValue([
      { provider: 'openai', chat: 'open', embed: 'closed', avg_latency_ms: 10 },
    ])
    apiMocks.resetGatewayBreaker.mockRejectedValue(new Error('boom'))

    const wrapper = mountView()
    await flushPromises()

    await wrapper.findComponent(Popconfirm).vm.$emit('confirm')
    await flushPromises()

    expect(rows(wrapper).length).toBe(1)
    expect(rows(wrapper)[0].text()).toContain('openai')
    wrapper.unmount()
  })
})

/**
 * P0-1 的聚合清单。这里钉住的是三件会让排障走错方向的事：
 *   1. 每张卡都要显示 status + detail，且**只在需要处理时**显示 fix（ok 行不该制造噪音）；
 *   2. `unknown` 必须与 `error` 分开显示 —— "看不见"和"坏了"的处置相反；
 *   3. 清单拉取失败要内联报错，且**不能**把上一次的结论留在屏幕上。
 */
const summaryCards = (wrapper: ReturnType<typeof mountView>) =>
  wrapper.findAll('[data-test="diag-summary-card"]')

describe('DiagnosticsView 的聚合清单', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.getGatewayBreakers.mockResolvedValue([])
  })

  it('按 key 渲染标题、状态与修复指引；ok 行不带修复指引', async () => {
    apiMocks.getDiagnosticsSummary.mockResolvedValue({
      generated_at: 1,
      status: 'error',
      counts: { ok: 1, warn: 0, error: 1, unknown: 0 },
      checks: [
        { key: 'schema', title: 'Schema 版本', status: 'ok', detail: 'schema 版本 0002，与期望一致', fix: '' },
        {
          key: 'tables', title: '必需数据表', status: 'error',
          detail: '缺少 1 张必需表：project_checkpoints',
          fix: '执行 alembic upgrade head',
        },
      ],
    })

    const wrapper = mountView()
    await flushPromises()

    const cards = summaryCards(wrapper)
    expect(cards.length).toBe(2)

    const schema = cards[0].text()
    expect(schema).toContain('Schema 版本')
    expect(schema).toContain('正常')
    expect(schema).toContain('0002')
    // ok 行不显示 fix：诊断页的 fix 是"待办"，待办清单里不该出现已完成项。
    expect(schema).not.toContain('alembic upgrade head')

    const tables = cards[1].text()
    expect(tables).toContain('必需数据表')
    expect(tables).toContain('异常')
    expect(tables).toContain('project_checkpoints')
    expect(tables).toContain('alembic upgrade head')
    wrapper.unmount()
  })

  it('unknown 与 error 分开显示，不把"看不见"报成"坏了"', async () => {
    apiMocks.getDiagnosticsSummary.mockResolvedValue({
      generated_at: 1,
      status: 'unknown',
      counts: { ok: 0, warn: 0, error: 0, unknown: 2 },
      checks: [
        { key: 'tables', title: '必需数据表', status: 'unknown', detail: '引擎未启动', fix: '启动 Python 引擎后重试' },
        { key: 'knowledge', title: '知识库服务', status: 'unknown', detail: '知识库服务未启动', fix: '启动 Python 引擎' },
      ],
    })

    const wrapper = mountView()
    await flushPromises()

    const text = summaryCards(wrapper).map(c => c.text()).join('\n')
    expect(text).toContain('未知')
    expect(text).not.toContain('异常')
    expect(text).toContain('引擎未启动')
    wrapper.unmount()
  })

  it('清单加载失败时内联报错，且不渲染任何结论', async () => {
    apiMocks.getDiagnosticsSummary.mockRejectedValue(new Error('502 Bad Gateway'))

    const wrapper = mountView()
    await flushPromises()

    expect(summaryCards(wrapper).length).toBe(0)
    expect(wrapper.text()).toContain('诊断清单加载失败')
    expect(wrapper.text()).toContain('502 Bad Gateway')
    wrapper.unmount()
  })
})
