/**
 * 审批 / 提问票据的**会话归属**（H59 + H61）。
 *
 * ## 为什么票据不能是"当前视图的一份列表"
 *
 * 这两类票（工具审批、`ask_user` 提问）此前是 ChatView 里的**两个全局 ref**：
 * `pendingApprovals` / `pendingQuestions`。于是有四处后果：
 *
 * 1. **串台**：会话 A 的流还在送 `approval`，用户切到 B，卡片就画在 B 的输入区上方 ——
 *    用户以为"B 在等我批准"，而 B 根本没跑；
 * 2. **误提交**：提交时读的是 `activeSessionId`（**当前视图**），不是票**属于**的会话。
 *    在 A 里出现、在 B 里点"允许"，批准就被发到 B 上 —— 引擎那边 A 继续等到超时；
 * 3. **丢票**：`switchSession` 会把两个数组一起清空（"tab activation clears stale approval"），
 *    可 A 的票**并没有作废**，切回去时它只剩在服务端 `pending_interactions` 里；
 * 4. **假成功**：提交失败被 `catch {}` 吞掉，卡片照样删掉（H61）。
 *
 * 所以本模块把票按 **session** 建图，并在**到达时**冻结归属（`sessionId` + 当时的运行代际
 * `runGen`）。提交只认票自己的归属；`claimTicket` 是唯一的放行判据 ——
 * 错配（会话不同 / 运行时已重建 / 已在提交中）一律拒绝，票原地保留。
 *
 * ## 为什么"代际"也要一起冻结
 *
 * 同一个会话 id 可以对应**前后两次运行时**（重开同一 id、重新建流）。旧回调送来的票属于
 * 上一次运行，把它的答复发出去等于替一次已经结束的运行做决定。
 */
import { reactive } from 'vue'
import type { PendingApproval } from '../../composables/useApprovalEdit'

/** 票据的两族：审批回布尔/参数，提问回答案文本 */
export type TicketKind = 'approval' | 'question'

/**
 * 待审批票据：`PendingApproval`（工具名 / 参数 / 到期）**加上不可变归属**。
 *
 * `busy` / `error`（H61）让"服务端还没确认"这件事在界面上可见：
 * 提交中卡片留着、按钮禁用；失败后卡片留着、错误写在卡片上、可以重试。
 */
export interface ScopedApproval extends PendingApproval {
  /** 不可变归属：票**到达时**属于哪个会话（提交只认它，绝不读当前视图会话） */
  sessionId: string
  /** 到达时该会话的运行代际（见文件头 §代际） */
  runGen: number
  /** 提交中（服务端未确认前票不摘） */
  busy?: boolean
  /** 上一次提交失败的原因；非空即"可重试" */
  error?: string
}

/** 待答提问票据（形状与 ChatView 模板一致，外加不可变归属与 busy/error） */
export interface ScopedQuestion {
  id: string
  question: string
  options: string[]
  allowFreeText: boolean
  /** 到期时刻；到点置 `expired` 但保留卡片与问题上下文 */
  expiresAt: number
  expired?: boolean
  /** 用户已开始作答（输入框有内容）：不再判超时 */
  drafting?: boolean
  sessionId: string
  runGen: number
  busy?: boolean
  error?: string
}

/** `claimTicket` 的结论。`ok:false` 时**不提交**、**不摘票**。 */
export type ClaimResult =
  | { ok: true }
  | { ok: false; reason: 'mismatch' | 'busy' | 'stale-run' | 'missing-owner' }

/** 提交时的现场：当前视图会话 + 该会话**此刻**的运行代际 */
export interface ClaimContext {
  sessionId: string
  gen: number
}

/**
 * 唯一的放行判据（**纯函数**，便于单测钉死）。
 *
 * 拒绝的三种情形都必须保留票据 —— 它们是"这次点错了/太早了"，不是"这张票已处理"：
 * - `missing-owner`：没有归属的票（旧数据/构造错误）不能猜一个会话提交；
 * - `mismatch`：票属于别的会话（用户已经切走，界面上的那份是陈旧的）；
 * - `stale-run`：票属于该会话**上一次**运行时（代际不符）；
 * - `busy`：已在提交中（连点两次不该发两次答复）。
 */
export function claimTicket(
  owner: { sessionId?: string; runGen?: number; busy?: boolean },
  ctx: ClaimContext,
): ClaimResult {
  if (!owner?.sessionId) return { ok: false, reason: 'missing-owner' }
  if (owner.sessionId !== ctx.sessionId) return { ok: false, reason: 'mismatch' }
  if (typeof owner.runGen === 'number' && owner.runGen !== ctx.gen) {
    return { ok: false, reason: 'stale-run' }
  }
  if (owner.busy) return { ok: false, reason: 'busy' }
  return { ok: true }
}

/** 冻结的空列表：无票会话读它（同一引用 ⇒ 不制造多余的响应式依赖） */
const EMPTY_APPROVALS: readonly ScopedApproval[] = Object.freeze([])
const EMPTY_QUESTIONS: readonly ScopedQuestion[] = Object.freeze([])

export interface NewApproval {
  id: string
  toolName: string
  arguments: string
  expiresAt: number
  sessionId: string
  runGen: number
}

export interface NewQuestion {
  id: string
  question: string
  options: string[]
  allowFreeText: boolean
  expiresAt: number
  sessionId: string
  runGen: number
}

/**
 * 按会话建图的票据仓库。
 *
 * 单实例归一个 ChatView 持有（与 `useSubagentStream` 同理：模块级单例会跨实例串台）。
 */
export interface InteractionScope {
  /** 某会话的待审批（模板读：当前视图会话） */
  approvalsOf(sessionId: string): readonly ScopedApproval[]
  /** 某会话的待答提问 */
  questionsOf(sessionId: string): readonly ScopedQuestion[]
  /** 全部会话的待审批数（提示音与"有票"判据：后台会话的票不该静音） */
  approvalCount(): number
  /** 全部会话的待答提问数 */
  questionCount(): number
  /** 全部待处理（审批 + 提问）的 id：与历史列表对账用（H61） */
  liveIds(): Set<string>
  /** 落一张审批票（同 id 重放就地更新，不重复） */
  addApproval(t: NewApproval): ScopedApproval
  /** 落一张提问票（同上） */
  addQuestion(t: NewQuestion): ScopedQuestion
  /** 标记提交中 / 结束提交中 */
  setBusy(kind: TicketKind, id: string, busy: boolean): void
  /** 提交失败：票保留 + 记原因（可重试） */
  markFailed(kind: TicketKind, id: string, reason: string): void
  /** 服务端确认后摘票 */
  settle(kind: TicketKind, id: string): void
  /** 审批过期清理（后端 300s 超时按拒绝处理）：返回剩余待审批数 */
  sweepApprovals(now: number): number
  /** 提问过期：置 `expired`、保留卡片；返回是否还有未过期的票（决定要不要停表） */
  sweepQuestions(now: number): boolean
  /** 切会话不再清空别人家的票，只清"已不属于任何运行时"的这一份（重开同一 id 时用） */
  clearSession(sessionId: string): void
  /** 卸载 / 认证切换：全部清掉 */
  clear(): void
  /** 卸载时的定时器复位判据：还有没有任何票 */
  isEmpty(): boolean
}

export function createInteractionScope(): InteractionScope {
  /**
   * ⚠️ 必须是**响应式**记录：模板与 computed 都从它派生，非响应式的话
   * "票来了却不显示、票答完了还挂着"。
   */
  const state = reactive({
    approvals: {} as Record<string, ScopedApproval[]>,
    questions: {} as Record<string, ScopedQuestion[]>,
  })

  function approvalsBucket(sid: string, create: boolean): ScopedApproval[] | undefined {
    const existing = state.approvals[sid]
    if (existing || !create) return existing
    const list: ScopedApproval[] = []
    state.approvals[sid] = list
    return list
  }

  function questionsBucket(sid: string, create: boolean): ScopedQuestion[] | undefined {
    const existing = state.questions[sid]
    if (existing || !create) return existing
    const list: ScopedQuestion[] = []
    state.questions[sid] = list
    return list
  }

  function eachApproval(fn: (t: ScopedApproval) => boolean): void {
    for (const sid of Object.keys(state.approvals)) {
      state.approvals[sid] = (state.approvals[sid] || []).filter(fn)
    }
  }

  function eachQuestion(fn: (t: ScopedQuestion) => boolean): void {
    for (const sid of Object.keys(state.questions)) {
      state.questions[sid] = (state.questions[sid] || []).filter(fn)
    }
  }

  const scope: InteractionScope = {
    approvalsOf: (sid) => (sid ? approvalsBucket(sid, false) || EMPTY_APPROVALS : EMPTY_APPROVALS),
    questionsOf: (sid) => (sid ? questionsBucket(sid, false) || EMPTY_QUESTIONS : EMPTY_QUESTIONS),

    approvalCount() {
      let n = 0
      for (const sid of Object.keys(state.approvals)) n += (state.approvals[sid] || []).length
      return n
    },
    questionCount() {
      let n = 0
      for (const sid of Object.keys(state.questions)) n += (state.questions[sid] || []).length
      return n
    },

    liveIds() {
      const out = new Set<string>()
      for (const sid of Object.keys(state.approvals)) {
        for (const t of state.approvals[sid] || []) out.add(t.id)
      }
      for (const sid of Object.keys(state.questions)) {
        for (const t of state.questions[sid] || []) out.add(t.id)
      }
      return out
    },

    addApproval(t) {
      const bucket = approvalsBucket(t.sessionId, true)!
      const ticket: ScopedApproval = {
        id: t.id,
        toolName: t.toolName,
        arguments: t.arguments,
        expiresAt: t.expiresAt,
        sessionId: t.sessionId,
        runGen: t.runGen,
        busy: false,
        error: '',
      }
      const at = bucket.findIndex((x) => x.id === t.id)
      if (at >= 0) {
        // 重放/重复送达同一张票：就地刷新（保留 busy 与错误 —— 用户的操作在进行中）
        const prev = bucket[at]!
        bucket.splice(at, 1, { ...ticket, busy: prev.busy, error: prev.error })
      } else {
        bucket.push(ticket)
      }
      return ticket
    },

    addQuestion(t) {
      const bucket = questionsBucket(t.sessionId, true)!
      const ticket: ScopedQuestion = {
        id: t.id,
        question: t.question,
        options: t.options,
        allowFreeText: t.allowFreeText,
        expiresAt: t.expiresAt,
        sessionId: t.sessionId,
        runGen: t.runGen,
        busy: false,
        error: '',
      }
      const at = bucket.findIndex((x) => x.id === t.id)
      if (at >= 0) {
        const prev = bucket[at]!
        bucket.splice(at, 1, { ...ticket, busy: prev.busy, error: prev.error, drafting: prev.drafting })
      } else {
        bucket.push(ticket)
      }
      return ticket
    },

    setBusy(kind, id, busy) {
      if (kind === 'approval') {
        eachApproval((t) => {
          if (t.id === id) t.busy = busy
          return true
        })
        return
      }
      eachQuestion((t) => {
        if (t.id === id) t.busy = busy
        return true
      })
    },

    markFailed(kind, id, reason) {
      if (kind === 'approval') {
        eachApproval((t) => {
          if (t.id === id) {
            t.busy = false
            t.error = reason
          }
          return true
        })
        return
      }
      eachQuestion((t) => {
        if (t.id === id) {
          t.busy = false
          t.error = reason
        }
        return true
      })
    },

    settle(kind, id) {
      if (kind === 'approval') {
        eachApproval((t) => t.id !== id)
        return
      }
      eachQuestion((t) => t.id !== id)
    },

    sweepApprovals(now) {
      eachApproval((t) => !t.expiresAt || t.expiresAt > now)
      return scope.approvalCount()
    },

    sweepQuestions(now) {
      let anyLive = false
      eachQuestion((t) => {
        // 已经在作答的项**不判超时**：人还在写，把卡片禁掉只会白丢他的输入
        if (t.expiresAt <= now && !t.drafting) t.expired = true
        else anyLive = true
        return true
      })
      return anyLive
    },

    clearSession(sid) {
      if (!sid) return
      if (state.approvals[sid]) delete state.approvals[sid]
      if (state.questions[sid]) delete state.questions[sid]
    },

    clear() {
      for (const sid of Object.keys(state.approvals)) delete state.approvals[sid]
      for (const sid of Object.keys(state.questions)) delete state.questions[sid]
    },

    isEmpty() {
      return scope.approvalCount() + scope.questionCount() === 0
    },
  }

  return scope
}
