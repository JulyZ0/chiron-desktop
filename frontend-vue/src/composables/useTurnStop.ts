/**
 * 「停止生成」的现场与判定（PRD 9.9 / H58：**停止与取消必须到达后端**）。
 *
 * 旧实现（ChatView.vue 的 `stopGeneration`）只关本地 SSE、置 `loading=false` 并给
 * 最后一条助手消息打 `stopped` 标记 —— 服务端的 run **完全不知道**用户已经喊停：
 * 它继续跑工具、继续写文件、继续计费。用户看到的是"已停止"，实际停不下来。
 * 相对的另一个极端也有害：发出取消就立刻当成功，run 还在收尾时界面已经允许「继续生成」，
 * 于是同一会话被两条链同时驱动。
 *
 * 因此这里把"停止"建模成一次**需要确认的请求**：
 *   idle ──按下停止──▶ stopping ──后端确认──▶ 收尾（收 loading / 关流 / 标 stopped）
 *                          └────失败────▶ failed（保留"还在跑"的事实，可直接重试）
 *
 * 关键不变量：
 *   1. **未确认不得当成已停止** —— `onStopped` 只在后端确认后调用一次；
 *   2. 取消目标是**这一次 run 所在的会话**（`turn.sessionId`），不是"此刻正在看的会话"
 *      —— 用户按下停止前可能已经切走了；
 *   3. 等待确认期间重复点击不重复下发（`stopping` 去重），失败后点击即重试。
 *
 * 纯依赖注入实现，故可脱离 ChatView 单测（见 `__tests__/useTurnStop.spec.ts`）。
 */

/** 一次 run 的现场：它是"这一轮"的身份，用来把取消打到正确的会话上。 */
export interface TurnRef {
  sessionId: string
  /** 本轮提交的 `client_msg_id`（本地幂等键，服务端据此去重同一轮提交） */
  clientMsgId: string
}

export type StopPhase = 'stopping' | 'failed'

export interface TurnStopState {
  /** 取消**打到**哪个会话（= 这一轮所在的会话） */
  sessionId: string
  clientMsgId: string
  phase: StopPhase
  /** 失败原因（面向排查的稳定标识，文案由调用方决定） */
  error?: string
  /** 第几次尝试（连点/失败重试累加） */
  attempts: number
}

export interface TurnStopDeps {
  /** 当前视图的会话 id（可能为空：没有会话就没有可停的东西） */
  activeSessionId: () => string
  /** 读该会话"这一轮"的现场 */
  turnOf: (sid: string) => TurnRef | undefined
  /** 读该会话"停止请求"的现场 */
  stopOf: (sid: string) => TurnStopState | undefined
  /** 写回停止现场（传 undefined 表示清空） */
  setStop: (sid: string, stop: TurnStopState | undefined) => void
  /** 清掉"这一轮"的现场（确认停止后） */
  clearTurn: (sid: string) => void
  /**
   * 真正下发取消。返回 `true` = 服务端**确认**已停止。
   * 抛错或返回 false 一律视为"未确认"。
   */
  interrupt: (sessionId: string) => Promise<boolean>
  /** 确认停止后的收尾（收 loading / 关 SSE / 给消息打 stopped 标记） */
  onStopped: (sid: string) => void
  /** 未确认时的告知（**不改** loading 与 stopped —— run 可能还在跑） */
  onFailed: (reason: StopPhase, sid: string) => void
}

/** 失败原因的稳定标识（调用方映射文案；不上屏的是这个常量，不是一句话） */
export const STOP_NOT_CONFIRMED = 'interrupt_not_confirmed'

/**
 * 请求停止当前轮次。返回 `true` 表示**已确认**停止（此时收尾已执行）。
 *
 * 幂等性由现场决定：`stopping` 期间重复调用直接返回 false（不重复下发）；
 * `failed` 状态下再次调用就是重试。
 */
export async function requestTurnStop(deps: TurnStopDeps): Promise<boolean> {
  const sid = deps.activeSessionId()
  if (!sid) return false

  const current = deps.stopOf(sid)
  // 等确认期间再点：不做第二次下发（后端会被打成一串 interrupt，且无法区分谁先到）
  if (current?.phase === 'stopping') return false

  const turn = deps.turnOf(sid)
  // 取消要打到**这一轮**的会话；没有现场（例如别的客户端发起的 run）就退回当前会话
  const target = turn?.sessionId || sid
  const state: TurnStopState = {
    sessionId: target,
    clientMsgId: turn?.clientMsgId || '',
    phase: 'stopping',
    attempts: (current?.attempts ?? 0) + 1,
  }
  deps.setStop(sid, state)

  let confirmed = false
  try {
    confirmed = (await deps.interrupt(target)) === true
  } catch {
    confirmed = false
  }

  if (!confirmed) {
    // 关键：**不**收 loading、**不**标记 stopped。run 还在跑是事实，
    // 界面谎称"已停止"会让用户以为文件不再被改、token 不再被烧。
    deps.setStop(sid, { ...state, phase: 'failed', error: STOP_NOT_CONFIRMED })
    deps.onFailed('failed', sid)
    return false
  }

  deps.setStop(sid, undefined)
  deps.clearTurn(sid)
  deps.onStopped(sid)
  return true
}
