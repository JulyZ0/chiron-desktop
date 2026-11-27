import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import { createSseDispatcher } from '../useChatSse'
import type { ChatSsePorts } from '../useChatSse'
import type { ChatItem } from '../../components/chat/chat-types'

/**
 * SSE 分发器（P0-1 收尾）：事件 → 生命周期动作。
 *
 * 这里只验证**接线**（谁被调用、带什么参数），不重复投影自身的断言
 * （那些在 `sseProjection.spec.ts`）。关键是 `done` / `turn_done` 的语义不同：
 * `done` 只收本段、`turn_done` 才关流与 loading。
 */
function makePorts(overrides: Partial<ChatSsePorts> = {}) {
  const items = ref<ChatItem[]>([])
  const calls = {
    close: vi.fn(),
    stopTimer: vi.fn(),
    flushWake: vi.fn(),
    clearWake: vi.fn(),
    scheduleGap: vi.fn(),
    flushGap: vi.fn(),
    refreshInbox: vi.fn(),
    refreshCost: vi.fn(),
    approvalTimer: vi.fn(),
    askTimer: vi.fn(),
    notifyError: vi.fn(),
    notifyWarning: vi.fn(),
    pushSubagent: vi.fn(),
    addApproval: vi.fn(),
    addQuestion: vi.fn(),
    setupTour: vi.fn(),
  }
  const ports: ChatSsePorts = {
    items,
    streamState: () => ({
      items: items.value,
      loading: false,
      gen: 1,
      turnSeq: 0,
      subagentEvents: [],
      streamBuf: '',
      streamTextId: 'txt-1',
      streamReasonId: '',
      streamStatsId: '',
    }),
    genItemId: () => 'gen-1',
    runOf: () => ({
      items: [],
      loading: false,
      gen: 7,
      turnSeq: 0,
      subagentEvents: [],
      streamBuf: '',
      streamTextId: '',
      streamReasonId: '',
      streamStatsId: '',
    }),
    onTextChunk: vi.fn(),
    onThinkingChunk: vi.fn(),
    flushStreamingFlags: vi.fn(),
    applyTodos: vi.fn(),
    mergeStats: () => '',
    loading: ref(true),
    stopTurnTimer: calls.stopTimer,
    closeSessionStream: calls.close,
    refreshInbox: calls.refreshInbox,
    flushPendingInboxWake: calls.flushWake,
    clearPendingInboxWake: calls.clearWake,
    scheduleGapResync: calls.scheduleGap,
    flushGapResync: calls.flushGap,
    refreshSessionCost: calls.refreshCost,
    interactionScope: { addApproval: calls.addApproval, addQuestion: calls.addQuestion },
    ensureApprovalTimer: calls.approvalTimer,
    ensureAskTimer: calls.askTimer,
    approvalTimeoutMs: 300_000,
    askTimeoutMs: 300_000,
    currentTraceId: ref(''),
    turnElapsed: ref(12),
    lastCompaction: ref(null),
    budgetSnapshot: ref(null),
    contextSnapshot: ref(null),
    cacheSnapshot: ref(null),
    verificationSnapshot: ref(null),
    pushSubagentEvent: calls.pushSubagent,
    notify: { warning: calls.notifyWarning, error: calls.notifyError },
    offerSetupTour: calls.setupTour,
    actionableStreamError: (raw: string) => `actionable:${raw}`,
    isProviderAuthError: (raw: string) => raw.includes('401'),
    t: (key: string) => key,
    ...overrides,
  }
  return { ports, items, calls }
}

describe('createSseDispatcher：事件 → 生命周期', () => {
  it('tool_call 同时通知视图侧（F4 记录本回合写过的文件）', () => {
    const { ports } = makePorts()
    const onToolCall = vi.fn()
    const onSSE = createSseDispatcher({ ...ports, onToolCall })
    onSSE({ type: 'tool_call', data: { id: 'tc1', name: 'write_file', arguments: '{"path":"a.ts"}' } }, 's1')
    expect(onToolCall).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'write_file', arguments: '{"path":"a.ts"}' }),
    )
  })

  it('tool_call / tool_result 走纯投影', () => {
    const { ports, items } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE({ type: 'tool_call', data: { id: 'tc1', name: 'read_file' } }, 's1')
    expect(items.value[0]).toMatchObject({ kind: 'tool_call', id: 'tc1', status: 'running' })
    onSSE({ type: 'tool_result', data: { tool_call_id: 'tc1', content: 'ok' } }, 's1')
    expect(items.value[1]).toMatchObject({ kind: 'tool_result', toolCallId: 'tc1' })
  })

  it('done 只收本段：写 turn_stats/快照，不关流、不落 loading', () => {
    const { ports, items, calls } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE(
      {
        type: 'done',
        data: { input_tokens: 10, output_tokens: 5, trace_id: 'tr1', budget: { tokens: { limit: 1 } } },
      },
      's1',
    )
    expect(items.value[0]).toMatchObject({ kind: 'turn_stats', inputTokens: 10, outputTokens: 5 })
    expect(ports.currentTraceId.value).toBe('tr1')
    expect(ports.budgetSnapshot.value).toMatchObject({ tokens: { limit: 1 } })
    expect(ports.loading.value).toBe(true)
    expect(calls.close).not.toHaveBeenCalled()
    expect(calls.refreshCost).toHaveBeenCalled()
  })

  it('turn_done 才收 loading、关来源流、补唤醒与 gap 重拉', () => {
    const { ports, calls } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE({ type: 'turn_done' }, 's1')
    expect(ports.loading.value).toBe(false)
    expect(calls.close).toHaveBeenCalledWith('s1')
    expect(calls.stopTimer).toHaveBeenCalled()
    expect(calls.refreshInbox).toHaveBeenCalled()
    expect(calls.flushWake).toHaveBeenCalled()
    expect(calls.flushGap).toHaveBeenCalledWith('s1')
  })

  it('approval 冻结 sessionId 与 runGen，并启动计时器', () => {
    const { ports, calls } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE({ type: 'approval', data: { id: 'a1', name: 'shell_exec', arguments: '{}' } }, 'sess-a')
    expect(calls.addApproval).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'a1', sessionId: 'sess-a', runGen: 7 }),
    )
    expect(calls.approvalTimer).toHaveBeenCalled()
  })

  it('error 关流并把可行动文案交给 notify；认证失败走引导', () => {
    const { ports, calls } = makePorts()
    const onSSE = createSseDispatcher(ports)

    onSSE({ type: 'error', data: { content: 'boom' } }, 's1')
    expect(calls.close).toHaveBeenCalledWith('s1')
    expect(calls.clearWake).toHaveBeenCalled()
    expect(calls.notifyError).toHaveBeenCalledWith('actionable:boom')

    onSSE({ type: 'error', data: { content: '401 unauthorized' } }, 's2')
    expect(calls.setupTour).toHaveBeenCalledWith(
      'provider-auth',
      'actionable:401 unauthorized',
      'error',
    )
  })

  it('stream_gap 只安排补拉；subagent.* 只进观测通道', () => {
    const { ports, calls } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE({ type: 'stream_gap' }, 's1')
    expect(calls.scheduleGap).toHaveBeenCalledWith('s1')

    onSSE({ type: 'subagent.text', data: { run_id: 'rs1', content: 'hi' } }, 's1', 'f1')
    expect(calls.pushSubagent).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'subagent.text', run_id: 'rs1' }),
      'f1',
    )
  })

  it('knowledge_retrieval 落徽标（口径来自结构化字段）', () => {
    const { ports, items } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE(
      {
        type: 'knowledge_retrieval',
        data: {
          content: JSON.stringify({
            kb_ids: ['kb1', 'kb2'],
            queried: 2,
            hits: 1,
            citations: 3,
            zero_hit: false,
            errors: [{ kb_id: 'kb2', category: 'timeout', message: 'slow' }],
          }),
        },
      },
      's1',
    )
    expect(items.value[0]).toMatchObject({
      kind: 'knowledge',
      kbIds: ['kb1', 'kb2'],
      queried: 2,
      hits: 1,
      citations: 3,
      zeroHit: false,
      errors: [{ kbId: 'kb2', category: 'timeout', message: 'slow' }],
    })
  })

  it('compaction 同时更新状态栏读数与 info 行（不是 warning）', () => {
    const { ports, items } = makePorts()
    const onSSE = createSseDispatcher(ports)
    onSSE(
      {
        type: 'compaction',
        data: { content: JSON.stringify({ saved_tokens: 4000, before_tokens: 12000, after_tokens: 8000 }) },
      },
      's1',
    )
    expect(ports.lastCompaction.value).toEqual({
      beforeTokens: 12000,
      afterTokens: 8000,
      savedTokens: 4000,
    })
    expect(items.value[0]).toMatchObject({ kind: 'notice', tone: 'info' })

    onSSE({ type: 'compaction', data: { content: '{}' } }, 's1')
    expect(items.value).toHaveLength(1)
  })
})
