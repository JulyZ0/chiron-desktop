import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 401 不该引发「提示风暴」。
 *
 * 回归背景（真机事故）：登录态失效后，cloud 分支的动作是
 * 「清 token → 弹提示 → 500ms 后跳回首页」，而跳转会重新挂载页面、重新发请求、
 * 又 401 —— 提示与跳转互相驱动。同时 `TakeoverOverlay` 挂在 `App.vue` 上每 5 秒
 * 轮询 `/v1/superagent/takeovers`（经 `authMW`），把"偶尔一次"放大成"频繁弹窗"。
 *
 * 这里钉住四件事：
 * 1. 同一时间窗内多条 401 只提示一次（不重复弹、不重复清 token、不重复跳转）；
 * 2. 静默请求（`__silent`）失败时**完全不**派发 `api:error`；
 * 3. 静默只影响提示，错误仍原样 reject 给调用方；
 * 4. 超出时间窗后重新提示 —— 收敛不等于永久忽略。
 */

type Host = {
  isDesktop?: boolean
  mode?: 'local' | 'cloud'
  apiUrl?: string
  deviceToken?: string
}

type ApiModule = typeof import('../index')

/** 每次都用新模块实例：`api/index.ts` 在 import 时就快照了 `globalThis.chironDesktop` */
async function loadApi(host: Host): Promise<ApiModule> {
  vi.resetModules()
  ;(globalThis as { chironDesktop?: Host }).chironDesktop = host
  try {
    localStorage.clear()
  } catch {
    /* jsdom 存储不可用时不影响本用例 */
  }
  return await import('../index')
}

/** 让 adapter 固定回 401，并记录被调用次数 */
function alwaysUnauthorized(mod: ApiModule, data: Record<string, unknown> = {}) {
  const calls: Array<Record<string, unknown>> = []
  mod.api.defaults.adapter = async (config) => {
    calls.push(config as unknown as Record<string, unknown>)
    return Promise.reject(
      Object.assign(new Error('Request failed with status code 401'), {
        isAxiosError: true,
        config,
        response: { status: 401, statusText: 'Unauthorized', headers: {}, data, config },
      }),
    )
  }
  return calls
}

function captureToasts(): string[] {
  const seen: string[] = []
  window.addEventListener('api:error', (e) => {
    seen.push(String((e as CustomEvent<{ message?: string }>).detail?.message ?? ''))
  })
  return seen
}

beforeEach(() => {
  document.body.innerHTML = ''
})

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('401 提示收敛', () => {
  it('并发的多条 401 只提示一次（不重复弹窗）', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    await Promise.all([
      mod.api.get('/v1/conversations').catch(() => undefined),
      mod.api.get('/v1/models').catch(() => undefined),
      mod.api.get('/v1/superagent/takeovers').catch(() => undefined),
      mod.api.get('/v1/billing/usage').catch(() => undefined),
    ])

    expect(toasts.length).toBe(1)
    expect(toasts[0]).toContain('登录已过期')
  })

  it('收敛不影响错误传播：窗口内后续 401 仍原样 reject 给调用方', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    alwaysUnauthorized(mod)

    const first = await mod.api.get('/v1/conversations').catch(e => e)
    // 闸门已关闭的第二次：不该被"静默成功"，调用方仍要能感知失败
    const second = await mod.api.get('/v1/models').catch(e => e)
    await vi.advanceTimersByTimeAsync(1000)

    for (const err of [first, second]) {
      expect((err as { response?: { status?: number } }).response?.status).toBe(401)
    }
  })

  it('local 模式同样收敛，只是不清会话也不跳转', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    await Promise.all([
      mod.api.get('/v1/conversations').catch(() => undefined),
      mod.api.get('/v1/models').catch(() => undefined),
    ])

    expect(toasts.length).toBe(1)
    expect(toasts[0]).toContain('登录状态已失效')
  })

  it('超出收敛窗口后重新提示（不是永久静音）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    await mod.api.get('/v1/conversations').catch(() => undefined)
    expect(toasts.length).toBe(1)

    // 实现常量是 10s；留出余量推进
    await vi.advanceTimersByTimeAsync(11_000)
    await mod.api.get('/v1/conversations').catch(() => undefined)

    expect(toasts.length).toBe(2)
  })
})

describe('静默请求', () => {
  it('失败时完全不派发 api:error（后台轮询不打扰用户）', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    await mod.api.get('/v1/superagent/takeovers', { __silent: true }).catch(() => undefined)

    expect(toasts.length).toBe(0)
  })

  it('静默只影响提示，错误仍原样 reject 给调用方', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnauthorized(mod)

    const error = await mod.api.get('/v1/superagent/takeovers', { __silent: true }).catch(e => e)

    expect((error as { response?: { status?: number } }).response?.status).toBe(401)
  })

  it('静默请求不占用收敛窗口：随后的可见请求照样提示', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    await mod.api.get('/v1/superagent/takeovers', { __silent: true }).catch(() => undefined)
    expect(toasts.length).toBe(0)

    // 会计入窗口的是这条可见请求，它必须提示
    await mod.api.get('/v1/conversations').catch(() => undefined)
    expect(toasts.length).toBe(1)
  })

  it('接管轮询的两个入口默认静默', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    alwaysUnauthorized(mod)
    const toasts = captureToasts()

    // 不传 options 就是轮询的用法 —— 默认静默
    await mod.listTakeovers().catch(() => undefined)
    await mod.getTakeoverSegment('s-1').catch(() => undefined)

    expect(toasts.length).toBe(0)
  })
})
