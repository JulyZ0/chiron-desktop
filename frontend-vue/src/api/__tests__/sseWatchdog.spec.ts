import { afterEach, expect, it, vi } from 'vitest'

/**
 * SSE **存活探测**（H69）。
 *
 * 背景：通用流此前只在 EOF / 异常时重连。而"半开连接"（NAT 空闲超时、代理掐长连接、
 * 合盖唤醒、Wi-Fi 换网）**既不抛错也不 EOF** —— `reader.read()` 就那么挂着，界面停在
 * "思考中"，一句提示都没有。网关那条流每 15s 发一次 `: ping`（internal/api/events.go），
 * 心跳帧在 `parseSSEFrame` 里被当注释丢掉，所以"活性"只能判在**字节**这一层。
 *
 * 这里做的是**故障注入**：造一条"连上了但永远不给字节"的流，验证
 *   ① 静默到阈值 ⇒ 自己断开并重连（带 Last-Event-ID）；
 *   ② 心跳/正文持续到达 ⇒ 不重连（不会在健康链路上抖）；
 *   ③ 后台恢复（`online`）时若已静默 ⇒ 立刻重连，不等下一拍。
 */

type Host = { isDesktop?: boolean; mode?: 'local' | 'cloud'; apiUrl?: string }
type ApiModule = typeof import('../index')

async function loadApi(): Promise<ApiModule> {
  vi.resetModules()
  ;(globalThis as { chironDesktop?: Host }).chironDesktop = {
    isDesktop: true,
    mode: 'local',
    apiUrl: 'http://127.0.0.1:918/v1',
  }
  return await import('../index')
}

const encoder = new TextEncoder()
type Call = { url: string; init: Record<string, unknown> }

function stubFetch(...makeResponse: Array<() => unknown>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: Record<string, unknown>) => {
    calls.push({ url, init })
    const index = Math.min(calls.length - 1, makeResponse.length - 1)
    return makeResponse[index]()
  }))
  return calls
}

/** 一条**永远不给字节**的流：这就是半开连接（读 promise 一直悬着）。 */
function silentResponse() {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({ read: () => new Promise<never>(() => {}) }),
    },
  }
}

/** 一条会依次吐出这些块的流，然后继续悬着（模拟长连接）。 */
function dripResponse(chunks: string[]) {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => {
        let i = 0
        return {
          read: async () => {
            if (i < chunks.length) return { done: false, value: encoder.encode(chunks[i++]) }
            return await new Promise<never>(() => {}) // 吐完就悬着，不 EOF
          },
        }
      },
    },
  }
}

/**
 * 一条**持续**每 `intervalMs` 吐一条心跳的流（吐满 `count` 条后悬着）。
 *
 * 必须让读操作落在**假定时器**上：否则所有块会在首个微任务里被一口气读光，
 * 之后就是"静默" —— 那测的就不是心跳保活，而是静默超时了。
 */
function heartbeatResponse(intervalMs = 1_000, count = 120) {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => {
        let i = 0
        return {
          read: async () => {
            if (i++ >= count) return await new Promise<never>(() => {})
            await new Promise((resolve) => setTimeout(resolve, intervalMs))
            return { done: false, value: encoder.encode(': ping\n\n') }
          },
        }
      },
    },
  }
}

function headersOf(call: Call): Record<string, string> {
  return (call.init.headers ?? {}) as Record<string, string>
}

afterEach(() => {
  delete (globalThis as { chironDesktop?: Host }).chironDesktop
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('静默到阈值 ⇒ 判定链路已死并重连（半开连接不再永远挂着）', async () => {
  vi.useFakeTimers()
  const mod = await loadApi()
  // 首连给一条有 id 的帧（证明重连会带游标），之后换一条彻底静默的流
  const calls = stubFetch(
    () => dripResponse(['id: 900-1\ndata: {"type":"text"}\n\n']),
    () => silentResponse(),
    () => silentResponse(),
    () => silentResponse(),
  )
  const onError = vi.fn()

  const handle = mod.openEventStream('http://x/events', vi.fn(), onError, {
    autoReconnect: true,
    maxAutoReconnects: 3,
    heartbeatTimeoutMs: 3_000,
  })

  // 先让首连把那一帧收下
  await vi.advanceTimersByTimeAsync(1)
  expect(calls).toHaveLength(1)

  // 静默 3s 以上：看门狗（阈值 1/3）应当主动断开并重连
  await vi.advanceTimersByTimeAsync(4_000)
  expect(calls.length).toBeGreaterThanOrEqual(2)
  // 重连带 Last-Event-ID（缺口补发靠它）
  expect(headersOf(calls[1]!)['Last-Event-ID']).toBe('900-1')
  expect(onError).not.toHaveBeenCalled()

  handle.close()
})

it('心跳持续到达 ⇒ 不重连（健康链路不被看门狗误杀）', async () => {
  vi.useFakeTimers()
  const mod = await loadApi()
  // 每 1s 一条心跳注释（`: ping`），与网关同形
  const calls = stubFetch(() => heartbeatResponse(1_000))

  const handle = mod.openEventStream('http://x/events', vi.fn(), vi.fn(), {
    autoReconnect: true,
    heartbeatTimeoutMs: 3_000,
  })

  try {
    for (let i = 0; i < 10; i++) await vi.advanceTimersByTimeAsync(1_000)
    expect(calls).toHaveLength(1) // 一次都没重连
  } finally {
    handle.close()
  }
})

it('后台恢复（online）且已静默 ⇒ 立刻重连，不等下一拍', async () => {
  vi.useFakeTimers()
  const mod = await loadApi()
  const calls = stubFetch(() => silentResponse())

  const handle = mod.openEventStream('http://x/events', vi.fn(), vi.fn(), {
    autoReconnect: true,
    heartbeatTimeoutMs: 60_000, // 阈值很大：只有 online 这一条路径会触发重连
  })

  try {
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(1)

    // 让"静默时长"超过阈值，再模拟网络恢复
    vi.setSystemTime(Date.now() + 61_000)
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(2) // 立刻重连（没有等 60s 的下一拍）
  } finally {
    handle.close()
  }
})

it('online 但链路健康（最近刚收到字节）⇒ 不重连（不浪费一帧补发）', async () => {
  vi.useFakeTimers()
  const mod = await loadApi()
  const calls = stubFetch(() => dripResponse([': ping\n\n']))

  const handle = mod.openEventStream('http://x/events', vi.fn(), vi.fn(), {
    autoReconnect: true,
    heartbeatTimeoutMs: 60_000,
  })

  await vi.advanceTimersByTimeAsync(1)
  window.dispatchEvent(new Event('online'))
  await vi.advanceTimersByTimeAsync(1)

  expect(calls).toHaveLength(1)
  handle.close()
})

it('未开启看门狗时行为不变（旧调用方零影响）', async () => {
  vi.useFakeTimers()
  const mod = await loadApi()
  const calls = stubFetch(() => silentResponse())

  const handle = mod.openEventStream('http://x/events', vi.fn(), vi.fn(), { autoReconnect: true })
  await vi.advanceTimersByTimeAsync(120_000)

  expect(calls).toHaveLength(1) // 静默不触发任何重连
  handle.close()
})

it('会话流的静默阈值 = 3× 网关心跳（跟随服务端，不自己发明节奏）', async () => {
  const mod = await loadApi()
  expect(mod.SSE_HEARTBEAT_MS).toBe(15_000)
  expect(mod.SSE_SILENT_TIMEOUT_MS).toBe(mod.SSE_HEARTBEAT_MS * 3)
})
