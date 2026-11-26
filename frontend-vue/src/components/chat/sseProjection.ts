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
