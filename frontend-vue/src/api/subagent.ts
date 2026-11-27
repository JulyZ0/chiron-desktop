/**
 * 子 Agent 运行观测 API（网关 internal/api/subagent_handler.go）。
 *
 * 数据源分层：Redis（运行期，TTL 1h）优先 → PostgreSQL（权威）回落；
 * 响应里的 `source` 字段标明本次数据来自哪一层 —— 排查"看不到进度"时先看它。
 *
 * 后端契约见 internal/api/subagent_handler.go。
 */
import { api } from './index'

export interface SubagentUsage {
  input_tokens?: number
  output_tokens?: number
  steps?: number
}

/** run 视图（Redis 与 DB 两条来源已由后端归一，前端只认这一份契约）。 */
export interface SubagentRunView {
  run_id: string
  parent_run_id?: string
  depth: number
  profile?: string
  status: string
  summary?: string
  usage?: SubagentUsage
  created_at?: string
  redacted_count?: number
  source?: string
  /**
   * 失败原因（DB 来源才有；Redis 运行期缓存里没有 `error`）。
   *
   * 面板据此区分"撞预算墙"与"普通失败" —— 引擎把预算越界写成
   * `budget_exceeded:<轴>`（见 python-engine/app/agent/task_budget.py）。
   */
  error?: string
  /**
   * 是否撞上预算墙（`status=partial` 或 `error` 以 `budget_exceeded` 开头）。
   *
   * 由后端判定（`internal/api/subagent_handler.go` 的 `runHitBudgetWall`）：上限来自部署
   * 环境的 `SUBAGENT_MAX_*`，前端与网关都读不到 —— 用写死的默认值去比 usage 只会误报。
   */
  budget_exceeded?: boolean
}

/** 进度事件（type 前缀 `subagent.`；经 SSE 实时到达，也可从事件端点回放）。 */
export interface SubagentEvent {
  id?: string
  type: string
  run_id: string
  parent_run_id?: string
  depth?: number
  profile?: string
  status?: string
  content?: string
  truncated?: boolean
  usage?: SubagentUsage
  /** L1 摘要：终态事件（`subagent.done`）携带，见 python-engine/app/agent/event_sink.py:120 */
  summary?: string
  /**
   * 审批事件（`subagent.approval`）的回传凭据。
   *
   * 只认这一组字段名（引擎 payload 与 DB 回放行都用这些名字）：回放路径会把 payload
   * 的 `id` 覆盖成 Redis Stream 消息 id，按 `id` 回传审批决定必然失败。
   */
  tool_call_id?: string
  tool_name?: string
  tool_arguments?: string
  /** 提问事件（`subagent.ask`）：与主 Agent 的 ask 帧同名，因此可直接复用 AskCard */
  question?: string
  options?: string[]
  allow_free_text?: boolean
  /**
   * 工具结果失败原因（`subagent.tool_result`）。
   *
   * 与主 Agent 的 tool_result 事件同名：有这个字段就说明**这次调用失败了**，
   * 界面据此标红并默认展开 —— 不必去猜 content 里那段 JSON 是失败还是工具的
   * 正常输出（有些工具的返回体本身就带 `error` 键，但那是成功返回的一部分）。
   */
  error?: string
  /**
   * G7：本次 run 到终态为止**被丢弃的过程事件数**（`preview` / `delivery` 分开）。
   *
   * 只有终态事件（`subagent.done`）带它 —— 丢弃统计必须走必达通道，自己单发一条
   * 统计事件也可能被丢。界面据此显示"过程事件缺失 N 条"，而不是让用户以为
   * "模型没做"。
   */
  dropped?: {
    dropped?: number
    delivery_dropped?: number
    pending?: number
  }
}

export interface SubagentRunsResponse {
  runs: SubagentRunView[]
  source: string
}

export interface SubagentEventsResponse {
  events: SubagentEvent[]
  source: string
}

/** 整棵层级树（session_id）或某节点的直接子节点（parent_run_id）。 */
export async function listSubagentRuns(params: {
  sessionId?: string
  parentRunId?: string
}): Promise<SubagentRunsResponse> {
  const { data } = await api.get('/v1/subagent/runs', {
    params: {
      session_id: params.sessionId || undefined,
      parent_run_id: params.parentRunId || undefined,
    },
  })
  return { runs: data?.data?.runs || [], source: data?.data?.source || 'db' }
}

export async function getSubagentRun(runId: string): Promise<SubagentRunView | null> {
  const { data } = await api.get(`/v1/subagent/runs/${encodeURIComponent(runId)}`)
  return data?.data?.run || null
}

/** 输出过程回放：Redis Stream（近实时）→ DB steps（历史）。 */
export async function getSubagentRunEvents(runId: string, limit = 200): Promise<SubagentEventsResponse> {
  const { data } = await api.get(`/v1/subagent/runs/${encodeURIComponent(runId)}/events`, {
    params: { limit },
  })
  return { events: data?.data?.events || [], source: data?.data?.source || 'db' }
}

/**
 * 中止单个子 Agent run。
 *
 * 网关只做租户校验 + Redis 广播，真正取消由**持有该 run 的引擎实例**执行（后台任务跑在
 * 父 turn 所在进程里）。所以返回的是「已受理」而不是「已停止」—— 终态靠事件/轮询收敛：
 * status 变 canceled，`error` 里带 `cancelled_by_user`。
 */
export async function cancelSubagentRun(runId: string): Promise<{ status: string }> {
  const { data } = await api.post(`/v1/subagent/runs/${encodeURIComponent(runId)}/cancel`)
  return { status: data?.data?.status || 'accepted' }
}

/** 中止某会话下所有活跃子 Agent run（面板上的「全部停止」）。 */
export async function cancelSessionSubagents(sessionId: string): Promise<{ status: string }> {
  const { data } = await api.post(`/v1/subagent/sessions/${encodeURIComponent(sessionId)}/cancel`)
  return { status: data?.data?.status || 'accepted' }
}

/** 重跑受理结果：新 run 的 id 由网关转发引擎后立刻返回（新 run 在后台跑）。 */
export interface SubagentRerunResult {
  status: string
  /** 新 run 的 id（**不是**原 run：原 run 的记录保持不变，血缘在 rerun_of 上） */
  run_id: string
  /** 被重跑的原 run */
  rerun_of: string
  parent_run_id?: string
  depth?: number
}

/**
 * 按**原任务描述**重跑一个已结束的子 Agent run（面板「重试」按钮的真实通道）。
 *
 * 契约（网关 `POST /v1/subagent/runs/{id}/rerun` → 引擎内网端点）：
 *  - 204/200 + 新 `run_id`：真的派出去了，后续进度走 `subagent.*` 事件；
 *  - **409**：原 run 非终态或库里没有任务描述 —— 调用方必须回退到"放回输入框"，
 *    不能假装重试成功（那会让用户等一个永远不会来的 run）；
 *  - **404**：这条 run 不属于当前会话/租户/用户（与只读端点同一口径，不泄露存在性）。
 */
export async function rerunSubagentRun(runId: string): Promise<SubagentRerunResult> {
  const { data } = await api.post(`/v1/subagent/runs/${encodeURIComponent(runId)}/rerun`)
  const d = data?.data || {}
  return {
    status: d.status || 'accepted',
    run_id: d.run_id || '',
    rerun_of: d.rerun_of || runId,
    parent_run_id: d.parent_run_id,
    depth: d.depth,
  }
}

/**
 * 给**运行中**的子 Agent 追加一条指令（父→子 steer，F1）。
 *
 * 语义与顶层 steer 一致：只入队、不打断；子 Agent 在下一轮开头取走。
 * 后端保证"没送到就报错"（409 已结束 / 429 队列满 / 503 通道不可用），
 * 调用方不要把失败显示成"已发送"。
 */
export async function steerSubagentRun(
  runId: string,
  text: string,
): Promise<{ delivery: string }> {
  const { data } = await api.post(
    `/v1/subagent/runs/${encodeURIComponent(runId)}/steer`,
    { text },
  )
  return { delivery: data?.data?.delivery || 'shared' }
}

/**
 * 在当前会话下**直接**启动一个后台子 Agent（F3）。
 *
 * 与模型自己调用 `subagent(...)` 完全同源（注册表 / 预算 / 落库 / 事件旁路），
 * 区别只是发起者是人。返回新 run 的 id；进度走 `subagent.*` 事件。
 */
export async function dispatchSubagent(
  sessionId: string,
  input: { task: string; profile?: string; allowWrite?: boolean; maxTurns?: number },
): Promise<{ run_id: string }> {
  const { data } = await api.post(
    `/v1/sessions/${encodeURIComponent(sessionId)}/subagents`,
    {
      task: input.task,
      profile: input.profile || '',
      allow_write: !!input.allowWrite,
      max_turns: input.maxTurns || 10,
    },
  )
  return { run_id: String(data?.data?.run_id || '') }
}
