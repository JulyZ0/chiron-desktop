/**
 * 子 Agent 实时通道（`useSubagentStream`）的回归防线。
 *
 * 这层从 ChatView 整块搬出来时是**纯搬运**，所以这里钉的不只是新文件的行为，而是
 * 「搬出来之后仍然只有一条通道」这件事：
 *
 * 1. **单写者**：同一会话重复 ensure 只建一条 SSE（通道收敛的判据），切会话才重建；
 * 2. **合批**：几十条事件只触发一次响应式写入（50ms 窗口），且有界截断；
 * 3. **过滤**：非 `subagent.*` / 无 run_id 的帧不进缓冲；
 * 4. **本轮 SSE 开着时跳过**：避免同一帧被两条连接各送一份；
 * 5. **卸载冲刷**：最后那批还没落盘的事件不能随组件一起消失。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api', () => ({
  createSSEConnection: vi.fn(() => ({ close: vi.fn() })),
  // 子 Agent 流现在也带静默看门狗，需要这个常量（值不影响本文件的断言）。
  SSE_SILENT_TIMEOUT_MS: 45_000,
}))

import { createSSEConnection } from '../../api'
import {
  SUBAGENT_FLUSH_MS,
  SUBAGENT_LIVE_MAX,
  useSubagentStream,
  type SubagentRunStateLike,
} from '../useSubagentStream'

const createMock = vi.mocked(createSSEConnection)

function setup(overrides: {
  sessionId?: string
  turnOpen?: boolean
} = {}) {
  const sessionId = { value: overrides.sessionId ?? 's1' }
  const run: SubagentRunStateLike = { subagentEvents: [] }
  let turnOpen = overrides.turnOpen ?? false
  const stream = useSubagentStream({
    sessionId: () => sessionId.value,
    resolveRun: () => run,
    isTurnStreamOpen: () => turnOpen,
  })
  return {
    sessionId,
    run,
    stream,
    setTurnOpen: (v: boolean) => { turnOpen = v },
  }
}

function evt(type = 'subagent.progress', runId = 'run-1') {
  return { type, run_id: runId }
}

describe('useSubagentStream', () => {
  beforeEach(() => {
    createMock.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('单写者：同一会话重复 ensure 只建一条连接', () => {
    const { stream } = setup()
    expect(createMock).toHaveBeenCalledTimes(1) // immediate 的首次建连

    stream.ensureSubagentStream()
    stream.ensureSubagentStream()

    expect(createMock).toHaveBeenCalledTimes(1)
    expect(createMock).toHaveBeenCalledWith(
      's1',
      expect.any(Function),
      expect.any(Function),
      expect.objectContaining({ autoReconnect: true, heartbeatTimeoutMs: 45_000 }),
    )
    stream.disposeSubagentStream()
  })

  it('切会话重建：先关旧连接再建新连接', () => {
    const { stream, sessionId } = setup()
    const first = createMock.mock.results[0].value as { close: ReturnType<typeof vi.fn> }

    sessionId.value = 's2'
    stream.ensureSubagentStream()

    expect(first.close).toHaveBeenCalled()
    expect(createMock).toHaveBeenCalledTimes(2)
    expect(createMock.mock.calls[1][0]).toBe('s2')
    stream.disposeSubagentStream()
  })

  it('无会话时不建连接（空串 = 未选中）', () => {
    const { stream } = setup({ sessionId: '' })
    expect(createMock).not.toHaveBeenCalled()
    stream.ensureSubagentStream()
    expect(createMock).not.toHaveBeenCalled()
  })

  it('50ms 合批：一批事件只落盘一次', () => {
    vi.useFakeTimers()
    const { stream, run } = setup()

    stream.pushSubagentEvent(evt())
    stream.pushSubagentEvent(evt('subagent.done'))
    expect(run.subagentEvents).toHaveLength(0) // 窗口内还没写

    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)
    expect(run.subagentEvents).toHaveLength(2)
    expect(run.subagentEvents[1].type).toBe('subagent.done')

    // 定时器已复位：下一批会重新开窗
    stream.pushSubagentEvent(evt())
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)
    expect(run.subagentEvents).toHaveLength(3)
    stream.disposeSubagentStream()
  })

  it('合批有界：超出上限只保留最新的 SUBAGENT_LIVE_MAX 条', () => {
    vi.useFakeTimers()
    const { stream, run } = setup()
    run.subagentEvents = Array.from({ length: SUBAGENT_LIVE_MAX }, () => evt() as never)

    stream.pushSubagentEvent(evt('subagent.latest'))
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(SUBAGENT_LIVE_MAX)
    expect(run.subagentEvents[SUBAGENT_LIVE_MAX - 1].type).toBe('subagent.latest')
    stream.disposeSubagentStream()
  })

  it('过滤：非 subagent.* 与缺 run_id 的帧都不进缓冲', () => {
    vi.useFakeTimers()
    const { stream, run } = setup()

    stream.pushSubagentEvent({ type: 'delta', run_id: 'run-1' })
    stream.pushSubagentEvent({ type: 'subagent.progress' })
    stream.pushSubagentEvent(null)
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(0)
    stream.disposeSubagentStream()
  })

  it('本轮主 SSE 开着时跳过处理（避免同一帧两条连接各送一份）', () => {
    vi.useFakeTimers()
    const { stream, run, setTurnOpen } = setup({ turnOpen: true })

    // 走真实连接回调：拿到 createSSEConnection 的 onMessage
    const onMessage = createMock.mock.calls[0][1] as (raw: unknown) => void
    onMessage(evt())
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)
    expect(run.subagentEvents).toHaveLength(0)

    setTurnOpen(false)
    onMessage(evt())
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)
    expect(run.subagentEvents).toHaveLength(1)
    stream.disposeSubagentStream()
  })

  it('卸载冲刷：窗口内未落盘的事件在 dispose 时写入并关连接', () => {
    const { stream, run } = setup()
    const handle = createMock.mock.results[0].value as { close: ReturnType<typeof vi.fn> }

    stream.pushSubagentEvent(evt())
    stream.disposeSubagentStream()

    expect(run.subagentEvents).toHaveLength(1)
    expect(handle.close).toHaveBeenCalled()
    // 关完之后再 ensure 会重建（不会被"已关闭"的陈旧标记挡住）
    stream.ensureSubagentStream()
    expect(createMock).toHaveBeenCalledTimes(2)
    stream.disposeSubagentStream()
  })

  it('连接回调里的 onError 会关掉通道，等下次切会话重建', () => {
    const { stream } = setup()
    const onError = createMock.mock.calls[0][2] as () => void
    const handle = createMock.mock.results[0].value as { close: ReturnType<typeof vi.fn> }

    onError()
    expect(handle.close).toHaveBeenCalled()

    stream.ensureSubagentStream()
    expect(createMock).toHaveBeenCalledTimes(2)
    stream.disposeSubagentStream()
  })
})
