import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

/**
 * schema 漂移页（`/schema-mismatch`）。
 *
 * 背景：迁移没跑时网关旧行为是启动期 FATAL 退出 —— 端口无人监听，前端只剩白屏。
 * 现在网关漂移时仍只起最小服务（`/healthz` 200 + 业务 503），这页就是"把日志里的
 * expected/actual 变成一页可操作的东西"。这里钉三件事：
 *
 *  1. **版本号与命令要如实显示**（这是用户唯一的判断依据，不能显示占位符）；
 *  2. **首屏来自查询参数**（业务接口此刻全是 503，页面不能依赖它们）；
 *  3. **重试的三种结果可区分**：仍漂移 → 原地更新；已恢复 → 回首页；探不到 → 说明
 *     网关没在听。旧行为下这三种都是"白屏"，正是本轮要消灭的形态。
 */

const schemaApi = vi.hoisted(() => ({
  probeSchemaMismatch: vi.fn(),
  parseSchemaMismatch: vi.fn(),
  DEFAULT_SCHEMA_FIX: 'run alembic upgrade head',
  SCHEMA_MISMATCH_STATUS: 'schema_mismatch',
}))
vi.mock('../../api/schemaMismatch', () => schemaApi)

const clipboard = vi.hoisted(() => ({ copyText: vi.fn() }))
vi.mock('../../utils/clipboard', () => clipboard)

const antd = vi.hoisted(() => ({ message: { success: vi.fn(), error: vi.fn() } }))
vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()
  return { ...actual, message: antd.message }
})

import SchemaMismatchView from '../SchemaMismatchView.vue'

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Home', component: { template: '<div />' } },
      { path: '/schema-mismatch', name: 'SchemaMismatch', component: { template: '<div />' } },
    ],
  })
}

const MISMATCH = {
  expected: '20260901_0001_head',
  actual: '20260801_0007_old',
  reason: 'revision',
  fix: 'run alembic upgrade head',
}

async function mountAt(router = makeRouter(), query: Record<string, string> = {}) {
  router.push({ path: '/schema-mismatch', query })
  await router.isReady()
  const wrapper = mount(SchemaMismatchView, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  schemaApi.probeSchemaMismatch.mockReset()
  clipboard.copyText.mockReset()
  antd.message.success.mockReset()
  antd.message.error.mockReset()
  // 默认没有任何漂移：不写 stubs 的用例不该被上一个用例的 mock 影响
  schemaApi.probeSchemaMismatch.mockResolvedValue(null)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SchemaMismatchView', () => {
  it('首屏直接用查询参数渲染 expected/actual，不再请求业务接口', async () => {
    const { wrapper } = await mountAt(makeRouter(), {
      expected: MISMATCH.expected,
      actual: MISMATCH.actual,
      reason: MISMATCH.reason,
      fix: MISMATCH.fix,
    })

    expect(wrapper.get('[data-testid="sm-expected"]').text()).toBe(MISMATCH.expected)
    expect(wrapper.get('[data-testid="sm-actual"]').text()).toBe(MISMATCH.actual)
    expect(wrapper.get('[data-testid="sm-fix"]').text()).toBe(MISMATCH.fix)
    // 查询参数已带 expected/actual ⇒ 首屏无需探测
    expect(schemaApi.probeSchemaMismatch).not.toHaveBeenCalled()
  })

  it('没有查询参数时挂载后探一次 /healthz 校正式渲染', async () => {
    schemaApi.probeSchemaMismatch.mockResolvedValue(MISMATCH)

    const { wrapper } = await mountAt()
    await flushPromises()

    expect(schemaApi.probeSchemaMismatch).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-testid="sm-expected"]').text()).toBe(MISMATCH.expected)
    expect(wrapper.get('[data-testid="sm-actual"]').text()).toBe(MISMATCH.actual)
  })

  it('缺失表（revision 一致但表被删）单独一行列出', async () => {
    const { wrapper } = await mountAt(makeRouter(), {
      expected: 'abc',
      actual: 'abc',
      reason: 'missing_tables',
      missing: 'users,sessions',
      fix: MISMATCH.fix,
    })

    expect(wrapper.get('[data-testid="sm-missing"]').text()).toBe('users, sessions')
  })

  it('复制修复命令走全站统一剪贴板入口，并给出成功反馈', async () => {
    clipboard.copyText.mockResolvedValue(true)
    const { wrapper } = await mountAt(makeRouter(), { fix: MISMATCH.fix })

    await wrapper.get('[data-testid="sm-copy"]').trigger('click')
    await flushPromises()

    expect(clipboard.copyText).toHaveBeenCalledWith(MISMATCH.fix)
    expect(antd.message.success).toHaveBeenCalled()
    expect(antd.message.error).not.toHaveBeenCalled()
  })

  it('复制失败不能静默：必须给出可操作的提示', async () => {
    clipboard.copyText.mockResolvedValue(false)
    const { wrapper } = await mountAt(makeRouter(), { fix: MISMATCH.fix })

    await wrapper.get('[data-testid="sm-copy"]').trigger('click')
    await flushPromises()

    expect(antd.message.error).toHaveBeenCalled()
  })

  it('重试后仍是漂移：原地更新版本号并说明原因，不跳走', async () => {
    const next = {
      expected: '20261001_0002_head',
      actual: '20260801_0007_old',
      reason: 'revision',
      fix: MISMATCH.fix,
    }
    schemaApi.probeSchemaMismatch.mockResolvedValue(next)
    const { wrapper, router } = await mountAt(makeRouter(), {
      expected: MISMATCH.expected,
      actual: MISMATCH.actual,
      fix: MISMATCH.fix,
    })

    await wrapper.get('[data-testid="sm-retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="sm-expected"]').text()).toBe(next.expected)
    expect(wrapper.find('[data-testid="sm-note"]').exists()).toBe(true)
    expect(router.currentRoute.value.path).toBe('/schema-mismatch')
  })

  it('重试后已恢复（/healthz 不再报漂移且 /ready 200）→ 回首页', async () => {
    schemaApi.probeSchemaMismatch.mockResolvedValue(null)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))

    const { wrapper, router } = await mountAt(makeRouter(), {
      expected: MISMATCH.expected,
      actual: MISMATCH.actual,
      fix: MISMATCH.fix,
    })

    await wrapper.get('[data-testid="sm-retry"]').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
  })

  it('重试时网关没在听：说明"可能正在重启"，不谎称已恢复', async () => {
    schemaApi.probeSchemaMismatch.mockResolvedValue(null)
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')))

    const { wrapper, router } = await mountAt(makeRouter(), {
      expected: MISMATCH.expected,
      actual: MISMATCH.actual,
      fix: MISMATCH.fix,
    })

    await wrapper.get('[data-testid="sm-retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="sm-note"]').exists()).toBe(true)
    expect(router.currentRoute.value.path).toBe('/schema-mismatch')
  })
})
