/** 断线重连后的恢复决策（PRD 9.2 / H60）。
 *
 * 缺陷形态（ChatView）：`attemptReconnect()` 在 `/health` 通过之后调用
 * `switchSession(activeSessionId.value)` —— 而 `switchSession` 第一行就是
 * `if (id === activeSessionId.value) return`。于是重连"成功"了，但：
 *   · 断线期间产生的历史**没有重拉**（界面停在旧内容，用户以为是模型卡住）；
 *   · 如果断线时正好有一轮在跑，那条流**没有被重新挂上**（即使服务端还在生成）。
 *
 * 这里把"重连后该做什么"抽成纯函数，便于钉住每种组合，也避免把判断散在回调里。
 */

export interface ReconnectPlan {
  /** 是否重拉当前会话历史（断线期间可能有新消息/新结果） */
  refreshHistory: boolean
  /** 是否重新挂上 SSE（断线时有回合在跑才需要） */
  reattachStream: boolean
  /** 重挂时带上的 `Last-Event-ID`（空串 = 从当前位置重开） */
  sinceEventId: string
}

export interface ReconnectState {
  /** 当前有没有激活的会话 */
  sessionId: string
  /** 断线前这一轮是否仍在生成 */
  wasLoading: boolean
  /** 该会话最后收到的事件 id（服务端按它补发缺口） */
  lastEventId: string
}

export function planReconnect(state: ReconnectState): ReconnectPlan {
  const hasSession = Boolean(state.sessionId)
  if (!hasSession) {
    // 没有会话：什么也不用做（不要凭空建会话）。
    return { refreshHistory: false, reattachStream: false, sinceEventId: '' }
  }
  return {
    // 一定要重拉：这正是"同 ID 直接返回"漏掉的那一步。
    refreshHistory: true,
    // 只有在跑的时候才重挂：空闲会话重挂会白占一条连接。
    reattachStream: Boolean(state.wasLoading),
    sinceEventId: state.wasLoading ? String(state.lastEventId || '') : '',
  }
}
