import { describe, expect, it } from 'vitest'
import { ref } from 'vue'

import { useChatStreamText } from '../useChatStreamText'
import type { ChatItem, ChatAttachment } from '../../components/chat/chat-types'
import type { SessionRunState } from '../useChatRunState'

/**
 * 流式正文/思考投影。
 *
 * 这里每条断言都对应一次真实事故：思考混进正文、两条通道互相覆盖、
 * 轮次结束后 streaming/running 不收口。
 */
function setup() {
  const items = ref<ChatItem[]>([])
  const state: SessionRunState = {
    items: [],
    loading: false,
    gen: 1,
    turnSeq: 0,
    subagentEvents: [],
    streamBuf: '',
    streamTextId: '',
    streamReasonId: '',
    streamStatsId: '',
  }
  const api = useChatStreamText({ items, streamState: () => state })
  return { items, state, api }
}

describe('useChatStreamText：流式正文与思考', () => {
  it('文本增量累加进同一条 assistant 条目', () => {
    const { items, state, api } = setup()
    api.onTextChunk('你')
    api.onTextChunk('好')
    expect(items.value).toHaveLength(1)
    const item = items.value[0]
    expect(item.kind).toBe('text')
    if (item.kind !== 'text') return
    expect(item.role).toBe('assistant')
    expect(item.content).toBe('你好')
    expect(item.streaming).toBe(true)
    expect(state.streamBuf).toBe('你好')
  })

  it('loose [thinking] 拆成 reasoning + 正文，两条各自累积', () => {
    const { items, state, api } = setup()
    api.onTextChunk('[thinking]为什么')
    api.onTextChunk('[/thinking]结论')
    const reasoning = items.value.find(it => it.kind === 'reasoning')
    const text = items.value.find(it => it.kind === 'text')
    expect(reasoning?.kind === 'reasoning' && reasoning.content).toBe('为什么')
    expect(text?.kind === 'text' && text.content).toBe('结论')
    // 缓冲保存的是原始累积文本（拆分只在投影层），但正文条目里不含标签
    expect(state.streamBuf).toContain('[thinking]')
    expect(text?.kind === 'text' && text.content).not.toContain('[thinking]')

    api.onTextChunk('，补充')
    expect(text?.kind === 'text' && text.content).toBe('结论，补充')
  })

  it('native thinking 事件独立累积，绝不进正文缓冲', () => {
    const { items, state, api } = setup()
    api.onThinkingChunk('想')
    api.onThinkingChunk('法')
    const reasoning = items.value.find(it => it.kind === 'reasoning')
    expect(reasoning?.kind === 'reasoning' && reasoning.content).toBe('想法')
    expect(state.streamBuf).toBe('')
    expect(items.value.some(it => it.kind === 'text')).toBe(false)
  })

  it('收口：流式标记落地、运行中的工具置 done、缓冲清空', () => {
    const { items, state, api } = setup()
    api.onTextChunk('答')
    items.value.push({
      kind: 'tool_call',
      id: 'tc1',
      name: 'read_file',
      arguments: '{}',
      status: 'running',
    } as ChatItem)
    api.flushStreamingFlags()

    for (const it of items.value) {
      if (it.kind === 'text') expect(it.streaming).toBe(false)
      if (it.kind === 'tool_call') expect(it.status).toBe('done')
    }
    expect(state.streamBuf).toBe('')
    expect(state.streamTextId).toBe('')
  })

  it('appendUserText 插入用户消息；genItemId 稳定且唯一', () => {
    const { items, api } = setup()
    const attachments = [{ kind: 'image', name: 'a.png' }] as unknown as ChatAttachment[]
    api.appendUserText('问题', attachments)
    const first = items.value[0]
    expect(first.kind === 'text' && first.role).toBe('user')
    expect(first.kind === 'text' && first.content).toBe('问题')
    expect(first.kind === 'text' && first.attachments).toEqual(attachments)

    const ids = new Set([api.genItemId(), api.genItemId(), api.genItemId()])
    expect(ids.size).toBe(3)
  })
})
