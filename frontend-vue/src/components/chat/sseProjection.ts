/**
 * SSE 事件 → ChatItem 的**纯投影**（从 `ChatView.onSSEMessage` 抽出的第四刀）。
 *
 * 只包含"给定 items + 事件载荷就能算出结果"的分支：工具调用/结果、任务清单、
 * 完成判定、用量、steer 提示。审批、路由、工作台、子 Agent 生命周期仍留在视图里 ——
 * 那些依赖十余个局部状态，整段搬走的风险大于收益。
 *
 * 抽出的动机：这些分支各自对应一类"改坏了不报错"的展示问题（工具状态不落、
 * 清单串会话、用量堆成 N 行、完成判定只显示一半），而它们与视图无关，值得单测。
 */
import type { ChatItem } from './chat-types'

type Obj = Record<string, unknown>

/** 工具调用到达：插入一条 running 卡片（id 取事件 id，缺失才用时间戳）。 */
export function projectToolCall(items: ChatItem[], d: Obj): void {
  items.push({
    kind: 'tool_call',
    id: String(d.id ?? Date.now()),
    name: String(d.name ?? 'tool'),
    arguments: String(d.arguments ?? ''),
    status: 'running',
  })
}

/** 工具结果到达：把对应调用置 done，再插入结果块（空内容不插入）。 */
export function projectToolResult(items: ChatItem[], d: Obj): void {
  const callId = String(d.tool_call_id ?? d.id ?? '')
  const call = items.find(it => it.kind === 'tool_call' && it.id === callId)
  if (call && call.kind === 'tool_call') call.status = 'done'
  const content = d.content ?? d.result ?? ''
  if (!content) return
  items.push({
    kind: 'tool_result',
    toolCallId: callId,
    id: `${callId}:res`,
    content: typeof content === 'string' ? content : JSON.stringify(content),
    isError: !!d.error,
  })
}

/**
 * 任务清单更新：**从事件投影**，不解析 tool_result 正文（大输出会被截断）。
 * 归属用显式 `sid`，不是当前视图的会话 —— SSE 在用户切走后仍会送达。
 */
export function projectTodoEvent(
  sid: string,
  d: Obj,
  evt: Obj,
  applyTodo: (sid: string, todos: unknown[], revision: unknown) => void,
): void {
  let todos: unknown = []
  try {
    todos = JSON.parse(String(d.content ?? evt.content ?? '[]'))
  } catch {
    todos = []
  }
  if (Array.isArray(todos)) applyTodo(sid, todos, d.revision ?? evt.revision)
}

/** 完成判定：只在"未完成"时插一条 warning notice（completed/none 不插行）。 */
export function projectCompletion(
  items: ChatItem[],
  raw: string,
  noticeOf: (raw: string) => string,
  now: () => number = Date.now,
): void {
  const notice = noticeOf(raw)
  if (!notice) return
  items.push({
    kind: 'notice',
    tone: 'warning',
    content: notice,
    time: new Date(now()).toLocaleTimeString(),
    id: `completion:${now()}`,
  })
}

/**
 * 用量：每次 LLM 调用发一条增量 `usage`，终态 `done` 是整轮累计。
 * 必须**累加进同一条** turn_stats（一次提交跨多步，逐条 push 会堆 N 行）。
 */
export function projectUsage(
  items: ChatItem[],
  st: { streamStatsId: string },
  d: Obj,
  evt: Obj,
  merge: (
    items: ChatItem[],
    id: string,
    usage: { inputTokens: number; outputTokens: number; durationMs: number },
    genId: () => string,
  ) => string,
  genId: () => string,
): void {
  const inputTokens = Number(d.input_tokens ?? evt.input_tokens ?? 0) || 0
  const outputTokens = Number(d.output_tokens ?? evt.output_tokens ?? 0) || 0
  const durationMs = Number(d.duration_ms ?? evt.duration_ms ?? 0) || 0
  if (!inputTokens && !outputTokens) return
  st.streamStatsId = merge(
    items,
    st.streamStatsId,
    { inputTokens, outputTokens, durationMs },
    genId,
  )
}

/**
 * 知识库检索事件（P1-6）：只落一个**徽标**，不进正文/思考。
 *
 * 用户要回答的是三个不同的问题（检索了几个库 / 命中几条 / 哪个库报错），
 * 一句话的 notice 装不下；做成气泡又会被误读成 Assistant 说话。
 * 历史不回放：这是实时过程事件，刷新后仅剩正文（与 `todo_updated` 同款处置）。
 */
export function projectKnowledgeRetrieval(
  items: ChatItem[],
  d: Obj,
  evt: Obj,
  now: () => number = Date.now,
): void {
  let kbEvent: Record<string, unknown> = {}
  try {
    const parsed = JSON.parse(String(d.content ?? evt.content ?? '{}'))
    kbEvent = parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    kbEvent = {}
  }
  const kbIds = Array.isArray(kbEvent.kb_ids) ? kbEvent.kb_ids.map(v => String(v)) : []
  const errors = Array.isArray(kbEvent.errors)
    ? (kbEvent.errors as Array<Record<string, unknown>>).map(e => ({
      kbId: String(e?.kb_id ?? ''),
      category: String(e?.category ?? 'unknown'),
      message: String(e?.message ?? ''),
    }))
    : []
  items.push({
    kind: 'knowledge',
    kbIds,
    queried: Number(kbEvent.queried ?? kbIds.length) || 0,
    hits: Number(kbEvent.hits ?? 0) || 0,
    citations: Number(kbEvent.citations ?? 0) || 0,
    zeroHit: kbEvent.zero_hit === true,
    errors,
    id: `kb:${now()}`,
  })
}

/**
 * 本轮验证结论（N6）：`done.verification` 的规范化形态。
 *
 * 与 `projectVerification` 共用同一份解析：徽标（工作台）与提示行（消息流）
 * 必须对同一帧得出相同结论，否则会出现"消息流说失败、diff 说通过"。
 */
export interface VerificationSnapshot {
  status: 'passed' | 'failed' | 'unverified' | 'not-needed'
  command: string
  exitCode: number | null
}

export function normalizeVerification(value: unknown): VerificationSnapshot | null {
  if (!value || typeof value !== 'object') return null
  const v = value as Obj
  const status = String(v.status ?? '')
  if (status !== 'passed' && status !== 'failed' && status !== 'unverified' && status !== 'not-needed') {
    return null
  }
  const rawCode = v.exit_code
  return {
    status,
    command: String(v.command ?? ''),
    exitCode: typeof rawCode === 'number' ? rawCode : null,
  }
}

/**
 * 本轮验证结论（N6）：`done.verification` 转成一行 notice。
 *
 * 只展示三档真实状态：通过 / 失败 / 未验证。`not-needed`（没改文件也没跑验证）
 * 不插行 —— 否则每个只读问答都会多一条"无需验证"的噪音。
 */
export function projectVerification(
  items: ChatItem[],
  verification: unknown,
  t: (key: string, params?: Record<string, unknown>) => string,
  now: () => number = Date.now,
): void {
  const snapshot = normalizeVerification(verification)
  if (!snapshot || snapshot.status === 'not-needed') return
  const content =
    snapshot.status === 'passed'
      ? t('common.verification_passed', { command: snapshot.command })
      : snapshot.status === 'failed'
        ? t('common.verification_failed', {
          command: snapshot.command,
          code: String(snapshot.exitCode ?? '?'),
        })
        : t('common.verification_unverified')
  if (!content) return
  items.push({
    kind: 'notice',
    // 通过是中性事实（info），失败/未验证需要用户注意（warning）。
    tone: snapshot.status === 'passed' ? 'info' : 'warning',
    content,
    id: `verify:${now()}`,
  })
}

export interface CompactionInfo {
  beforeTokens: number
  afterTokens: number
  savedTokens: number
}

/**
 * 自动压缩事件：把"上下文悄悄变短"变成状态栏可见的读数 + 一条 info 行。
 *
 * 与**历史投影**产出同一种 notice（`chat-history.ts` 把 `tool_name='compaction'` 映射过来）
 * ⇒ 实时与刷新后观感一致；文案复用状态栏那个键与单位（k），两处读数不会互相矛盾。
 */
export function projectCompaction(
  items: ChatItem[],
  d: Obj,
  evt: Obj,
  onInfo: (info: CompactionInfo) => void,
  label: (before: string, after: string) => string,
  now: () => number = Date.now,
): void {
  let info: { saved_tokens?: unknown; before_tokens?: unknown; after_tokens?: unknown } = {}
  try {
    const parsed = JSON.parse(String(d.content ?? evt.content ?? '{}'))
    info = parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    info = {}
  }
  if (!info.saved_tokens) return
  const beforeTokens = Number(info.before_tokens) || 0
  const afterTokens = Number(info.after_tokens) || 0
  onInfo({ beforeTokens, afterTokens, savedTokens: Number(info.saved_tokens) || 0 })
  const kTok = (n: number) => (n ? `${(n / 1000).toFixed(1)}k` : '0')
  items.push({
    kind: 'notice',
    tone: 'info',
    content: label(kTok(beforeTokens), kTok(afterTokens)),
    id: `compaction-${String(d.id ?? now())}`,
  })
}

/** steer 被消费：只落一行小字（正文已作为 user 消息进历史，贴两遍就是重复）。 */
export function projectSteerNotice(
  items: ChatItem[],
  content: unknown,
  label: string,
  now: () => number = Date.now,
): void {
  if (!String(content ?? '')) return
  items.push({
    kind: 'notice',
    tone: 'info',
    content: label,
    time: new Date(now()).toLocaleTimeString(),
    id: `steer:${now()}`,
  })
}


/**
 * 完成判定的实时文案：只在**未完成**时返回（completed/none 不插行；解析不出也不插）。
 * 与历史留痕 `goal_incomplete` 同一套措辞，避免"实时看到的"与"刷新后看到的"不一致。
 */
export function completionNotice(
  raw: string,
  t: (key: string, params?: Record<string, unknown>) => string,
): string {
  try {
    const report = JSON.parse((raw || '').trim() || '{}') as { outcome?: unknown; reasons?: unknown }
    const outcome = typeof report.outcome === 'string' ? report.outcome : ''
    if (!outcome || outcome === 'completed' || outcome === 'none') return ''
    const reasons = Array.isArray(report.reasons)
      ? report.reasons.filter((r): r is string => typeof r === 'string' && r.trim() !== '')
      : []
    const head = reasons.slice(0, 3).join('；')
    const more = reasons.length > 3
      ? `（${t('chat.turn_incomplete_more', { n: reasons.length - 3 })}）`
      : ''
    return t('chat.turn_incomplete_reasons', { reasons: head + more || outcome })
  } catch {
    return ''
  }
}
