/**
 * 流式正文/思考的累积与落条（从 `ChatView.vue` 抽出的第三刀）。
 *
 * 这一层只做**纯文本到 ChatItem 的投影**，不关心 SSE 帧、审批、路由或网络：
 *  - `onTextChunk`：把增量累进**本会话**的 `streamBuf`，用 loose 状态机拆
 *    `[thinking]…[/thinking]`，原文/思考各归一条 `ChatItem`；
 *  - `onThinkingChunk`：native reasoning 走独立事件与独立条目，**绝不进 `streamBuf`**
 *    —— 否则 splitThinking 会把它当正文，刚分开的两条通道又被合回去；
 *  - `flushStreamingFlags`：轮次结束时把 streaming/running 收口并清空缓冲；
 *  - `appendUserText` / `genItemId`：用户消息插入与稳定 id（虚拟列表 key）。
 *
 * 为什么值得单独抽：这里每一条都对应真实事故（思考混进正文、两条通道互相覆盖、
 * 后台会话污染当前缓冲），而它们与视图状态无关，可以脱离 ChatView 单测。
 */
import type { Ref } from 'vue'

import type { ChatAttachment, ChatItem, TextItem } from '../components/chat/chat-types'
import { splitThinking } from '../components/chat/chat-types'
import type { SessionRunState } from './useChatRunState'

export interface ChatStreamTextPort {
  /** 当前写入作用域下的 items（SSE 回写时是事件所属会话的切片）。 */
  items: Ref<ChatItem[]>
  /** 当前写入作用域下的 run state（含 streamBuf / 三条流式条目 id）。 */
  streamState: () => SessionRunState
}

export function useChatStreamText({ items, streamState }: ChatStreamTextPort) {
  // 性能/正确性：稳定 id（虚拟列表 key + 流式定位，loadEarlier 头部插入不错位）。
  let itemIdSeq = 0
  function genItemId(): string {
    return `msg_${Date.now().toString(36)}_${itemIdSeq++}`
  }

  function resetStreamState(): void {
    const st = streamState()
    st.streamBuf = ''
    st.streamTextId = ''
    st.streamReasonId = ''
    st.streamStatsId = ''
  }

  function appendUserText(text: string, attachments?: ChatAttachment[]): void {
    items.value.push({ kind: 'text', role: 'user', content: text, id: genItemId(), attachments })
  }

  /**
   * native reasoning（独立 `thinking` 事件）的累积。
   *
   * 事件是**增量**（与 `onTextChunk` 的"从累积文本重算"不同），所以直接 `+=`。
   */
  function onThinkingChunk(text: string): void {
    if (!text) return
    const st = streamState()
    const existing = items.value.find(it => it.id === st.streamReasonId)
    if (existing?.kind === 'reasoning') {
      existing.content += text
      return
    }
    const id = genItemId()
    st.streamReasonId = id
    items.value.push({ kind: 'reasoning', content: text, streaming: true, id })
  }

  function onTextChunk(text: string): void {
    const st = streamState()
    st.streamBuf += text
    // 引擎按 ~80 字分段下发 "[thinking]片段[/thinking]"：多段思考全部归 reasoning，
    // 正文不再残留 [/thinking][thinking] 标签。
    const { reasoning, body } = splitThinking(st.streamBuf, { loose: true })
    if (reasoning) {
      const existing = items.value.find(it => it.id === st.streamReasonId)
      if (existing?.kind === 'reasoning') {
        existing.content = reasoning
      } else {
        const id = genItemId()
        st.streamReasonId = id
        items.value.push({ kind: 'reasoning', content: reasoning, streaming: true, id })
      }
    }
    if (body) {
      const existing = items.value.find(it => it.id === st.streamTextId)
      if (existing?.kind === 'text' && existing.role === 'assistant') {
        ;(existing as TextItem).content = body
      } else {
        const id = genItemId()
        st.streamTextId = id
        items.value.push({ kind: 'text', role: 'assistant', content: body, streaming: true, id })
      }
    }
  }

  /** 轮次收口：流式标记落地、运行中的工具置 done、清空本会话缓冲。 */
  function flushStreamingFlags(): void {
    for (const it of items.value) {
      if (it.kind === 'text' && it.streaming) it.streaming = false
      if (it.kind === 'reasoning' && it.streaming) it.streaming = false
      if (it.kind === 'tool_call' && it.status === 'running') it.status = 'done'
    }
    resetStreamState()
  }

  return {
    genItemId,
    resetStreamState,
    appendUserText,
    onThinkingChunk,
    onTextChunk,
    flushStreamingFlags,
  }
}
