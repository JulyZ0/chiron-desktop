/**
 * 子 Agent 通道的**归属与回放**（H59）。
 *
 * 与 `useSubagentStream.spec.ts`（单写者 / 合批 / 卸载冲刷）互补，这里钉的是 H59 那三条：
 *
 * 1. **"主流开着"按会话判定**：旧代码读的是 ChatView 的全局单句柄 —— 只要**任一**会话
 *    的主流动着，所有会话的子 Agent 事件就被丢掉（后台子 Agent 的进度/审批/提问全消失）；
 * 2. **按 run 去重**：重连补发与实时推送会给同一帧各送一份 ⇒ 不能落两遍；
 * 3. **Last-Event-ID 回放**：重建连接（切走再回来 / 断线）时带上本会话的游标。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api', () => ({
  createSSEConnection: vi.fn(() => ({ close: vi.fn() })),
}))

import { createSSEConnection } from '../../api'
import { SUBAGENT_FLUSH_MS, useSubagentStream, type SubagentRunStateLike } from '../useSubagentStream'

const createMock = vi.mocked(createSSEConnection)

function setup() {
  const sessionId = { value: 's1' }
  const run: SubagentRunStateLike = { subagentEvents: [] }
  /** 哪些会话的主流动着（真实实现里是 ChatView 的 sseBySession） */
  const openTurnSessions = new Set<string>()
  const stream = useSubagentStream({
    sessionId: () => sessionId.value,
    resolveRun: () => run,
    isTurnStreamOpen: (sid?: string) => (sid ? openTurnSessions.has(sid) : openTurnSessions.size > 0),
  })
  return { sessionId, run, stream, openTurnSessions }
}

/** 最近一次建连拿到的 onMessage / opts */
function lastCall() {
  const call = createMock.mock.calls[createMock.mock.calls.length - 1]!
  return {
    onMessage: call[1] as (raw: unknown, meta?: { id: string }) => void,
    opts: call[3] as { autoReconnect?: boolean; initialLastEventId?: string } | undefined,
  }
}

function evt(type = 'subagent.progress', runId = 'run-1') {
  return { type, run_id: runId }
}

describe('子 Agent 通道：按会话判归属、按 run 回放（H59）', () => {
  beforeEach(() => {
    createMock.mockClear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('别的会话的主流动着，不该吞掉本会话的子 Agent 事件', () => {
    const { stream, run, openTurnSessions } = setup()
    const { onMessage } = lastCall()

    // 会话 s9 的一轮流开着（旧实现：全局单句柄非空 ⇒ 所有会话的事件都被丢掉）
    openTurnSessions.add('s9')
    onMessage(evt('subagent.progress'), { id: '100-1' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(1)
    stream.disposeSubagentStream()
  })

  it('本会话主流动着时仍然跳过（同一帧不该被两条连接各送一份）', () => {
    const { stream, run, openTurnSessions } = setup()
    const { onMessage } = lastCall()

    openTurnSessions.add('s1')
    onMessage(evt('subagent.progress'), { id: '100-2' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(0)
    stream.disposeSubagentStream()
  })

  it('按 run 去重：补发窗口与实时推送重叠时只落一次', () => {
    const { stream, run, openTurnSessions } = setup()
    const { onMessage } = lastCall()
    openTurnSessions.clear()

    // 同一条逻辑事件被送达两次（多实例扇出 / 重连补发与实时重叠）
    onMessage(evt('subagent.approval'), { id: '200-7' })
    onMessage(evt('subagent.approval'), { id: '200-7' })
    onMessage(evt('subagent.progress'), { id: '200-8' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(2)
    stream.disposeSubagentStream()
  })

  it('去重窗口按 run 分开：另一个 run 的同号帧不受影响', () => {
    const { stream, run, openTurnSessions } = setup()
    const { onMessage } = lastCall()
    openTurnSessions.clear()

    onMessage(evt('subagent.progress', 'run-1'), { id: '300-1' })
    onMessage(evt('subagent.progress', 'run-2'), { id: '300-1' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(2)
    stream.disposeSubagentStream()
  })

  it('没有帧 id 时不猜去重（宁可重复一帧，也不静默吞掉真事件）', () => {
    const { stream, run, openTurnSessions } = setup()
    const { onMessage } = lastCall()
    openTurnSessions.clear()

    onMessage(evt('subagent.progress'))
    onMessage(evt('subagent.progress'))
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    expect(run.subagentEvents).toHaveLength(2)
    stream.disposeSubagentStream()
  })

  it('重建连接时带上本会话的 Last-Event-ID（断线/切走再回来的缺口回放）', () => {
    const { stream, openTurnSessions } = setup()
    openTurnSessions.clear()
    const first = lastCall()
    first.onMessage(evt('subagent.progress'), { id: '400-11' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)

    // 断开重建（同一条会话：dispose 后 ensure 就是"重连"这条路）
    stream.disposeSubagentStream()
    stream.ensureSubagentStream()

    expect(createMock).toHaveBeenCalledTimes(2)
    expect(lastCall().opts?.initialLastEventId).toBe('400-11')
    // 首次建连没有游标 ⇒ 选项对象保持原样（不带空值头）
    expect(createMock.mock.calls[0]![3]).toEqual({ autoReconnect: true })
    stream.disposeSubagentStream()
  })

  it('被跳过的帧也要推进游标（否则下次重连会把已交出去的这段再补一遍）', () => {
    const { stream, openTurnSessions } = setup()
    const { onMessage } = lastCall()

    openTurnSessions.add('s1') // 本会话主流开着 ⇒ 这一帧交给主流处理
    onMessage(evt('subagent.progress'), { id: '500-3' })
    vi.advanceTimersByTime(SUBAGENT_FLUSH_MS)
    openTurnSessions.clear()

    stream.disposeSubagentStream()
    stream.ensureSubagentStream()

    expect(lastCall().opts?.initialLastEventId).toBe('500-3')
    stream.disposeSubagentStream()
  })
})
