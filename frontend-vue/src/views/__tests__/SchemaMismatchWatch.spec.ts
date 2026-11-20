import { describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

/**
 * schema 漂移跳转器（`src/router/schemaMismatch.ts`）。
 *
 * 为什么这个 spec 也放在 `views/__tests__/`：它是 `/schema-mismatch` 这条路径的
 * "入口"实现（谁把用户送到这个页面），与页面本身一起被
 * `vitest run src/views/__tests__/SchemaMismatch*` 覆盖。
 *
 * 钉住三条不变量（每一条写错都会把正常用户困在错误页或让导航变慢）：
 *
 *  1. **只在 status=schema_mismatch 时跳**：探测为 null（正常网关 / 网关没起来 /
 *     超时）一律不动 —— 误跳的代价是系统明明正常，用户却被锁在诊断页。
 *  2. **不阻塞导航**：`check()` 是 fire-and-forget，导航在该 Promise settle 之前就已
 *     完成；不能因为探测慢就把每次点击都拖一个 RTT。
 *  3. **不自旋**：节流窗口内的导航不再探测；已停在漂移页时也不探测（页面有自己的
 *     重试按钮）。
 */
import { MIN_PROBE_INTERVAL_MS, SCHEMA_MISMATCH_ROUTE, setupSchemaMismatchWatch } from '../../router/schemaMismatch'

const MISMATCH = {
  expected: 'head_abc',
  actual: 'old_xyz',
  reason: 'revision',
  fix: 'run alembic upgrade head',
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Home', component: { template: '<div />' } },
      { path: '/chat', name: 'Chat', component: { template: '<div />' } },
      { path: '/schema-mismatch', name: SCHEMA_MISMATCH_ROUTE, component: { template: '<div />' } },
    ],
  })
}

describe('setupSchemaMismatchWatch', () => {
  it('启动时探测到漂移 → 跳到漂移页并带上 expected/actual', async () => {
    const router = makeRouter()
    await router.push('/')
    const probe = vi.fn().mockResolvedValue(MISMATCH)

    setupSchemaMismatchWatch(router, { probe })
    await flushPromises()

    expect(probe).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe(SCHEMA_MISMATCH_ROUTE)
    expect(router.currentRoute.value.query.expected).toBe(MISMATCH.expected)
    expect(router.currentRoute.value.query.actual).toBe(MISMATCH.actual)
    expect(router.currentRoute.value.query.fix).toBe(MISMATCH.fix)
  })

  it('探测不到漂移（null）时绝不跳转 —— 正常网关的 /healthz 就是 404', async () => {
    const router = makeRouter()
    await router.push('/')
    const probe = vi.fn().mockResolvedValue(null)

    setupSchemaMismatchWatch(router, { probe })
    await flushPromises()

    expect(probe).toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('探测抛错也不能打断导航（fire-and-forget）', async () => {
    const router = makeRouter()
    await router.push('/')
    const probe = vi.fn().mockRejectedValue(new Error('network down'))

    setupSchemaMismatchWatch(router, { probe })
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
  })

  it('导航不等待探测：慢探测期间路由已经切过去了', async () => {
    const router = makeRouter()
    await router.push('/')
    let release: (value: null) => void = () => {}
    const probe = vi.fn().mockImplementation(
      () => new Promise<null>(resolve => {
        release = resolve
      }),
    )
    const watch = setupSchemaMismatchWatch(router, { probe, now: () => 0 })
    await flushPromises()

    // 探测仍挂着，导航必须已经完成
    const navigation = router.push('/chat')
    await navigation
    expect(router.currentRoute.value.name).toBe('Chat')

    release(null)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('Chat')
    expect(watch).toBeTruthy()
  })

  it('节流窗口内不重复探测；force 可绕过', async () => {
    const router = makeRouter()
    await router.push('/')
    const probe = vi.fn().mockResolvedValue(null)
    const now = vi.fn().mockReturnValue(1_000)

    const { check } = setupSchemaMismatchWatch(router, { probe, now })
    await flushPromises()
    expect(probe).toHaveBeenCalledTimes(1)

    now.mockReturnValue(1_000 + MIN_PROBE_INTERVAL_MS - 1)
    check()
    await flushPromises()
    expect(probe).toHaveBeenCalledTimes(1)

    now.mockReturnValue(1_000 + MIN_PROBE_INTERVAL_MS)
    check()
    await flushPromises()
    expect(probe).toHaveBeenCalledTimes(2)

    check({ force: true })
    await flushPromises()
    expect(probe).toHaveBeenCalledTimes(3)
  })

  it('已停在漂移页时不再自动探测（页面有自己的重试）', async () => {
    const router = makeRouter()
    await router.push({ path: '/schema-mismatch', query: {} })
    const probe = vi.fn().mockResolvedValue(MISMATCH)

    setupSchemaMismatchWatch(router, { probe })
    await flushPromises()

    expect(probe).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe(SCHEMA_MISMATCH_ROUTE)
  })
})
