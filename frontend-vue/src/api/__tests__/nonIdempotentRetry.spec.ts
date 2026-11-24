import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * H66（PRD 9.10）**非幂等请求不得全局盲重试**。
 *
 * 回归背景：响应拦截器把「503 / 429 / `retryable=true`」当成重试的**充分条件**，
 * 用原 `config` 直接 `api.request(config)`。限流时这对读接口是对的，对写接口是事故：
 *   · `POST /submit` 在响应回来前就建了 session 并启动引擎；
 *   · `POST /v1/agents/{id}/run` 一次派发 = 一个 AgentSession（还会计费）；
 *   · multipart 上传会把文件再传一遍。
 *
 * 这里钉住：
 * 1. GET 的重试行为**完全不变**（后向兼容）；
 * 2. POST/PATCH/DELETE 在 429/503/`retryable=true` 下一律**只发一次**；
 * 3. 带 `Idempotency-Key` 的写请求可以重试（服务端按键去重）；
 * 4. 上传（FormData）即使带幂等键也不重放；
 * 5. 本地模式的"组件未就绪"503 同样只重放可重放的请求（不重复派发）。
 */

type Host = {
  isDesktop?: boolean
  mode?: 'local' | 'cloud'
  apiUrl?: string
  deviceToken?: string
}

type ApiModule = typeof import('../index')

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

/** 让 adapter 固定回一个状态码，并记录被调用次数（每次调用 = 一次真实请求） */
function alwaysFails(
  mod: ApiModule,
  status: number,
  data: Record<string, unknown> = {},
): Array<Record<string, unknown>> {
  const calls: Array<Record<string, unknown>> = []
  mod.api.defaults.adapter = async (config) => {
    calls.push(config as unknown as Record<string, unknown>)
    return Promise.reject(
      Object.assign(new Error(`Request failed with status code ${status}`), {
        isAxiosError: true,
        config,
        response: { status, statusText: 'Error', headers: {}, data, config },
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

describe('可重放判定接线（H66）', () => {
  it('GET 的限流重试行为不变（读路径后向兼容）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 503, { error: 'rate limiter unavailable' })

    const pending = mod.api.get('/v1/conversations').catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    // 首次 + 2 次自动重试（`canAutoRetry` 上限 3 次判定为此前的次数）
    expect(calls.length).toBeGreaterThan(1)
  })

  it('POST 在 503 下**不**被重放，也不会谎报"正在自动重试"', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 503, { error: 'rate limiter unavailable' })
    const toasts = captureToasts()

    const pending = mod.api.post('/submit', { content: 'hi' }).catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBe(1)
    expect(toasts.some(m => m.includes('正在自动重试'))).toBe(false)
  })

  it('服务端 retryable=true 也不放行非幂等写请求', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 409, { error: 'session_busy', retryable: true })

    const pending = mod.api.post('/v1/agent/steer', { text: 'x' }).catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBe(1)
  })

  it('PATCH / DELETE 同样默认不重放', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 429, {})

    await mod.api.patch('/v1/conversations/c-1', { title: 't' }).catch(() => undefined)
    await mod.api.delete('/v1/agents/a-1').catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)

    expect(calls.length).toBe(2)
  })

  it('带 Idempotency-Key 的写请求可以自动重试（服务端按键去重）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 503, {})

    const pending = mod.api
      .post('/v1/agents/a-1/run', { task: 'x' }, { headers: { 'Idempotency-Key': 'idem-1' } })
      .catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBeGreaterThan(1)
  })

  it('上传（FormData）即使带幂等键也不重放', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 503, {})

    const form = new FormData()
    form.append('file', new File(['hello'], 'a.txt', { type: 'text/plain' }))
    const pending = mod.api
      .post('/v1/media/upload', form, { headers: { 'Idempotency-Key': 'idem-2' } })
      .catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBe(1)
  })

  it('派发 Agent（runAgent）在 503 下只发一次 —— 不会重复建 AgentSession', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 503, {})

    const pending = mod.runAgent('a-1', 'do the thing').catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBe(1)
    expect(calls[0]?.method).toBe('post')
  })

  it('createAgent 在 503 下只发一次 —— 不会凭空多出一个 Agent', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const calls = alwaysFails(mod, 429, {})

    const pending = mod.createAgent({ name: 'a' }).catch(() => undefined)
    await vi.advanceTimersByTimeAsync(120_000)
    await pending

    expect(calls.length).toBe(1)
  })

  it('本地模式的"组件未就绪"503 只重放读请求', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'local', apiUrl: 'http://127.0.0.1:1' })
    const calls = alwaysFails(mod, 503, { detail: '本地 PostgreSQL 未启动' })

    const pending = mod.api.post('/submit', { content: 'hi' }).catch(() => undefined)
    // 本地节奏 1+2+3+4+5+5s：可重放的请求会在这段里重发 6 次
    for (const ms of [1000, 2000, 3000, 4000, 5000, 5000]) {
      await vi.advanceTimersByTimeAsync(ms)
    }
    await pending

    expect(calls.length).toBe(1)
  })
})
