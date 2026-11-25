/** 断线重连的恢复决策（PRD 9.2 / H60）。
 *
 * 缺陷形态（ChatView.attemptReconnect）：`/health` 通过后调用
 * `switchSession(activeSessionId.value)` —— 而 `switchSession` 开头就是
 * `if (id === activeSessionId.value) return`。于是重连"成功"了，但：
 *   · 断线期间的历史/工具结果**没有重拉**（界面停在旧内容，看起来像模型卡住）；
 *   · 断线时正在跑的那一轮，SSE **没有被重新挂上**（服务端可能还在生成）。
 */
import { describe, it, expect } from 'vitest'
import { planReconnect } from '../streamReattach'

describe('planReconnect (H60)', () => {
  it('没有会话时什么都不做', () => {
    expect(planReconnect({ sessionId: '', wasLoading: true, lastEventId: '7' })).toEqual({
      refreshHistory: false,
      reattachStream: false,
      sinceEventId: '',
    })
  })

  it('有会话就一定要重拉历史（这正是"同 ID 直接返回"漏掉的一步）', () => {
    const plan = planReconnect({ sessionId: 's1', wasLoading: false, lastEventId: '' })
    expect(plan.refreshHistory).toBe(true)
  })

  it('空闲会话不重挂流（否则白占一条连接）', () => {
    const plan = planReconnect({ sessionId: 's1', wasLoading: false, lastEventId: '12' })
    expect(plan.reattachStream).toBe(false)
    expect(plan.sinceEventId).toBe('')
  })

  it('跑着的会话要重挂，并带上最后事件 id 供服务端补发缺口', () => {
    const plan = planReconnect({ sessionId: 's1', wasLoading: true, lastEventId: '42' })
    expect(plan.reattachStream).toBe(true)
    expect(plan.sinceEventId).toBe('42')
  })

  it('跑着但没有事件 id：重挂但不带 since（从当前位置重开）', () => {
    const plan = planReconnect({ sessionId: 's1', wasLoading: true, lastEventId: '' })
    expect(plan.reattachStream).toBe(true)
    expect(plan.sinceEventId).toBe('')
  })
})
