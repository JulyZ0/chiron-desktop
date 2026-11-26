/**
 * ChatView 的 SSE 事件分发器（P0-1 收尾）。
 *
 * 前四刀把"状态所有权"（runtimes / 文本投影）与"纯投影"（工具卡/清单/用量）拿出去后，
 * 剩下的 `onSSEMessage` 是一张**事件类型 → 生命周期动作**的分发表。它仍然依赖视图的
 * 十余个 action（收尾、计费刷新、收件箱唤醒、补拉、审批 timer），所以这里不 import
 * 视图，而是用一份**显式端口**注入：依赖清单就是接口，谁新增依赖一眼可见。
 *
 * 判据：`ChatView.onSSEMessage` 只剩一行调用；事件分支全部落在本文件，且可用假端口
 * 单测（不需要挂载 ChatView）。
 */
import type { Ref } from 'vue'

import type { ChatItem } from '../components/chat/chat-types'
import { normalizeMeta } from '../components/chat/chat-history'
import type { SubagentEvent } from '../api/subagent'
import type { SessionRunState } from './useChatRunState'
import {
  completionNotice,
  projectCompaction,
  projectCompletion,
  projectKnowledgeRetrieval,
  projectSteerNotice,
  projectTodoEvent,
  projectToolCall,
  projectToolResult,
  projectUsage,
} from '../components/chat/sseProjection'
import type { CompactionInfo } from '../components/chat/sseProjection'

type Obj = Record<string, unknown>

export interface BudgetSnapshot {
  used?: number
  limit?: number
}
export interface ContextSnapshot {
  used?: number
  limit?: number
  compact_ratio?: number
  breakdown?: Record<string, number>
  largest?: Array<{ class?: string; role?: string; tokens?: number }>
  estimated?: boolean
}
export interface CacheSnapshot {
  prefix_changed?: string[]
  first_turn?: boolean
}

export interface ChatSsePorts {
  // ── 投影 ──
  items: Ref<ChatItem[]>
  streamState: () => SessionRunState
  genItemId: () => string
  runOf: (sid: string) => SessionRunState
  onTextChunk: (text: string) => void
  onThinkingChunk: (text: string) => void
  flushStreamingFlags: () => void
  applyTodos: (sid: string, todos: unknown[], revision: unknown) => void
  mergeStats: (
    items: ChatItem[],
    id: string,
    usage: { inputTokens: number; outputTokens: number; durationMs: number },
    genId: () => string,
  ) => string
  // ── 生命周期 ──
  loading: { value: boolean }
  stopTurnTimer: () => void
  closeSessionStream: (sid: string) => void
  refreshInbox: () => void
  flushPendingInboxWake: () => void
  clearPendingInboxWake: () => void
  scheduleGapResync: (sid: string) => void
  flushGapResync: (sid: string) => void
  refreshSessionCost: () => void
  // ── 交互票据 ──
  interactionScope: {
    addApproval: (ticket: Obj) => void
    addQuestion: (ticket: Obj) => void
  }
  ensureApprovalTimer: () => void
  ensureAskTimer: () => void
  approvalTimeoutMs: number
  askTimeoutMs: number
  // ── 遥测快照 ──
  currentTraceId: Ref<string>
  turnElapsed: Ref<number>
  lastCompaction: Ref<CompactionInfo | null>
  budgetSnapshot: Ref<BudgetSnapshot | null>
  contextSnapshot: Ref<ContextSnapshot | null>
  cacheSnapshot: Ref<CacheSnapshot | null>
  // ── 子 Agent ──
  pushSubagentEvent: (event: SubagentEvent, frameId: string) => void
  // ── UI ──
  notify: { warning: (msg: string) => void; error: (msg: string) => void }
  offerSetupTour: (reason: 'provider-auth', message: string, tone: 'warning' | 'error') => void
  actionableStreamError: (raw: string) => string
  isProviderAuthError: (raw: string) => boolean
  t: (key: string, params?: Record<string, unknown>) => string
}

export function createSseDispatcher(ports: ChatSsePorts) {
  return function onSSEMessage(raw: unknown, sid: string, frameId = ''): void {
    const evt = (raw ?? {}) as Obj
    const type = evt.type
    const d = (evt.data ?? {}) as Obj

    if (type === 'text') {
      const text = String(d.content ?? evt.content ?? '')
      if (text) ports.onTextChunk(text)
    } else if (type === 'thinking') {
      ports.onThinkingChunk(String(d.content ?? evt.content ?? ''))
    } else if (type === 'tool_call') {
      projectToolCall(ports.items.value, d)
    } else if (type === 'tool_result') {
      projectToolResult(ports.items.value, d)
    } else if (type === 'todo_updated') {
      projectTodoEvent(sid, d, evt, ports.applyTodos)
    } else if (type === 'completion') {
      projectCompletion(
        ports.items.value,
        String(d.content ?? evt.content ?? ''),
        raw => completionNotice(raw, ports.t),
      )
    } else if (type === 'usage' || type === 'turn_stats') {
      projectUsage(ports.items.value, ports.streamState(), d, evt, ports.mergeStats, ports.genItemId)
    } else if (type === 'steer') {
      projectSteerNotice(ports.items.value, d.content ?? evt.content, ports.t('common.steer_applied'))
    } else if (type === 'knowledge_retrieval') {
      projectKnowledgeRetrieval(ports.items.value, d, evt)
    } else if (type === 'compaction') {
      projectCompaction(
        ports.items.value,
        d,
        evt,
        info => { ports.lastCompaction.value = info },
        (before, after) => ports.t('common.compressed_before_after', { before, after }),
      )
    } else if (type === 'done') {
      // `done` 是**引擎一段**的终态，不是整条任务；链级收尾是 `turn_done`。
      ports.flushStreamingFlags()
      ports.currentTraceId.value = String(d.trace_id || '')
      const doneMeta = normalizeMeta(d.metadata)
      const doneStreamId = ports.streamState().streamTextId
      if (doneMeta && Object.keys(doneMeta).length && doneStreamId) {
        const streamItem = ports.items.value.find(x => x.id === doneStreamId)
        if (streamItem?.kind === 'text' && streamItem.role === 'assistant') {
          streamItem.metadata = { ...(streamItem.metadata || {}), ...doneMeta }
        }
      }
      const it = Number(d.input_tokens ?? 0) || 0
      const ot = Number(d.output_tokens ?? 0) || 0
      if (it || ot) {
        ports.items.value.push({
          kind: 'turn_stats',
          inputTokens: it,
          outputTokens: ot,
          durationSec: ports.turnElapsed.value,
        })
      }
      ports.budgetSnapshot.value = (d.budget && typeof d.budget === 'object')
        ? (d.budget as Record<string, BudgetSnapshot>)
        : null
      ports.contextSnapshot.value = (d.context && typeof d.context === 'object')
        ? (d.context as ContextSnapshot)
        : null
      ports.cacheSnapshot.value = (d.cache && typeof d.cache === 'object')
        ? (d.cache as CacheSnapshot)
        : null
      void ports.refreshSessionCost()
    } else if (type === 'turn_done') {
      ports.flushStreamingFlags()
      ports.loading.value = false
      ports.stopTurnTimer()
      ports.closeSessionStream(sid)
      ports.refreshInbox()
      ports.flushPendingInboxWake()
      ports.flushGapResync(sid)
    } else if (type === 'approval') {
      const callId = String(d.id ?? d.tool_call_id ?? Date.now())
      ports.interactionScope.addApproval({
        id: callId,
        toolName: String(d.name ?? 'tool'),
        arguments: String(d.arguments ?? ''),
        expiresAt: Date.now() + ports.approvalTimeoutMs,
        sessionId: sid,
        runGen: ports.runOf(sid).gen,
      })
      ports.ensureApprovalTimer()
    } else if (type === 'ask') {
      ports.interactionScope.addQuestion({
        id: String(d.id ?? d.tool_call_id ?? Date.now()),
        question: String(d.question ?? d.content ?? ports.t('common.needs_your_confirmation')),
        options: Array.isArray(d.options) ? d.options.map((option: unknown) => String(option)) : [],
        allowFreeText: d.allow_free_text !== false,
        expiresAt: Date.now() + ports.askTimeoutMs,
        sessionId: sid,
        runGen: ports.runOf(sid).gen,
      })
      ports.ensureAskTimer()
    } else if (type === 'stream_gap') {
      ports.scheduleGapResync(sid)
    } else if (type === 'guardrail_blocked') {
      ports.flushStreamingFlags()
      ports.loading.value = false
      ports.stopTurnTimer()
      ports.closeSessionStream(sid)
      ports.clearPendingInboxWake()
      const reason = typeof d.content === 'string' && d.content
        ? d.content
        : ports.t('admin.request_blocked_by_security_policy')
      ports.items.value.push({
        kind: 'notice',
        tone: 'warning',
        content: reason,
        id: `guard-${String(d.id ?? Date.now())}`,
      })
      ports.notify.warning(reason)
    } else if (type === 'error') {
      ports.flushStreamingFlags()
      ports.loading.value = false
      ports.stopTurnTimer()
      ports.closeSessionStream(sid)
      ports.clearPendingInboxWake()
      const rawText = typeof d.content === 'string' && d.content
        ? d.content
        : (typeof d.error === 'string' && d.error ? d.error : '')
      if (rawText && ports.isProviderAuthError(rawText)) {
        ports.offerSetupTour('provider-auth', ports.actionableStreamError(rawText), 'error')
      } else {
        ports.notify.error(
          rawText ? ports.actionableStreamError(rawText) : ports.t('errors.request_failed_2'),
        )
      }
    } else if (typeof type === 'string' && type.startsWith('subagent.')) {
      const event = (Object.keys(d).length ? { ...d, type } : { ...evt }) as unknown as SubagentEvent
      ports.pushSubagentEvent(event, frameId)
    }
  }
}
