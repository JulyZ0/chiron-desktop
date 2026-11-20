import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

import { resetMonitorOverviewForTest } from '@/composables/useMonitorOverview'
import type { MonitorOverview } from '@/api/admin'

/**
 * `MonitorView`（后台「运行时监控」）。
 *
 * 这一页的核心承诺是**诚实**：采不到就写「—」，而不是 0。因此这里钉四件事：
 *   1. 三个 Tab 各自渲染对应面板（沿用 AdminTabbedView，懒加载）；
 *   2. 10s 自动刷新可暂停 —— 暂停后真的不再请求；
 *   3. 空数据 / 缺字段渲染「—」与「无采样数据」，不出现误导性的 0；
 *   4. 时间范围切换会带参重取。
 */

const apiMocks = vi.hoisted(() => ({ getMonitorOverview: vi.fn() }))

vi.mock('@/api/admin', () => ({
  getMonitorOverview: apiMocks.getMonitorOverview,
}))

import MonitorView from '../MonitorView.vue'

// jsdom 没有 matchMedia，antd 的 Tabs/Grid 挂载时会读它。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/admin/monitor', component: { template: '<div />' } },
    ],
  })
}

const okMeta = {
  generated_at: '2026-11-20T10:00:00Z',
  status: 'ok' as const,
  sources: ['test'],
  errors: [],
}

/** 一份"数据齐全"的响应；用 patch 覆盖个别区块来构造降级/空数据场景。 */
function overview(patch: Partial<MonitorOverview> = {}): MonitorOverview {
  const base: MonitorOverview = {
    generated_at: '2026-11-20T10:00:00Z',
    window_seconds: 900,
    window: '15m',
    partial: false,
    performance: {
      ...okMeta,
      window_seconds: 900,
      window: '15m',
      requests: {
        has_samples: true,
        samples: 120,
        success: 118,
        client_errors: 0,
        server_errors: 2,
        error_rate_percent: 1.7,
        p50_ms: 12,
        p95_ms: 80,
        p99_ms: 140,
        max_ms: 200,
        buckets: [
          { at: '2026-11-20T09:45:00Z', count: 40, errors: 0, p95_ms: 70, avg_ms: 20 },
          { at: '2026-11-20T09:50:00Z', count: 40, errors: 1, p95_ms: 82, avg_ms: 21 },
          { at: '2026-11-20T09:55:00Z', count: 40, errors: 1, p95_ms: 91, avg_ms: 22 },
        ],
      },
      db_pool: {
        total_conns: 5,
        idle_conns: 3,
        acquired_conns: 2,
        max_conns: 10,
        constructing_conns: 0,
        empty_acquire_count: 0,
        acquire_duration_ms: 4,
        utilization_percent: 20,
        source: 'pgxpool.Stat()',
      },
      go_runtime: {
        goroutines: 42,
        heap_alloc_mb: 30,
        heap_sys_mb: 40,
        heap_inuse_mb: 35,
        gc_runs: 7,
        gomaxprocs: 8,
        cpu_percent: 3.5,
        uptime_seconds: 3600,
        source: 'test',
        cpu_note: '',
      },
      engine: {
        configured: true,
        reachable: true,
        instances: 1,
        version: '3.0.0',
        uptime_seconds: 120,
        memory_mb: 320,
        cpu_percent: 5,
        active_tasks: 2,
        source: 'test',
        cpu_note: '',
      },
      redis_latency_ms: 1,
      db_latency_ms: 2,
    },
    queue: {
      ...okMeta,
      streams: [
        {
          name: 'engine',
          key: 'engine:tasks',
          depth: 7,
          pending: 1,
          lag: 2,
          groups: 1,
          consumers: 2,
          dlq_depth: 0,
          oldest_wait_seconds: 30,
          source: 'test',
        },
      ],
      run_state: {
        active: 2,
        awaiting_input: 1,
        failed_last_hour: 0,
        timeout_last_hour: 0,
        run_leases: 2,
        source: 'agent_runs',
      },
    },
    cache: {
      ...okMeta,
      redis: {
        used_memory_mb: 12,
        max_memory_mb: 100,
        hit_rate_percent: 88,
        keyspace_hits: 880,
        keyspace_misses: 120,
        keys: 1000,
        keys_with_ttl: 600,
        avg_ttl_ms: 300000,
        evicted_keys: 0,
        expired_keys: 5,
        maxmemory_policy: 'allkeys-lru',
        source: 'test',
      },
      llm: {
        l1_hits: 90,
        l2_hits: 6,
        l3_hits: 4,
        misses: 10,
        hit_rate_percent: 90,
        source: 'test',
      },
    },
  }
  return { ...base, ...patch }
}

async function mountView() {
  const router = makeRouter()
  await router.push('/admin/monitor')
  await router.isReady()
  const wrapper = mount(MonitorView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

function tabButtons(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('[role="tab"]')
}

describe('MonitorView 的运行时监控仪表盘', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetMonitorOverviewForTest()
    apiMocks.getMonitorOverview.mockResolvedValue(overview())
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('默认渲染性能面板，切换到队列 / 缓存后渲染对应面板', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-test="performance-panel"]').exists()).toBe(true)

    const tabs = tabButtons(wrapper)
    expect(tabs.length).toBe(3)

    await tabs[1].trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="queue-panel"]').exists()).toBe(true)

    await tabButtons(wrapper)[2].trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="cache-panel"]').exists()).toBe(true)

    wrapper.unmount()
  })

  it('10s 自动刷新开启时轮询，暂停后不再请求', async () => {
    vi.useFakeTimers()
    const wrapper = await mountView()
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenCalledTimes(2)

    // 暂停：Switch 的根节点带 data-test。
    await wrapper.find('[data-test="auto-refresh"]').trigger('click')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenCalledTimes(2)

    // 恢复：立刻补一次，而不是等下一个 10s。
    await wrapper.find('[data-test="auto-refresh"]').trigger('click')
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenCalledTimes(3)

    wrapper.unmount()
  })

  it('无采样数据时显示「无采样数据」与「—」，而不是 0', async () => {
    const data = overview()
    data.performance.requests = {
      has_samples: false,
      samples: 0,
      success: 0,
      client_errors: 0,
      server_errors: 0,
      error_rate_percent: 0,
      p50_ms: null,
      p95_ms: null,
      p99_ms: null,
      max_ms: null,
      note: '近 15m 无请求采样数据',
      buckets: [],
    }
    data.partial = true
    apiMocks.getMonitorOverview.mockResolvedValue(data)

    const wrapper = await mountView()
    const text = wrapper.text()
    expect(text).toContain('无采样数据')
    expect(wrapper.find('[data-test="kpi-requests"]').text()).toBe('—')
    expect(wrapper.find('[data-test="kpi-p99"]').text()).toBe('—')
    expect(wrapper.find('[data-test="monitor-partial"]').exists()).toBe(true)

    // 每个区块都要有体检条，而不是静默留白。
    expect(wrapper.find('[data-test="section-status"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('组件不可达时队列/缓存显示「—」并点名组件', async () => {
    const data = overview()
    data.partial = true
    data.queue = {
      generated_at: '2026-11-20T10:00:00Z',
      status: 'unavailable',
      sources: ['Redis XLEN'],
      errors: [{ component: 'redis', message: 'Redis 未配置，队列深度未知' }],
      streams: [],
      run_state: null,
    }
    data.cache = {
      generated_at: '2026-11-20T10:00:00Z',
      status: 'unavailable',
      sources: ['Redis INFO'],
      errors: [{ component: 'redis', message: 'Redis 未配置' }],
      redis: null,
      llm: null,
    }
    apiMocks.getMonitorOverview.mockResolvedValue(data)

    const wrapper = await mountView()
    await tabButtons(wrapper)[1].trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="kpi-queue-depth"]').text()).toBe('—')
    expect(wrapper.find('[data-test="kpi-active-runs"]').text()).toBe('—')
    expect(wrapper.text()).toContain('redis')

    await tabButtons(wrapper)[2].trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="kpi-redis-hit"]').text()).toBe('—')
    expect(wrapper.find('[data-test="kpi-llm-hit"]').text()).toBe('—')
    wrapper.unmount()
  })

  it('切换时间范围会以新窗口重取', async () => {
    const wrapper = await mountView()
    expect(apiMocks.getMonitorOverview).toHaveBeenLastCalledWith('15m')

    await wrapper.find('[data-test="window-5m"] input').setValue(true)
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenLastCalledWith('5m')

    await wrapper.find('[data-test="window-60m"] input').setValue(true)
    await flushPromises()
    expect(apiMocks.getMonitorOverview).toHaveBeenLastCalledWith('60m')
    wrapper.unmount()
  })

  it('读取失败时显示错误条，而不是空白页', async () => {
    apiMocks.getMonitorOverview.mockRejectedValue(new Error('network down'))
    const wrapper = await mountView()
    const alert = wrapper.find('[data-test="monitor-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('network down')
    wrapper.unmount()
  })
})
