import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * H68（PRD 9.10）请求层的认证代次围栏。
 *
 * 每一笔请求在**发出时**记下"属于哪一代认证 / 用的是哪份凭据"；之后的 401 处理、
 * 重试与跳转都先比对：不一致说明账号已经换过，整条结果作废。
 *
 * 这里钉住：
 * 1. 旧账号的迟到 401 不清新账号的登录态、不弹"登录已过期"、不跳登录页；
 * 2. 当前代的 401 行为完全不变（清态 + 提示一次）；
 * 3. 旧账号的可重试失败不会被重放（否则 B 的 token 会附到 A 的请求上）；
 * 4. 代次与凭据会被钉在 config 上（供上层排查/断言）。
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
    /* jsdom 存储不可用不影响本用例 */
  }
  return await import('../index')
}

function httpError(config: unknown, status: number, data: Record<string, unknown> = {}) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    config,
    response: { status, statusText: 'Error', headers: {}, data, config },
  })
}

/**
 * 可手动结束的 adapter：让用例精确控制"A 的请求什么时候回来"，
 * 从而把"迟到"这件事写成确定性的顺序，而不是靠计时器碰运气。
 */
type Deferred = {
  config: Record<string, unknown>
  reject: (reason: unknown) => void
  resolve: (value: unknown) => void
}

function controllableAdapter(mod: ApiModule): Deferred[] {
  const pending: Deferred[] = []
  mod.api.defaults.adapter = (config) =>
    new Promise((resolve, reject) => {
      pending.push({ config: config as unknown as Record<string, unknown>, resolve, reject })
    })
  return pending
}

function captureToasts(): string[] {
  const seen: string[] = []
  window.addEventListener('api:error', (e) => {
    seen.push(String((e as CustomEvent<{ message?: string }>).detail?.message ?? ''))
  })
  return seen
}

/** 让已排队的微任务跑完（请求拦截器 / adapter 调用都在微任务里） */
async function flushMicrotasks(times = 4): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve()
}

beforeEach(() => {
  document.body.innerHTML = ''
  window.location.hash = ''
})

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('认证代次围栏（H68）', () => {
  it('请求在发出时钉住代次与凭据', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const pending = controllableAdapter(mod)
    mod.setAuthToken('token-a')

    const p = mod.api.get('/v1/conversations')
    await flushMicrotasks()

    expect(pending[0]?.config.__authGeneration).toBe(mod.currentAuthGeneration())
    expect(pending[0]?.config.__authToken).toBe('token-a')

    pending[0]!.resolve({ data: {}, status: 200, headers: {}, config: pending[0]!.config })
    await p
  })

  it('★ 旧账号的迟到 401 不清新账号的登录态、不提示、不跳转', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const pending = controllableAdapter(mod)
    const toasts = captureToasts()
    localStorage.setItem('user', JSON.stringify({ id: 'u-new', name: 'B', role: 'user' }))

    mod.setAuthToken('token-a')
    const p = mod.api.get('/v1/conversations').catch(e => e)
    await flushMicrotasks()

    // 账号切到 B（真实流程里由 store.login 做：换 token + 递增代次）
    mod.setAuthToken('token-b')
    mod.bumpAuthGeneration('login')
    await flushMicrotasks()

    // A 的请求现在才以 401 收尾
    pending[0]!.reject(httpError(pending[0]!.config, 401))
    const err = await p

    expect((err as { response?: { status?: number } }).response?.status).toBe(401)
    // 不清 B 的登录态
    expect(localStorage.getItem('user')).not.toBeNull()
    // 不弹"登录已过期"
    expect(toasts.some(m => m.includes('登录已过期'))).toBe(false)
  })

  it('同一代内的 401 行为不变：提示一次并清本地登录态', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const pending = controllableAdapter(mod)
    const toasts = captureToasts()
    localStorage.setItem('user', JSON.stringify({ id: 'u-a', name: 'A', role: 'user' }))

    mod.setAuthToken('token-a')
    const p = mod.api.get('/v1/conversations').catch(e => e)
    await flushMicrotasks()

    pending[0]!.reject(httpError(pending[0]!.config, 401))
    await p

    expect(localStorage.getItem('user')).toBeNull()
    expect(toasts.some(m => m.includes('登录已过期'))).toBe(true)
  })

  it('★ 旧账号的可重试失败不会被重放（B 的 token 不能附到 A 的请求上）', async () => {
    vi.useFakeTimers()
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const pending = controllableAdapter(mod)

    mod.setAuthToken('token-a')
    const p = mod.api.get('/v1/conversations').catch(e => e)
    await flushMicrotasks()
    expect(pending.length).toBe(1)

    // 请求还在飞的时候换了账号
    mod.setAuthToken('token-b')
    mod.bumpAuthGeneration('login')
    await flushMicrotasks()

    // A 的请求以可重试的 503 回来
    pending[0]!.reject(httpError(pending[0]!.config, 503, {}))
    await vi.advanceTimersByTimeAsync(120_000)
    await p

    // 只发过一次：没有用 B 的凭据把 A 的请求再发一遍
    expect(pending.length).toBe(1)
  })

  it('代次递增会重置账号域缓存与 401 提示闸门', async () => {
    const mod = await loadApi({ isDesktop: true, mode: 'cloud' })
    const pending = controllableAdapter(mod)
    const toasts = captureToasts()
    localStorage.setItem('user', JSON.stringify({ id: 'u-a', name: 'A', role: 'user' }))

    // 第一代：一次 401 → 提示
    mod.setAuthToken('token-a')
    const first = mod.api.get('/v1/conversations').catch(e => e)
    await flushMicrotasks()
    pending[0]!.reject(httpError(pending[0]!.config, 401))
    await first
    expect(toasts.length).toBe(1)

    // 换账号后 10s 窗口内的新 401 仍必须提示（闸门随代次重置）
    localStorage.setItem('user', JSON.stringify({ id: 'u-b', name: 'B', role: 'user' }))
    mod.setAuthToken('token-b')
    mod.bumpAuthGeneration('login')
    await flushMicrotasks()

    const second = mod.api.get('/v1/models').catch(e => e)
    await flushMicrotasks()
    pending[1]!.reject(httpError(pending[1]!.config, 401))
    await second

    expect(toasts.length).toBe(2)
  })
})
