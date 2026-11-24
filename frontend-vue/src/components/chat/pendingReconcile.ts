/**
 * 「历史待办」与「实时票据」的**统一对账**（H61）。
 *
 * 同一张票会在两个地方出现，而两者都有各自的盲区：
 *
 * * **实时票据**（`interactionScope`）：只有这条 SSE 还开着、且用户此刻正看着那个会话时才在；
 *   刷新页面、切走、进程换实例之后就没了；
 * * **历史待办**（`pending_interactions`，收件箱与角标的数据源）：跨进程都在，但那份列表是
 *   **拉取时**的快照 —— 用户在上面点完"允许"后，角标/收件箱不重拉就会继续显示"还欠一次答复"。
 *
 * 于是出现了"历史 pending 与实时徽标不一致"：同一张票在收件箱里是待答、在上方实时卡片里
 * 已经答完；或者反过来 —— 角标数着 2，界面上只有 1 张卡能点。修法不是其中一边让步，而是
 * **两个数据源按票对齐**（`id` 与 `tool_call_id` 是同一张票的两个键）：
 *
 * 1. 实时覆盖历史：同一张票以实时那份为准（它才是"这一轮真的在等"的现场）；
 * 2. 已由实时通道覆盖的票**不再在收件箱里重复出一张**（一张票一个答复入口）；
 * 3. 角标读**并集**计数（`pendingUnionCount`），不再各说各话。
 *
 * 另外两条**权威值**也在这里：到期只认服务端 `expires_at`，自由输入只认
 * `request.allow_free_text` —— 前端自己算的 300s 只是兜底展示，不能覆盖服务端的说法。
 */
import type { PendingInteraction } from '../../api/interactions'

/** 实时票据的最小形状（ChatView 从 `interactionScope` 投影出来） */
export interface LiveTicketRef {
  /** 票的主键（= 审批/提问的 `tool_call_id`） */
  id: string
  kind: 'approval' | 'question' | 'budget'
  /** 与历史行对齐用的第二个键（历史里叫 `tool_call_id`） */
  toolCallId?: string
}

/** 对账后的收件箱行 */
export interface ReconciledRow extends PendingInteraction {
  /** 这张票在本轮实时通道里也有（已有实时卡片，收件箱不再重复给答复入口） */
  live: boolean
  /** 权威到期判定（服务端 `expires_at`） */
  expired: boolean
}

/**
 * 权威到期判据：**只认服务端 `expires_at`**。
 *
 * 缺字段 ⇒ 不算过期（宁可多给一次机会，也不能凭前端时钟把一张还能答的票禁掉）。
 * 时间戳解析不出来同理 —— 解析失败不等于"已经过期"。
 */
export function interactionExpired(expiresAt: string | undefined, now = Date.now()): boolean {
  if (!expiresAt) return false
  const at = Date.parse(expiresAt)
  return Number.isFinite(at) && at <= now
}

/**
 * 权威自由输入开关：`request.allow_free_text`。
 *
 * 缺省 **true**：与引擎 `ask_user` 的默认一致（不给这个字段就是允许自由作答）。
 * 旧实现根本没读它 —— 于是"只让选、不许打字"的提问在收件箱里仍然带一个输入框，
 * 用户打完点提交才被后端拒掉。
 */
export function allowFreeTextOf(item: PendingInteraction): boolean {
  return item.request?.allow_free_text !== false
}

/** 一张票落定/新增时的广播事件名（角标靠它重算，而不是等 30s 轮询） */
export const INTERACTIONS_CHANGED_EVENT = 'interactions:changed'

/**
 * 通知"待办集合变了"。
 *
 * 同一件事有三个观察者（收件箱列表、侧栏角标、会话卡片区），它们各自按不同节奏取数。
 * 没有这个广播，答完一张票后角标会继续数着它 —— 用户看到的是"我答过了，角标还在"。
 */
export function notifyInteractionsChanged(): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(INTERACTIONS_CHANGED_EVENT))
}

/** 一张票的全部对齐键（`id` + `tool_call_id`，去空） */function keysOf(id?: string, toolCallId?: string): string[] {
  const keys: string[] = []
  if (id) keys.push(id)
  if (toolCallId && toolCallId !== id) keys.push(toolCallId)
  return keys
}

/** 实时票 → 对齐键集合 */
function liveKeySet(live: readonly LiveTicketRef[]): Set<string> {
  const set = new Set<string>()
  for (const t of live) for (const k of keysOf(t.id, t.toolCallId)) set.add(k)
  return set
}

/**
 * 历史 + 实时 → 收件箱行。
 *
 * 只保留历史行（历史是那张表的读视图，含跨进程留下的票）；实时通道覆盖到的行标 `live`，
 * 收件箱据此**不再重复渲染**一张答复卡（实时卡片就在它上面）。`expired` 用权威 `expires_at`。
 */
export function reconcilePending(
  history: readonly PendingInteraction[],
  live: readonly LiveTicketRef[],
  now = Date.now(),
): ReconciledRow[] {
  const liveKeys = liveKeySet(live)
  return history.map((item) => {
    const keys = keysOf(item.id, item.tool_call_id)
    return {
      ...item,
      live: keys.some((k) => liveKeys.has(k)),
      expired: interactionExpired(item.expires_at, now),
    }
  })
}

/**
 * 角标读数：历史 ∪ 实时，同一张票只算一次。
 *
 * 为什么不是"取较大者"：两张表都可能各缺一张（实时只有当前会话、历史可能还没落库完），
 * 取并集才是用户真正还欠的答复数。
 */
export function pendingUnionCount(
  history: readonly PendingInteraction[],
  live: readonly LiveTicketRef[],
): number {
  /**
   * 一张票 = 一组**别名**（`id` 与 `tool_call_id` 指同一张票）。两个数据源里同一个键
   * （或互为别名）落到同一组 ⇒ 只算一张。没有这层归并，"历史 + 实时"会把同一张票算两次。
   */
  const aliasToTicket = new Map<string, number>()
  let tickets = 0
  const adopt = (keys: string[]): void => {
    if (!keys.length) return
    const known = keys
      .map((k) => aliasToTicket.get(k))
      .filter((v): v is number => v !== undefined)
    if (known.length) {
      const owner = known[0]!
      for (const k of keys) aliasToTicket.set(k, owner)
      // 历史里同一张票的两个键可能被两条记录分别引入 ⇒ 把并进来的组统一到 owner
      for (const other of known.slice(1)) {
        if (other === owner) continue
        for (const [k, v] of aliasToTicket) if (v === other) aliasToTicket.set(k, owner)
      }
      return
    }
    const id = tickets++
    for (const k of keys) aliasToTicket.set(k, id)
  }
  for (const item of history) adopt(keysOf(item.id, item.tool_call_id))
  for (const t of live) adopt(keysOf(t.id, t.toolCallId))
  return tickets
}
