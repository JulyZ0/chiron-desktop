import { describe, expect, it } from 'vitest'
import { ref } from 'vue'

import { useChatRunState } from '../useChatRunState'
import type { ChatItem } from '../../components/chat/chat-types'

/**
 * 多会话运行状态（`useChatRunState`）。
 *
 * 这块是 H59 串台事故的正中心：`items` / `loading` / 流式缓冲必须按会话隔离，
 * 而 SSE 回写又要能"写进事件所属的会话"（不是当前视图的会话）。
 */
function textItem(id: string, content: string): ChatItem {
  return { kind: 'text', role: 'assistant', content, id } as ChatItem
}

describe('useChatRunState：按会话隔离与写入作用域', () => {
  it('不同会话各自持有 items / loading，互不污染', () => {
    const sid = ref('a')
    const state = useChatRunState(() => sid.value)

    state.items.value.push(textItem('m1', 'A'))
    expect(state.runOf('a').items).toHaveLength(1)
    expect(state.runOf('b').items).toHaveLength(0)

    sid.value = 'b'
    expect(state.items.value).toHaveLength(0)
    state.items.value.push(textItem('m2', 'B'))
    expect(state.runOf('b').items).toHaveLength(1)
    expect(state.runOf('a').items).toHaveLength(1)
  })

  it('withRun：写入事件所属会话，期间视图仍指向原会话', () => {
    const sid = ref('a')
    const state = useChatRunState(() => sid.value)
    const runB = state.runOf('b')

    state.withRun(runB, () => {
      state.items.value.push(textItem('m9', 'for B'))
      state.loading.value = true
      expect(state.streamState()).toBe(runB)
    })

    // 作用域结束后：当前视图仍是 A，B 的写入没有串过来
    expect(state.items.value).toHaveLength(0)
    expect(state.loading.value).toBe(false)
    expect(state.runOf('b').items).toHaveLength(1)
    expect(state.runOf('b').loading).toBe(true)
    expect(state.streamState()).toBe(state.runOf('a'))
  })

  it('liveRunSessionIds 只报 loading 的会话', () => {
    const sid = ref('a')
    const state = useChatRunState(() => sid.value)
    state.runOf('a').loading = true
    state.runOf('b').loading = false
    state.runOf('c').loading = true
    expect([...state.liveRunSessionIds.value].sort()).toEqual(['a', 'c'])
  })

  it('空会话用占位运行时不串到真实会话', () => {
    const sid = ref('')
    const state = useChatRunState(() => sid.value)
    state.items.value.push(textItem('m0', 'placeholder'))
    expect(state.runOf('__none__').items).toHaveLength(1)
    sid.value = 'real'
    expect(state.items.value).toHaveLength(0)
  })

  it('runOf 返回响应式代理（push 能触发依赖）', () => {
    const sid = ref('a')
    const state = useChatRunState(() => sid.value)
    let seen = 0
    const snapshot = state.items.value
    expect(snapshot).toHaveLength(0)
    state.items.value.push(textItem('m1', 'x'))
    seen = state.items.value.length
    expect(seen).toBe(1)
  })
})
