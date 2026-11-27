<script setup lang="ts">
/**
 * 子 Agent 观测面板：递归层级树 + 选中 run 的实时输出。
 *
 * 数据来源（三层，见 docs/subagent-design.md §3.4/§5.2）：
 *   1. 树骨架与状态：GET /v1/subagent/runs（Redis → DB），轮询刷新；
 *   2. 实时输出：父组件（ChatView）把 SSE 的 `subagent.*` 事件按到达顺序传进来（liveEvents）；
 *   3. 历史输出：选中 run 时 GET /v1/subagent/runs/{id}/events 拉一次，再与实时事件拼接。
 *
 * 层级用「depth 缩进 + 折叠」呈现：后端返回的扁平列表已带 depth/parent_run_id，
 * 折叠即按 parent_run_id 隐藏整棵子树 —— 比递归组件更省，且不会因深层嵌套而抖。
 */
import { copyTextOrThrow } from '../../utils/clipboard'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Empty, Spin, Tag, Tooltip, message } from 'ant-design-vue'
import { submitAnswer, submitApproval } from '../../api'
import {
  cancelSessionSubagents,
  cancelSubagentRun,
  getSubagentRunEvents,
  listSubagentRuns,
  rerunSubagentRun,
  steerSubagentRun,
  type SubagentEvent,
  type SubagentRunView,
} from '../../api/subagent'
import SubAgentApprovalCard from './SubAgentApprovalCard.vue'
import AskCard from './AskCard.vue'
import SubagentRunCard from '../agent/SubagentRunCard.vue'
// 与主对话流**同一套**工具渲染：子 Agent 的工具调用同样是"调了什么 + 结果如何"，
// 换一份长得差不多的实现只会让两边的摘要/展开/失败配色各自漂移。
import ToolCallCard from './ToolCallCard.vue'
import ToolResultBlock from './ToolResultBlock.vue'
import { projectSubagentOutput } from './subagentOutput'

import { useI18n } from 'vue-i18n'
import { createKeyedGuard } from '../../utils/staleGuard'
const { t } = useI18n()

/**
 * 「重试」= **先真的重跑**，失败再回退到"放回输入框"。
 *
 * 为什么不再是"直接把摘要丢回输入框"：那个兜底动作会把已经跑过一次的任务当成新任务
 * 重发 —— 血缘断了（新 run 与失败的那条毫无关系），重试计数也就无从统计。P0-2 之后
 * 后端有了 `POST /v1/subagent/runs/{id}/rerun`：按**库里存的原任务**再派一次，
 * 新 run 以 parent_run_id / rerun_of 挂回原 run。
 *
 * 什么时候回退（emit `retry-run`）：
 *  - 409：原 run 还在跑，或库里没有任务描述（要求用户从前端输入框重发）；
 *  - 404：这条 run 不属于当前会话/租户/用户；
 *  - 其它错误（引擎不可用等）：也不假装成功 —— 放回输入框让用户决定。
 */
const emit = defineEmits<{
  (e: 'retry-run', payload: { runId: string; text: string }): void
}>()

/** F1：给选中且仍在跑的 run 追加指令 */
const steerText = ref('')
const steering = ref(false)
const canSteer = computed(() => {
  const run = selectedRun.value
  return !!run && !TERMINAL_STATUSES.has(String(run.status))
})

async function sendSteer(): Promise<void> {
  const run = selectedRun.value
  const text = steerText.value.trim()
  if (!run || !text || steering.value) return
  steering.value = true
  try {
    await steerSubagentRun(run.run_id, text)
    steerText.value = ''
    void message.success(t('agent.subagent_steer_sent'))
  } catch (e) {
    // 失败必须显式说出来：这条通道的全部价值就是"让用户相信话带到了"。
    void message.error(t('agent.subagent_steer_failed', { reason: describeSteerError(e) }))
  } finally {
    steering.value = false
  }
}

function describeSteerError(e: unknown): string {
  const err = e as { response?: { status?: number } }
  const status = err?.response?.status
  if (status === 409) return t('agent.subagent_steer_finished')
  if (status === 429) return t('agent.subagent_steer_queue_full')
  if (status === 503) return t('agent.subagent_steer_unavailable')
  return t('errors.request_failed_2')
}

/** 统一时钟：只在有活跃 run 时每秒推进（父面板只有一个定时器，卡片共用这个值）。 */
const nowTick = ref(Date.now())
let nowTimer: ReturnType<typeof globalThis.setInterval> | null = null

/** 正在请求重跑的 run（按钮禁用 + 防连点：连点会真的派出多个作业） */
const rerunning = ref<Set<string>>(new Set())

function emitRetry(run: SubagentRunView) {
  const summary = (run.summary || '').trim()
  const name = run.profile || run.run_id
  emit('retry-run', {
    runId: run.run_id,
    text: summary || t('agent.subagent_card_retry_default', { name }),
  })
}

/** 卡片上的「重试」：先调重跑接口；被拒/失败再回退到"放回输入框"。 */
async function rerunRun(run: SubagentRunView) {
  const runId = run.run_id
  if (!runId || rerunning.value.has(runId)) return
  rerunning.value = new Set(rerunning.value).add(runId)
  let started = ''
  try {
    const res = await rerunSubagentRun(runId)
    if (res.status === 'accepted' && res.run_id) started = res.run_id
  } catch {
    // 409/404/网络/引擎不可用：统一走兜底路径（下面），不在这里弹全局错误
  } finally {
    const next = new Set(rerunning.value)
    next.delete(runId)
    rerunning.value = next
  }
  if (!started) {
    // 兜底：把任务放回输入框，由用户确认后重新派发（并说明为什么没能直接重跑）
    emitRetry(run)
    void message.info(t('agent.subagent_rerun_fallback'))
    return
  }
  void message.success(t('agent.subagent_rerun_started', { run: started }))
  // 立刻刷新树并选中新 run：它的进度与审批都在新 run 下，用户不必等 3s 轮询
  await loadRuns()
  void selectRun(started)
}

const props = withDefaults(defineProps<{
  /** 当前会话 id；为空时面板提示"无会话" */
  sessionId?: string
  /** 父组件累积的实时事件（按到达顺序），只包含当前会话的 subagent.* 事件 */
  liveEvents?: SubagentEvent[]
  /**
   * 外部请求定位的 run（对话流里点某个子 Agent 工具卡时带入）。
   *
   * 只在对照栏里用：从浮卡改成栏之后，入口是"点对话里的那一行"，
   * 而栏可能已经开着在看别的 run —— 此时必须**切到被点的那一个**。
   */
  focusRunId?: string
  /** 请求序号：连点同一个 run 也要能重新定位（只比 id 的话第二次值相同、watch 不触发） */
  focusSeq?: number
  /** 隐藏面板自己的标题行（栏外壳已经有标题，重复一层只是噪音） */
  hideHeader?: boolean
}>(), { sessionId: '', liveEvents: () => [], focusRunId: '', focusSeq: 0, hideHeader: false })

const runs = ref<SubagentRunView[]>([])
const loading = ref(false)
const source = ref('')
const selectedRunId = ref('')
const historyEvents = ref<SubagentEvent[]>([])
const historyLoading = ref(false)
const collapsed = ref<Set<string>>(new Set())
const COST_HINT_KEY = 'chiron.subagent.costHintSeen'
const showCostHint = ref(false)

let timer: ReturnType<typeof globalThis.setInterval> | undefined

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  reasoning: 'processing',
  responding: 'processing',
  tool: 'processing',
  retrying: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
}

/**
 * 是否被某个已折叠的祖先隐藏（沿 parent_run_id 上溯）。
 *
 * `byId` 由调用方传入：`visibleRuns` 之前对**每个** run 各建一次 Map，
 * 几十个 run 就是几十次全表扫描（O(n²)），而这张表一次就够。
 */
function hiddenByAncestor(run: SubagentRunView, byId: Map<string, SubagentRunView>): boolean {
  let parentId = run.parent_run_id || ''
  let guard = 0
  while (parentId && guard++ < 20) {
    if (collapsed.value.has(parentId)) return true
    parentId = byId.get(parentId)?.parent_run_id || ''
  }
  return false
}

/**
 * 一次扫描 `liveEvents`，派生面板需要的**全部**实时视图。
 *
 * 为什么合并：此前 `liveRuns` / `liveTail` / `approvalEvents` / `pendingQuestions`
 * 各自完整遍历一遍同一个数组。事件缓冲上限 500，看起来不多，但每个 run 每秒
 * 都在推增量，四个 computed 又都被模板直接引用 —— 一条事件到达会引发**四次**
 * O(n) 扫描，而 n 还在增长。合成一次之后，同一条事件的代价与视图数量无关。
 *
 * 这里只做**纯提取**（不做过滤/去重策略），策略留给各自的派生 computed：
 * 谁依赖 `runs`（API 结果）或 `askAnswers`（本地状态）就在谁那里加，
 * 免得把无关依赖混进这个热点。
 */
const liveScan = computed(() => {
  const runStates: Record<string, SubagentRunView> = {}
  const tails: Record<string, string> = {}
  const approvals: Array<{ toolCallId: string; runId: string; toolName: string; args: string; content: string }> = []
  const questions: Array<{ toolCallId: string; runId: string }> = []
  const approvalSeen = new Set<string>()
  const questionSeen = new Set<string>()

  for (const e of props.liveEvents || []) {
    const id = e?.run_id
    if (!id) continue

    // ① run 骨架：终态带 summary/usage，其它事件只补层级与 profile
    const prev = runStates[id]
    const done = e.type === 'subagent.done'
    runStates[id] = {
      run_id: id,
      parent_run_id: e.parent_run_id || prev?.parent_run_id || '',
      depth: Number(e.depth ?? prev?.depth ?? 1) || 1,
      profile: e.profile || prev?.profile || '',
      status: done ? (e.status || 'done') : (prev?.status || 'running'),
      summary: done ? (e.summary ?? prev?.summary) : prev?.summary,
      usage: e.usage ?? prev?.usage,
      created_at: prev?.created_at,
    }

    // ② 每个 run 的最新一段输出（列表行上的"它现在在做什么"）
    if (typeof e.content === 'string' && e.content) {
      tails[id] = e.content.replace(/\s+/g, ' ').slice(-120)
    }

    // ③ 审批：按 tool_call_id 去重且保序（同一 id 在历史回放与实时流里各来一次）
    if (e.type === 'subagent.approval' && e.tool_call_id && !approvalSeen.has(e.tool_call_id)) {
      approvalSeen.add(e.tool_call_id)
      approvals.push({
        toolCallId: e.tool_call_id,
        runId: id,
        toolName: e.tool_name || '',
        args: e.tool_arguments || '',
        content: e.content || '',
      })
    }

    // ④ 提问：同样按 tool_call_id 去重
    if (e.type === 'subagent.ask' && e.tool_call_id && !questionSeen.has(e.tool_call_id)) {
      questionSeen.add(e.tool_call_id)
      questions.push({ toolCallId: e.tool_call_id, runId: id })
    }
  }
  return { runStates, tails, approvals, questions }
})

/**
 * 从**实时事件**合成的运行条目 —— 观测面的第二个数据源。
 *
 * 为什么必须有它：`GET /v1/subagent/runs` 依赖后端把运行写进 journal/Redis，那条链路
 * 任何一环出问题（或该 run 尚未落库），面板就会显示"本次会话还没有子 Agent 运行" ——
 * 而 SSE 里其实正在源源不断地送 `subagent.*` 事件。观测不该因为一个数据源失败而全瞎。
 * （与 ZCode 的"run 的真相在 journal、观察面出问题不影响可用性"同一思路。）
 */
const liveRuns = computed(() => {
  const known = new Set(runs.value.map(r => r.run_id))
  return Object.values(liveScan.value.runStates).filter(r => !known.has(r.run_id))
})

/** 面板实际展示的运行：**API 结果优先，实时事件补齐**（按 run_id 去重） */
const displayRuns = computed(() => {
  const apiIds = new Set(runs.value.map(r => r.run_id))
  return [...runs.value, ...liveRuns.value.filter(r => !apiIds.has(r.run_id))]
})

/**
 * 每个 run 的**最新一段实时输出**，用于列表行上的"它现在在做什么"。
 *
 * 为什么需要：事件流 tab 只渲染选中的 run —— 用户不点开就只看得到状态标签。
 * 这里把每个 run 的最后一段内容（尾部 120 字符）挂到行上，未展开也能看到进度在流动。
 */
const liveTail = computed(() => liveScan.value.tails)

/** 已到终态的 run 状态（这些状态之后不再有增量，只补一次尾巴） */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'killed', 'stopped', 'lost', 'done'])

/** 是否还有运行中的 run（决定「全部停止」是否出现） */
const hasRunning = computed(() => displayRuns.value.some(r => !TERMINAL_STATUSES.has(r.status)))

// 时钟只在有活跃 run 时推进：终态列表不需要每秒重算耗时。
watch(
  hasRunning,
  active => {
    if (active && nowTimer === null) {
      nowTick.value = Date.now()
      nowTimer = globalThis.setInterval(() => { nowTick.value = Date.now() }, 1000)
    } else if (!active && nowTimer !== null) {
      globalThis.clearInterval(nowTimer)
      nowTimer = null
    }
  },
  { immediate: true },
)

/** 已请求取消的 run（本地即时反馈；终态到达后由事件/轮询清掉） */
const cancelling = ref<Set<string>>(new Set())

/**
 * 停止单个 run。
 *
 * 只是「请求已受理」：网关做租户校验 + Redis 广播 → 持有该 run 的引擎实例 `task.cancel()`
 * → 收尾后终态（status=cancelled, error=cancelled_by_user）经事件/轮询回来。
 * 所以这里**不直接改 status** —— 状态的真相在引擎，前端只做按钮禁用这类即时反馈。
 */
async function stopRun(runId: string) {
  if (cancelling.value.has(runId)) return
  cancelling.value = new Set(cancelling.value).add(runId)
  try {
    const res = await cancelSubagentRun(runId)
    if (res.status === 'lost') {
      // 网关等不到持有实例的回执（实例已重启/被驱逐）→ 它把作业标成了 lost。
      // 如实告诉用户，而不是让"正在停止"一直挂着、状态永远不变（假成功）。
      void message.warning(t('auth.this_sub_agent_lost_contact_instance_restarted_or_exited_and_is_marked_as_lost'))
    }
  } catch {
    // 失败就放开按钮让用户能重试（不做假成功提示）
    const next = new Set(cancelling.value)
    next.delete(runId)
    cancelling.value = next
  }
}

/** 停止该会话下所有活跃子 Agent（一次清场） */
async function stopAll() {
  if (!props.sessionId) return
  try {
    await cancelSessionSubagents(props.sessionId)
    cancelling.value = new Set([
      ...cancelling.value,
      ...displayRuns.value.filter(r => !TERMINAL_STATUSES.has(r.status)).map(r => r.run_id),
    ])
  } catch {
    /* 忽略：下一次 tick 会重新渲染，用户可再点一次 */
  }
}

/** 终态到达后清掉本地 cancelling 标记，避免按钮永久禁用 */
watch(displayRuns, (list) => {
  if (!cancelling.value.size) return
  const next = new Set([...cancelling.value].filter(id =>
    !list.some(r => r.run_id === id && TERMINAL_STATUSES.has(r.status))))
  if (next.size !== cancelling.value.size) cancelling.value = next
})

// ── 子 Agent 审批（P3-后续）──────────────────────────────────────────────
//
// 子 Agent 默认 tools_mode=auto，而 shell_exec 这类工具在 auto 下就需要确认 ——
// 所以"子 Agent 请求审批"几乎每次调用命令类工具都会发生。此前它只以一行 notice
// 出现，用户能看见却**没有任何办法批准**：子 Agent 空转到 300s 超时后以
// "approval timed out" 被拒。这里把 approval 事件渲染成**可点击**的卡片，
// 决定经与主 Agent 完全相同的通道回传（POST /v1/agent/approval）。
//
// 回传凭据只认 `tool_call_id`（见 api/subagent.ts 的说明）。

/** 已提交决定的 tool_call_id → 本地结论（避免重复提交，也给用户一个即时反馈） */
const approvalDecisions = ref<Record<string, 'approved' | 'denied'>>({})
/** 正在提交的 tool_call_id */
const approvalSubmitting = ref<Set<string>>(new Set())
/** 提交失败的原因（按 tool_call_id），失败时保留按钮让用户重试 —— 不做假成功 */
const approvalErrors = ref<Record<string, string>>({})

/** 收集所有已到达的 approval 事件（历史回放 + 实时），按 tool_call_id 去重且保序。 */
const approvalEvents = computed(() => liveScan.value.approvals)

/** 尚未决定、也未被终态作废的审批（子 Agent 已结束 → 卡片自动消失）。 */
const pendingApprovals = computed(() => {
  const terminal = new Set(
    displayRuns.value.filter(r => TERMINAL_STATUSES.has(r.status)).map(r => r.run_id),
  )
  return approvalEvents.value.filter(
    a => !approvalDecisions.value[a.toolCallId] && !terminal.has(a.runId),
  )
})

/** 每个 run 待处理数（列表行上的可见标记：不点开也知道有东西在等）。 */
const pendingByRun = computed(() => {
  const out: Record<string, number> = {}
  for (const a of pendingApprovals.value) out[a.runId] = (out[a.runId] || 0) + 1
  for (const q of pendingQuestions.value) out[q.runId] = (out[q.runId] || 0) + 1
  return out
})

// ── 子 Agent 提问（ask）──────────────────────────────────────────────
//
// 与审批同构，只是回传的是**答案文本**而不是布尔 —— 因此走 /v1/agent/answer，
// 并复用主对话流已经在用的 AskCard 组件（同一套 UI，用户不必学两次）。
//
// 此前子 Agent 的 ask 只发一行 notice：用户看得见却答不了，子 Agent 只能空转到超时。

/** 已提交答案的 tool_call_id → 答案（避免重复提交，也给用户即时反馈） */
const askAnswers = ref<Record<string, string>>({})
/** 正在提交的 tool_call_id */
const askSubmitting = ref<Set<string>>(new Set())
/** 提交失败原因（按 tool_call_id）；非空同时表示"该提问已失效"（引擎超时/已被取走） */
const askErrors = ref<Record<string, string>>({})

/** 尚未回答、也未被终态作废的提问（子 Agent 已结束 → 卡片自动失效）。 */
const pendingQuestions = computed(() => {
  const terminal = new Set(
    displayRuns.value.filter(r => TERMINAL_STATUSES.has(r.status)).map(r => r.run_id),
  )
  return liveScan.value.questions.filter(
    q => !askAnswers.value[q.toolCallId] && !terminal.has(q.runId),
  )
})

/**
 * 提交子 Agent 提问的答案。
 *
 * 与审批一样只做一次乐观标记：**状态的真相在引擎** —— 答案送达后子 Agent 继续跑，
 * 它的后续事件会出现在同一条时间线上。
 */
async function resolveAsk(toolCallId: string, answer: string) {
  const id = (toolCallId || '').trim()
  const value = (answer || '').trim()
  if (!id || !value || askSubmitting.value.has(id) || askAnswers.value[id]) return
  askSubmitting.value = new Set(askSubmitting.value).add(id)
  const errs = { ...askErrors.value }
  delete errs[id]
  askErrors.value = errs
  try {
    const ok = await submitAnswer({
      session_id: props.sessionId || '',
      tool_call_id: id,
      answer: value,
    })
    if (!ok) {
      askErrors.value = { ...askErrors.value, [id]: t('errors.answer_not_delivered_may_have_timed_out') }
      return
    }
    askAnswers.value = { ...askAnswers.value, [id]: value }
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    askErrors.value = { ...askErrors.value, [id]: detail }
  } finally {
    const next = new Set(askSubmitting.value)
    next.delete(id)
    askSubmitting.value = next
  }
}

/** 审批参数：美化后的 JSON 已由 SubAgentApprovalCard 负责 —— 面板只做数据与回传。 */

/**
 * 提交子 Agent 审批决定。
 *
 * 只做一次乐观标记：**状态真相在引擎**（批准后子 Agent 会继续产出事件；
 * 拒绝则它收到 error 并在结果里体现），前端不伪造后续状态。
 *
 * `editedArguments`（C5）非空 ⇒ "**编辑后批准**"：回传 `decision: 'edit'` 与编辑后的
 * 完整参数；是否**真的执行**由引擎按编辑后参数重跑分级后决定（级别升高则不执行），
 * 前端不做本地放行判断。乐观标记仍记 `approved` —— 若引擎拦下，后续事件会体现出来。
 */
async function resolveApproval(
  a: { toolCallId: string },
  approved: boolean,
  editedArguments?: string,
) {
  const id = a.toolCallId
  if (!id || approvalSubmitting.value.has(id) || approvalDecisions.value[id]) return
  approvalSubmitting.value = new Set(approvalSubmitting.value).add(id)
  const errors = { ...approvalErrors.value }
  delete errors[id]
  approvalErrors.value = errors
  try {
    const ok = await submitApproval({
      session_id: props.sessionId || '',
      tool_call_id: id,
      approved: editedArguments ? true : approved,
      decision: editedArguments ? 'edit' : approved ? 'approve' : 'reject',
      ...(editedArguments ? { arguments: editedArguments } : {}),
    })
    if (!ok) {
      // 引擎返回 ok=false：通常是超时（决定到得太晚）或该调用已不在等待。
      // 明确说出来，而不是把卡片默默撤掉（用户会以为点生效了）。
      approvalErrors.value = { ...approvalErrors.value, [id]: t('errors.approval_not_effective_may_have_timed_out') }
      return
    }
    approvalDecisions.value = { ...approvalDecisions.value, [id]: approved ? 'approved' : 'denied' }
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    approvalErrors.value = { ...approvalErrors.value, [id]: detail }
  } finally {
    const next = new Set(approvalSubmitting.value)
    next.delete(id)
    approvalSubmitting.value = next
  }
}

/** 点列表行上的"待确认"标记：选中该 run 并切到「输出」（可操作卡片在那里）。 */
function focusApprovals(runId: string) {
  saTab.value = 'output'
  void selectRun(runId)
}

/** 可见运行：按 parent_run_id 沿祖先上溯，被折叠的那一支整棵隐藏 */
const visibleRuns = computed(() => {
  const list = displayRuns.value
  const byId = new Map(list.map(r => [r.run_id, r]))
  return list.filter(r => !hiddenByAncestor(r, byId))
})

const selectedRun = computed<SubagentRunView | null>(() => {
  const id = selectedRunId.value
  if (!id) return null
  const known = runs.value.find(r => r.run_id === id)
  if (known) return known
  // 回退到**实时事件合成的条目**：后台 run 往往还没落进 GET /v1/subagent/runs，
  // 而「输出」区（审批卡片就在这里）由 selectedRun 决定是否渲染 ——
  // 只认 API 会让"点得开待确认标记、却看不到卡片"，等于又回到批不了的状态。
  const live = liveRuns.value.find(r => r.run_id === id) as SubagentRunView | undefined
  if (live) return live
  // 第三层回退：**从事件回放合成**。从对话流点进来的 run 可能已经跑完并被轮转掉
  // （既不在 API 结果里、也不在实时缓冲里），但事件端点仍能回放出它 —— 此时
  // 「输出」区必须能渲染，否则点了那一行等于没反应。
  if (historyEvents.value.some(e => e.run_id === id)) {
    const last = historyEvents.value.filter(e => e.run_id === id).at(-1)
    return {
      run_id: id,
      parent_run_id: last?.parent_run_id || '',
      depth: Number(last?.depth ?? 1) || 1,
      profile: last?.profile || '',
      status: String(last?.status || 'completed'),
      summary: last?.summary,
      usage: last?.usage,
    }
  }
  return null
})

/** 选中 run 的事件流 = 历史（回放）+ 实时（本会话累积中属于该 run 的部分）。 */
const selectedEvents = computed<SubagentEvent[]>(() => {
  const runId = selectedRunId.value
  if (!runId) return []
  const live = (props.liveEvents || []).filter(e => e.run_id === runId)
  // 历史里可能已含同 id 的事件（Redis Stream 的 id）——按 id 去重后再拼接
  const seen = new Set(historyEvents.value.map(e => e.id).filter(Boolean) as string[])
  return [...historyEvents.value, ...live.filter(e => !e.id || !seen.has(e.id))]
})

/**
 * 输出区一次最多渲染多少行。
 *
 * 为什么要有上限：一个跑得久的子 Agent 能产出**上千条**事件（每次思考、每段正文、
 * 每条工具结果各一条）。全量渲染进 DOM，每次新事件到达 Vue 都要为整条列表做
 * diff —— 事件以每秒数条的速度进来时，这就是卡顿的来源。过程视图是给人"读现在
 * 在做什么"的，不需要无限历史；更早的部分由「事件流」tab 与轮询回放兜底。
 */
/**
 * G7：该 run 的过程事件丢弃数（终态事件携带）。
 *
 * 为什么要显示：预览事件有每秒预算与队列上限，丢弃是**设计内**的行为；但不说明就
 * 变成"模型少做了几步"的错觉。审批 / 终态 / 工具调用走必达白名单，不会被丢。
 */
const droppedNote = computed(() => {
  const runId = String(selectedRunId.value || '')
  if (!runId) return ''
  // 实时事件与回放事件是两条来源，丢弃计数只出现在**终态**上，两处都要看。
  const events: SubagentEvent[] = [
    ...(props.liveEvents || []),
    ...(historyEvents.value || []),
  ].filter((e) => String(e.run_id) === runId)
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const d = events[i]?.dropped
    if (!d) continue
    const total = Number(d.dropped || 0) + Number(d.delivery_dropped || 0)
    if (total > 0) return String(total)
  }
  return ''
})

const OUTPUT_ROW_LIMIT = 400

/**
 * 输出区实际渲染的行（**与主 Agent 同构**：工具调用卡 + 工具结果块）。
 *
 * 配对规则在 `subagentOutput.ts` 里（纯函数、有单测）；这里只负责截尾：
 * 超出上限时保留**最近**的若干行 —— 过程视图关心的是"现在"，而不是开头。
 *
 * 一次投影同时算出 `hidden`（被折叠掉的行数）：分成两个 computed 会把整条事件流
 * 投影两遍，而每来一条新事件两个都会失效重算 —— 那正是要避免的开销。
 */
const outputView = computed(() => {
  const rows = projectSubagentOutput(selectedEvents.value)
  if (rows.length <= OUTPUT_ROW_LIMIT) return { rows, hidden: 0 }
  return { rows: rows.slice(-OUTPUT_ROW_LIMIT), hidden: rows.length - OUTPUT_ROW_LIMIT }
})
const outputRows = computed(() => outputView.value.rows)
const outputHiddenRows = computed(() => outputView.value.hidden)

/**
 * 「输出」tab 里的并列 tab 条 —— 哪些 run 值得占一格。
 *
 * 取 `visibleRuns`（尊重折叠）而不是 `displayRuns`：用户在「运行」里把一整支折叠掉了，
 * 输出区却又把它列出来，等于折叠没生效。顺序沿用层级顺序（父在子前），
 * 与运行树一致 —— 两处对"谁是谁的上层"的判断不该相反。
 */
const outputTabs = computed(() => visibleRuns.value)

const totals = computed(() => {
  let input = 0
  let output = 0
  for (const run of runs.value) {
    input += run.usage?.input_tokens || 0
    output += run.usage?.output_tokens || 0
  }
  return { input, output }
})

/** 失败率统计窗口（毫秒）：近 24 小时。 */
const FAILURE_WINDOW_MS = 24 * 60 * 60 * 1000

/**
 * 近 24h 的失败率 —— **只来自列表数据**（`GET /v1/subagent/runs`）。
 *
 * 为什么不另开一个统计接口：这条列表已经带着每条 run 的状态与 `created_at`，
 * 再查一次只会多一个可能对不上的数字（而"失败率"最怕的就是"跟卡片上的状态不一致"）。
 *
 * 口径：
 *  - 分母 = 窗口内创建的 run 总数（`created_at` 缺失的行**不计入**：宁可少算，
 *    也不把一条时间未知的旧 run 塞进"最近 24 小时"）；
 *  - 分子 = 其中 `failed`（真失败）与 `lost`（失联：实例重启/被驱逐，用户视角同样是没跑成）。
 *    `cancelled`（用户自己停的）与 `partial`（有产出）**不算失败** —— 把它们算进去会让
 *    失败率变成"我按了几次停止"的指标，失去预警意义。
 */
const failureStats = computed(() => {
  const since = Date.now() - FAILURE_WINDOW_MS
  let total = 0
  let failed = 0
  for (const run of runs.value) {
    const ts = run.created_at ? new Date(run.created_at).getTime() : Number.NaN
    if (Number.isNaN(ts) || ts < since) continue
    total += 1
    const status = String(run.status)
    if (status === 'failed' || status === 'lost') failed += 1
  }
  const rate = total ? Math.round((failed / total) * 100) : 0
  return { total, failed, rate }
})

/** 近 24h 有没有撞预算墙的 run（头部给一个汇总入口，卡片各自高亮） */
const budgetHitCount = computed(() => {
  const since = Date.now() - FAILURE_WINDOW_MS
  return runs.value.filter((run) => {
    const ts = run.created_at ? new Date(run.created_at).getTime() : Number.NaN
    if (!Number.isNaN(ts) && ts < since) return false
    return run.budget_exceeded === true || String(run.status) === 'partial' ||
      String(run.error || '').startsWith('budget_exceeded')
  }).length
})

const childCount = computed(() => {
  const map = new Map<string, number>()
  for (const run of runs.value) {
    const parent = run.parent_run_id || ''
    map.set(parent, (map.get(parent) || 0) + 1)
  }
  return map
})

function toggleCollapse(runId: string) {
  const next = new Set(collapsed.value)
  if (next.has(runId)) next.delete(runId)
  else next.add(runId)
  collapsed.value = next
}

// H60：子 Agent 列表是**按会话**取的（3 秒轮询）。切会话时旧轮询回来会写
// `runs/source` 并重新 `followActiveRun` —— 于是 A 的子 Agent 进度显示在 B 的面板里，
// 用户还会对着它批准一条属于 A 的请求。
const runsGuard = createKeyedGuard(() => props.sessionId || '')

async function loadRuns() {
  if (!props.sessionId) {
    runs.value = []
    return
  }
  const ticket = runsGuard.ticket(props.sessionId)
  loading.value = runs.value.length === 0
  try {
    const res = await listSubagentRuns({ sessionId: props.sessionId })
    // 票失效（换了会话，或组件已卸载）⇒ 一个字都不写
    if (!runsGuard.isValid(ticket)) return
    runs.value = res.runs
    source.value = res.source
    followActiveRun(res.runs)
  } catch {
    // 面板失败不打断对话；下一轮轮询会再试
  } finally {
    if (runsGuard.isValid(ticket)) loading.value = false
  }
}

/**
 * 让"选中的 run"始终指向**还在产生内容**的那一个。
 *
 * 为什么必须跟随：选中 run 的增量只靠 3 秒轮询（后台 run 没有 SSE 连接），而轮询在
 * run 到终态后就停了（`tailLoaded`）。此前只在"从未选中"时才自动选第一个 —— 于是一轮
 * 对话结束后（选中的那个已 completed），**之后新派发的子 Agent 永远不会被选中**：
 * 它的进度与审批请求都看不到，用户只能刷新页面（刷新后 selectedRunId 为空 → 重新自选）。
 */
function followActiveRun(list: SubagentRunView[]) {
  if (!list.length) {
    selectedRunId.value = ''
    return
  }
  const active = list.filter(r => !TERMINAL_STATUSES.has(String(r.status)))
  const current = selectedRunId.value
    ? list.find(r => r.run_id === selectedRunId.value)
    : undefined
  if (current && active.some(r => r.run_id === current.run_id)) return // 当前就在跑：不动
  const fallback = list[list.length - 1]?.run_id || ''
  const target = active.length ? active[active.length - 1]?.run_id || '' : (current?.run_id || fallback)
  if (!target || target === selectedRunId.value) return
  selectedRunId.value = target
  // 切换目标时立刻拉一次历史（之后的增量交给 3 秒轮询）
  void selectRun(target)
}

async function selectRun(runId: string) {
  selectedRunId.value = runId
  historyEvents.value = []
  historyLoading.value = true
  try {
    const res = await getSubagentRunEvents(runId)
    historyEvents.value = res.events
  } catch {
    historyEvents.value = []
  } finally {
    historyLoading.value = false
  }
}

/**
 * 点「查看」：切到输出 tab **并**选中该 run。
 *
 * 修前只做 `selectedRunId = runId`：选中的是输出了，但用户还在「运行」tab 上，
 * 界面上除了一行高亮没有任何变化 —— 这就是"查看按钮点了没反应"的机械原因。
 * 外部定位（对话流里的工具卡）早就会切 tab，只有卡片上的入口漏了。
 */
function openRunDetails(runId: string): void {
  saTab.value = 'output'
  void selectRun(runId)
}

/** 面包屑的一个节点：`known=false` 表示父级已轮转出列表（只显示层级，不可点开）。 */
interface RunCrumb {
  runId: string
  label: string
  known: boolean
}

function runDisplayLabel(run: SubagentRunView): string {
  const profile = String(run.profile || '').trim()
  const short = run.run_id.length > 8 ? run.run_id.slice(-6) : run.run_id
  return profile ? `${profile} · ${short}` : `${t('agent.sub_agent')} ${short}`
}

/**
 * 选中 run 的层级链（主会话 → 最深祖先 → 当前 run）。
 *
 * 为什么需要：子 Agent 可以嵌套（`parent_run_id` 已经有完整血缘），但「输出」区
 * 只显示一个 run —— 用户看到一段过程时无法回答"它在替谁干活、上面还有几层"。
 * 面包屑把这条链一字排开，并且**可点祖先**直接切过去比对；根节点回运行列表。
 *
 * 父级不在列表里（后台 run 结束较久、已被轮转）时仍保留一个不可点的占位：
 * 层级关系本身就是信息，不能因为拿不到父级的正文就把这段关系抹掉。
 */
const runBreadcrumbs = computed<RunCrumb[]>(() => {
  const current = selectedRun.value
  if (!current) return []
  const byId = new Map<string, SubagentRunView>()
  for (const run of [...displayRuns.value, ...liveRuns.value]) byId.set(run.run_id, run)
  const ancestors: RunCrumb[] = []
  const seen = new Set<string>([current.run_id])
  let parentId = current.parent_run_id || ''
  while (parentId && !seen.has(parentId) && ancestors.length < 20) {
    seen.add(parentId)
    const parent = byId.get(parentId)
    if (!parent) {
      const short = parentId.length > 8 ? parentId.slice(-6) : parentId
      ancestors.unshift({
        runId: parentId,
        label: `${t('agent.sub_agent')} ${short}`,
        known: false,
      })
      break
    }
    ancestors.unshift({ runId: parent.run_id, label: runDisplayLabel(parent), known: true })
    parentId = parent.parent_run_id || ''
  }
  return [...ancestors, { runId: current.run_id, label: runDisplayLabel(current), known: true }]
})

/** 点祖先面包屑：切到那个 run（同一套"选中 + 切 tab"）。 */
function goRunCrumb(crumb: RunCrumb): void {
  if (!crumb.known || crumb.runId === selectedRunId.value) return
  openRunDetails(crumb.runId)
}

/** 面包屑根「主会话」：回到运行列表并清空选择。 */
function backToRunList(): void {
  selectedRunId.value = ''
  saTab.value = 'runs'
}

/** 已经为某个 run 补过"终态尾巴"的标记（避免终态后继续空轮询） */
const tailLoaded = new Set<string>()

/**
 * 轮询选中 run 的事件流。
 *
 * 为什么需要：选中 run 的事件只有两个来源 —— 父会话 SSE（`props.liveEvents`）与
 * `GET /v1/subagent/runs/{id}/events`。`run_in_background=true` 的子 Agent 在父 turn 结束后
 * 不再有 SSE（事件此时只落运行期缓存），所以必须靠这条轮询把增量拉回来。
 */
async function pollSelectedEvents() {
  const runId = selectedRunId.value
  if (!runId || historyLoading.value) return
  const current = displayRuns.value.find(r => r.run_id === runId)
  const status = String(current?.status || 'running')
  const terminal = TERMINAL_STATUSES.has(status)
  if (terminal && tailLoaded.has(runId)) return
  try {
    const res = await getSubagentRunEvents(runId)
    if (selectedRunId.value !== runId) return // 期间切换了 run：丢弃这次结果
    const seen = new Set(historyEvents.value.map(e => e.id).filter(Boolean) as string[])
    const fresh = res.events.filter(e => !e.id || !seen.has(e.id))
    if (fresh.length) historyEvents.value = [...historyEvents.value, ...fresh]
    if (terminal) tailLoaded.add(runId)
  } catch {
    /* 轮询失败不打断对话；下一次 tick 会再试 */
  }
}

function dismissCostHint() {
  showCostHint.value = false
  try {
    globalThis.localStorage?.setItem(COST_HINT_KEY, '1')
  } catch { /* 隐私模式下不可用，忽略 */ }
}

// 实时流里出现了新 run（通常是刚委派的）→ 立刻刷新树，让卡片尽快出现
/** 面板内部 tab（设计稿第四节 + 产物：运行 · 输出 · 用量 · 产物 · 事件流） */
const SA_TABS = [
  { id: 'runs', labelKey: 'chat.tabs.runs' },
  { id: 'output', labelKey: 'chat.tabs.output' },
  { id: 'usage', labelKey: 'chat.tabs.usage' },
  { id: 'artifacts', labelKey: 'chat.tabs.artifacts' },
  { id: 'events', labelKey: 'chat.tabs.events' },
] as const
const saTab = ref<(typeof SA_TABS)[number]['id']>('runs')

/**
 * 选中运行的**写入路径**。
 *
 * 后端把 `write_paths`（jsonb）**原样透传为字符串**，形状由这里解析 ——
 * 解析不了就当作空（观测面不该因为字段形状不合预期而报错）。
 */
const artifactPaths = computed<string[]>(() => {
  const raw = (selectedRun.value as { write_paths?: string } | null)?.write_paths
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === 'string') : []
  } catch {
    return []
  }
})

/** 按目录分组：一屏几十个路径平铺是读不出来的，按目录归拢后能一眼看出"动了哪几处" */
const artifactGroups = computed(() => {
  const groups = new Map<string, string[]>()
  for (const path of artifactPaths.value) {
    const idx = path.lastIndexOf('/')
    const dir = idx > 0 ? path.slice(0, idx) : '.'
    const list = groups.get(dir) ?? []
    list.push(path)
    groups.set(dir, list)
  }
  return [...groups.entries()]
    .map(([dir, paths]) => ({ dir, paths }))
    .sort((a, b) => a.dir.localeCompare(b.dir))
})

/** 最近一次已复制的路径（用于就地反馈，避免依赖全局 message 组件） */
const copiedPath = ref('')

async function copyPath(path: string) {
  try {
    await copyTextOrThrow(path)
    copiedPath.value = path
    window.setTimeout(() => {
      if (copiedPath.value === path) copiedPath.value = ''
    }, 1200)
  } catch {
    /* 剪贴板不可用（非安全上下文）：静默，不做假成功提示 */
  }
}

watch(() => props.liveEvents?.length || 0, (len, prev) => {
  if (!len || len === prev) return
  const latest = props.liveEvents?.[len - 1]
  if (latest && !runs.value.some(r => r.run_id === latest.run_id)) void loadRuns()
})

watch(() => props.sessionId, () => { selectedRunId.value = ''; historyEvents.value = []; void loadRuns() })

/**
 * 外部定位请求（对话流里点了某个子 Agent 工具卡）。
 *
 * 为什么看 `focusSeq` 而不是只比 run id：栏已经开着看 A，用户又点了 A —— 只比 id
 * 时值没变化，`watch` 不触发，"点了没反应"。序号让每次点击都是新值。
 *
 * 落在「输出」tab：用户点那一行要的是**过程与审批**（它做了什么、在等什么），
 * 而不是再看一遍运行列表。
 */
watch(
  () => [props.focusRunId, props.focusSeq] as const,
  ([id]) => {
    if (!id) return
    saTab.value = 'output'
    if (id !== selectedRunId.value) void selectRun(id)
    else void pollSelectedEvents()
  },
  { immediate: true },
)

onMounted(() => {
  try {
    showCostHint.value = globalThis.localStorage?.getItem(COST_HINT_KEY) !== '1'
  } catch { showCostHint.value = true }
  void loadRuns()
  // 3s tick：刷新运行树 + 拉选中 run 的增量事件（后台 run 没有 SSE，只能靠这条）
  timer = globalThis.setInterval(() => {
    void loadRuns()
    void pollSelectedEvents()
  }, 3000)
})

onBeforeUnmount(() => {
  if (timer) globalThis.clearInterval(timer)
  if (nowTimer !== null) globalThis.clearInterval(nowTimer)
})
</script>

<template>
  <div class="subagent-panel">
    <div
      v-if="showCostHint"
      class="cost-hint"
    >
      <span>{{ $t('workflow.sub_agents_consume_tokens_independently_cost_multiplies_when_multiple_subtasks_run_in_parallel') }}</span>
      <button
        type="button"
        class="cost-hint-close"
        @click="dismissCostHint"
      >
        {{ $t('common.got_it') }}
      </button>
    </div>

    <!-- 标题行可关：对照栏的外壳已经有一层标题（栏名 + 运行中计数 + 关闭），
         面板再画一遍只是同一句话说两次，还白占一行高度。 -->
    <div
      v-if="!hideHeader"
      class="panel-head"
    >
      <span class="panel-title">{{ $t('agent.sub_agent') }}</span>
      <span class="panel-meta">
        {{ runs.length }} {{ $t('common.running_3') }} ↑{{ totals.input }} / ↓{{ totals.output }}
        <!-- 近 24h 失败率：列表数据直接算出来，不需要额外接口 -->
        <Tooltip
          v-if="failureStats.total"
          :title="$t('agent.subagent_failure_rate_hint', { failed: failureStats.failed, total: failureStats.total })"
        >
          <span
            class="failure-rate"
            :class="{ bad: failureStats.rate >= 30 }"
          >{{ $t('agent.subagent_failure_rate', { rate: failureStats.rate }) }}</span>
        </Tooltip>
        <!-- 预算超限汇总：卡片各自高亮，这里是"有几条" -->
        <Tag
          v-if="budgetHitCount"
          color="warning"
          :title="$t('agent.subagent_budget_exceeded_hint')"
        >
          {{ $t('agent.subagent_budget_exceeded') }} ×{{ budgetHitCount }}
        </Tag>
        <Tooltip :title="$t('admin.data_source_redis_runtime_cache_db_history')">
          <Tag
            v-if="source"
            :color="source === 'redis' ? 'processing' : 'default'"
            class="source-tag"
          >
            {{ source }}
          </Tag>
        </Tooltip>
      </span>
    </div>

    <!-- 面板内部 tab（设计稿第四节）：运行 / 输出 / 用量 / 事件流。
         前两个是"看过程"，后两个是"算花销"与"排查原始事件"。
         用 v-show 而非重排 DOM：保留既有树与输出的全部逻辑，只切换可见性。 -->
    <nav
      class="sa-tabs"
      role="tablist"
      :aria-label="$t('agent.sub_agent_view')"
    >
      <button
        v-for="tab in SA_TABS"
        :key="tab.id"
        type="button"
        role="tab"
        class="sa-tab"
        :class="{ active: saTab === tab.id }"
        :aria-selected="saTab === tab.id"
        @click="saTab = tab.id"
      >
        {{ $t(tab.labelKey) }}
        <span
          v-if="tab.id === 'runs' && runs.length"
          class="sa-tab-count"
        >{{ runs.length }}</span>
        <span
          v-if="tab.id === 'events' && liveEvents.length"
          class="sa-tab-count"
        >{{ liveEvents.length }}</span>
      </button>
    </nav>

    <Spin
      v-show="saTab === 'runs'"
      :spinning="loading"
    >
      <Empty
        v-if="!displayRuns.length"
        :description="$t('agent.no_sub_agent_has_run_in_this_session_yet')"
        :image="Empty.PRESENTED_IMAGE_SIMPLE"
      />
      <div
        v-else
        class="timeline"
      >
        <!-- 一次清场：只在确实有运行中的 run 时才出现 -->
        <div
          v-if="hasRunning"
          class="bulk-stop"
        >
          <button
            type="button"
            class="run-stop"
            @click.stop="stopAll"
          >
            {{ $t('agent.stop_all_sub_agents') }}
          </button>
        </div>
        <!-- 每个 run 一张时间线卡片。`key` 用 run_id：事件更新只改 props，
             不会重建卡片（整表重渲染正是要避免的）。 -->
        <SubagentRunCard
          v-for="run in visibleRuns"
          :key="run.run_id"
          :run="run"
          :depth="run.depth"
          :selected="run.run_id === selectedRunId"
          :child-count="childCount.get(run.run_id) || 0"
          :collapsed="collapsed.has(run.run_id)"
          :live-tail="liveTail[run.run_id] || ''"
          :pending="pendingByRun[run.run_id] || 0"
          :cancelling="cancelling.has(run.run_id)"
          :rerunning="rerunning.has(run.run_id)"
          :now="nowTick"
          @select="openRunDetails(run.run_id)"
          @toggle="toggleCollapse(run.run_id)"
          @stop="stopRun(run.run_id)"
          @pending="focusApprovals(run.run_id)"
          @rerun="rerunRun(run)"
        />
      </div>
    </Spin>

    <!-- 用量：按 run 列明细。会话语义在「统计」浮层，这里是**子 Agent 粒度**，两者不重复 -->
    <div
      v-show="saTab === 'usage'"
      class="usage"
    >
      <div
        v-if="runs.length"
        class="usage-grid"
      >
        <div class="usage-row usage-head">
          <span>{{ $t('common.run') }}</span>
          <span>{{ $t('common.status') }}</span>
          <span class="num">in</span>
          <span class="num">out</span>
          <span class="num">{{ $t('workflow.steps') }}</span>
        </div>
        <div
          v-for="run in runs"
          :key="run.run_id"
          class="usage-row"
        >
          <span class="u-name">{{ run.profile || run.run_id }}</span>
          <Tag :color="STATUS_COLOR[run.status] || 'default'">
            {{ run.status }}
          </Tag>
          <span class="num">{{ run.usage?.input_tokens || 0 }}</span>
          <span class="num">{{ run.usage?.output_tokens || 0 }}</span>
          <span class="num">{{ run.usage?.steps || 0 }}</span>
        </div>
      </div>
      <div
        v-else
        class="sa-empty"
      >
        {{ $t('agent.no_sub_agent_has_run_yet_so_there_is_no_usage_to_compute') }}
      </div>
      <p class="usage-note">
        {{ $t('agent.sub_agents_consume_tokens_independently_cost_multiplies_when_several_run_in_parallel') }}
      </p>
    </div>

    <!-- 事件流：**原始** subagent.* 事件（未按语义分组），排查"到底发生了什么"时用；
         「输出」tab 才是给人读的结构化过程。 -->
    <div
      v-show="saTab === 'events'"
      class="events"
    >
      <div
        v-for="(e, i) in liveEvents"
        :key="(e.id as string) || i"
        class="ev-row"
      >
        <span class="ev-type">{{ e.type }}</span>
        <span class="ev-run">{{ e.run_id }}</span>
        <span class="ev-text">{{ e.content || '' }}</span>
      </div>
      <div
        v-if="!liveEvents.length"
        class="sa-empty"
      >
        {{ $t('agent.no_sub_agent_events_received_in_this_session_yet') }}
      </div>
    </div>

    <!-- 产物：这次运行**写入了哪些文件**（`write_paths`）。
         按目录分组 —— 一屏几十个路径平铺是读不出来的，归拢后能一眼看出"动了哪几处"。
         刻意不做图表/指标：ZCode 的 ArtifactChart/Metrics 围绕其沙箱产物（带结构化元数据），
         而我们目前只有路径，先把这个真实有用的最小版做好。 -->
    <div
      v-show="saTab === 'artifacts'"
      class="artifacts"
    >
      <template v-if="selectedRun">
        <div
          v-if="!artifactPaths.length"
          class="sa-empty"
        >
          {{ $t('media.this_run_did_not_write_any_files') }}
        </div>
        <div
          v-for="group in artifactGroups"
          :key="group.dir"
          class="art-group"
        >
          <div class="art-dir">
            {{ group.dir }}
          </div>
          <button
            v-for="path in group.paths"
            :key="path"
            type="button"
            class="art-item"
            :class="{ copied: copiedPath === path }"
            :title="path"
            @click="copyPath(path)"
          >
            {{ copiedPath === path ? $t('common.copied_2') : path.split('/').pop() }}
          </button>
        </div>
      </template>
      <div
        v-else
        class="sa-empty"
      >
        {{ $t('agent.select_a_sub_agent_in_run_first') }}
      </div>
    </div>

    <!-- 输出区：**与主 Agent 同一套渲染**（工具调用卡 + 工具结果块），
         再加一条多个 run 的并列 tab 条。
         外层的 v-show 负责 tab 切换；内层 v-if 负责"无选中不渲染"（同时让 TS 把
         selectedRun 收窄为非空）。 -->
    <div
      v-show="saTab === 'output'"
      class="output"
    >
      <!-- 层级面包屑：子 Agent 可以嵌套，「输出」区又只显示一个 run —— 这条链回答
           "它在替谁干活、上面还有几层"，祖先可点、根节点回运行列表。 -->
      <nav
        v-if="runBreadcrumbs.length"
        class="run-breadcrumb"
        data-testid="run-breadcrumb"
        :aria-label="$t('agent.subagent_breadcrumb_aria')"
      >
        <button
          type="button"
          class="crumb crumb-root"
          @click="backToRunList()"
        >
          {{ $t('agent.subagent_breadcrumb_root') }}
        </button>
        <template
          v-for="(crumb, index) in runBreadcrumbs"
          :key="crumb.runId"
        >
          <span
            class="crumb-sep"
            aria-hidden="true"
          >›</span>
          <span
            v-if="index === runBreadcrumbs.length - 1"
            class="crumb crumb-current"
            aria-current="page"
          >{{ crumb.label }}</span>
          <button
            v-else
            type="button"
            class="crumb"
            :disabled="!crumb.known"
            :title="crumb.label"
            @click="goRunCrumb(crumb)"
          >
            {{ crumb.label }}
          </button>
        </template>
      </nav>
      <!-- 多个子 Agent **并列成 tab**：一个会话里常有多个 run（并行委派、嵌套子代理），
           一次只看一个就逼用户回「运行」页来回点。tab 条让"切到那一个"只需一次点击，
           与"同一类东西并排摆放"的直觉一致。
           只有一个 run 时不画这条：多一行 tab 只是噪音。 -->
      <div
        v-if="outputTabs.length > 1"
        class="output-tabs"
        role="tablist"
        :aria-label="$t('agent.sub_agent_view')"
      >
        <button
          v-for="run in outputTabs"
          :key="run.run_id"
          type="button"
          role="tab"
          class="output-tab"
          :class="{ active: run.run_id === selectedRunId }"
          :aria-selected="run.run_id === selectedRunId"
          :title="`${run.profile || run.run_id} · ${run.status}`"
          @click="selectRun(run.run_id)"
        >
          <span
            class="output-tab-dot"
            :data-state="TERMINAL_STATUSES.has(run.status) ? 'done' : 'live'"
            aria-hidden
          />
          <span class="output-tab-name">{{ run.profile || run.run_id.slice(0, 8) }}</span>
          <!-- 待处理（审批/提问）的 run 必须在 tab 上就看得出来：
               否则用户要逐个点开才知道"哪个在等我"。 -->
          <span
            v-if="pendingByRun[run.run_id]"
            class="output-tab-pending"
            :title="$t('agent.sub_agent_is_waiting_for_your_confirmation_or_answer')"
          >⚠{{ pendingByRun[run.run_id] > 1 ? pendingByRun[run.run_id] : '' }}</span>
        </button>
      </div>

      <template v-if="selectedRun">
        <div class="output-head">
          <span>{{ selectedRun.profile || selectedRun.run_id }}</span>
          <span class="output-meta">
            {{ $t('workflow.steps') }} {{ selectedRun.usage?.steps || 0 }}
          </span>
        </div>
        <div class="output-summary">
          {{ selectedRun.summary || $t('common.running_no_summary_yet') }}
        </div>
        <!-- F1：运行中的子 Agent 可直接补一句（不打断当前 run，下一轮生效） -->
        <div
          v-if="canSteer"
          class="steer-box"
        >
          <input
            v-model="steerText"
            class="steer-input"
            type="text"
            maxlength="2000"
            :placeholder="$t('agent.subagent_steer_placeholder')"
            :disabled="steering"
            @keydown.enter.prevent="sendSteer"
          >
          <button
            type="button"
            class="steer-send"
            :disabled="steering || !steerText.trim()"
            @click="sendSteer"
          >
            {{ steering ? $t('agent.subagent_steer_sending') : $t('agent.subagent_steer_send') }}
          </button>
        </div>
        <Spin :spinning="historyLoading">
          <!-- 被行数上限折叠掉的部分要如实说明：静默截断会让用户以为
               "它只做了这么多"，而「事件流」tab 里明明还有更早的内容。 -->
          <div
            v-if="outputHiddenRows"
            class="output-hidden"
          >
            {{ $t('common.n_more_items_above', { n: outputHiddenRows }) }}
          </div>
          <div class="stream">
            <template
              v-for="row in outputRows"
              :key="row.key"
            >
              <div
                v-if="row.kind === 'reasoning'"
                class="line reasoning"
              >
                <span class="line-tag">{{ $t('chat.reasoning') }}</span>{{ row.content }}
              </div>
              <div
                v-else-if="row.kind === 'text'"
                class="line text"
              >
                {{ row.content }}
              </div>
              <div
                v-else-if="row.kind === 'notice'"
                class="line notice"
              >
                {{ row.content }}
              </div>
              <!-- 工具过程：**复用主对话流的组件**。摘要、参数展开、失败配色
                   都只有一份实现，两个界面不可能漂移。 -->
              <div
                v-else-if="row.kind === 'tool'"
                class="line tool"
              >
                <ToolCallCard
                  v-if="row.call"
                  :item="row.call"
                />
                <ToolResultBlock
                  v-if="row.result"
                  :item="row.result"
                  :session-id="sessionId"
                />
              </div>
              <div
                v-else-if="row.kind === 'approval'"
                class="line approval"
              >
                <SubAgentApprovalCard
                  :tool-name="row.event.tool_name || ''"
                  :args="row.event.tool_arguments || ''"
                  :content="row.event.content || ''"
                  :submitting="approvalSubmitting.has(row.event.tool_call_id || '')"
                  :decision="approvalDecisions[row.event.tool_call_id || ''] || ''"
                  :error="approvalErrors[row.event.tool_call_id || ''] || ''"
                  @decide="(approved: boolean) => resolveApproval({ toolCallId: row.event.tool_call_id || '' }, approved)"
                  @edit="(args: string) => resolveApproval({ toolCallId: row.event.tool_call_id || '' }, true, args)"
                />
              </div>
              <div
                v-else-if="row.kind === 'ask'"
                class="line ask"
              >
                <AskCard
                  :question="row.event.question || row.event.content || ''"
                  :options="row.event.options || []"
                  :allow-free-text="row.event.allow_free_text !== false"
                  :expired="!!askErrors[row.event.tool_call_id || '']"
                  @answer="(value: string) => resolveAsk(row.event.tool_call_id || '', value)"
                />
                <div
                  v-if="askAnswers[row.event.tool_call_id || '']"
                  class="ask-done"
                >
                  {{ $t('chat.replied') }}：{{ askAnswers[row.event.tool_call_id || ''] }}
                </div>
                <div
                  v-if="askErrors[row.event.tool_call_id || '']"
                  class="approval-error"
                >
                  {{ askErrors[row.event.tool_call_id || ''] }}
                </div>
              </div>
              <div
                v-else-if="row.kind === 'status'"
                class="line status"
              >
                {{ row.event.status }}<span v-if="row.event.truncated"> {{ $t('common.preview_truncated') }}</span>
              </div>
              <!-- G7：过程事件被丢弃时必须说出来（否则"少了几行"看起来像"模型没做"） -->
              <div
                v-else-if="row.kind === 'done' && droppedNote"
                class="line dropped"
              >
                {{ $t('chat.subagent_events_dropped', { n: droppedNote }) }}
              </div>
              <div
                v-else-if="row.kind === 'done'"
                class="line done"
              >
                {{ $t('common.end') }}：{{ row.event.status }}
                <span v-if="row.event.usage"> · ↑{{ row.event.usage.input_tokens || 0 }}/↓{{ row.event.usage.output_tokens || 0 }}</span>
              </div>
            </template>
            <div
              v-if="!outputRows.length"
              class="stream-empty"
            >
              {{ $t('errors.no_output_yet_running_or_process_expired') }}
            </div>
          </div>
        </Spin>
      </template>
      <div
        v-else
        class="sa-empty"
      >
        {{ $t('agent.select_a_sub_agent_in_run_first') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.subagent-panel { display: flex; flex-direction: column; gap: 10px; padding: 8px; font-size: var(--fs-sm); }

.cost-hint {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px 10px; border-radius: var(--radius-md);
  background: var(--warning-bg, rgba(245, 158, 11, 0.12));
  color: var(--text-secondary, #595959); line-height: 1.5;
}
.cost-hint-close {
  flex: none; border: none; background: none; cursor: pointer;
  color: var(--primary-color, #1677ff); font-size: var(--fs-sm);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.cost-hint-close:hover { color: var(--text-primary); }

.panel-head { display: flex; align-items: center; justify-content: space-between; }
.panel-title { font-weight: var(--fw-semibold); }
.panel-meta { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); display: flex; align-items: center; gap: 6px; }
.source-tag { margin: 0; }
/* 失败率：平常是中性数字，≥30% 才染成警示色（否则一整片红色等于没有红） */
.failure-rate { font-variant-numeric: tabular-nums; color: var(--text-tertiary, #8c8c8c); }
.failure-rate.bad { color: var(--warning); font-weight: var(--fw-medium); }

.timeline { display: flex; flex-direction: column; }
.tree-row {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 8px; border-radius: var(--radius-md); cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.tree-row:hover { background: var(--surface-2, rgba(127, 127, 127, 0.08)); }
.tree-row.active { background: var(--primary-light, var(--shadow-brand)); }
.twisty {
  width: 14px; flex: none; border: none; background: none; cursor: pointer;
  color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); padding: 0;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.twisty:hover { color: var(--text-primary); }
.twisty.placeholder { cursor: default; }
.run-profile { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-usage, .run-redacted { font-size: var(--fs-xs); color: var(--text-tertiary, #8c8c8c); }
/* 中止按钮：紧凑型，别把行撑高；hover 用错误色（停止是破坏性动作） */
.run-stop {
  flex: none; padding: 0 6px; line-height: 18px; cursor: pointer;
  font-size: var(--fs-xs); color: var(--text-secondary, #595959);
  background: transparent; border: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.24));
  border-radius: var(--radius-sm);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.run-stop:hover:not(:disabled) { color: var(--error, var(--error)); border-color: var(--error, var(--error)); }
.run-stop:disabled { opacity: 0.5; cursor: default; }
.bulk-stop { display: flex; justify-content: flex-end; padding: 0 8px 6px; }

/* 运行中 run 的实时预览：尾巴对齐（看到的永远是最新输出），完整内容点开看 */
.run-tail {
  flex: 1 1 auto; min-width: 40px; max-width: 50%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: var(--fs-xs); color: var(--text-tertiary, #8c8c8c);
}

/* 待确认标记：让"有东西在等我"在不点开该 run 时也看得见 */
.run-approval {
  flex: none; padding: 0 6px; line-height: 18px; cursor: pointer;
  font-size: var(--fs-xs); color: var(--warning, var(--warning));
  background: transparent; border: var(--hairline-width) solid var(--warning, var(--warning)); border-radius: var(--radius-sm);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.run-approval:hover { filter: brightness(0.92); }

/* 子 Agent 审批卡片的视觉在 SubAgentApprovalCard.vue（与主对话区的确认卡片同一套） */
.line.approval { width: 100%; }
/* 提问卡片复用主对话流的 AskCard —— 只要撑满宽度 */
.line.ask { width: 100%; }
.ask-done { margin-top: 6px; font-size: var(--fs-xs); color: var(--success, #16a34a); }

.output { border-top: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.24)); padding-top: 10px; }
/* 层级面包屑：一行、可横向收缩（深层嵌套时截断的是中间标签，不是整条链）。
   祖先可点 → 直接切过去比对；根节点回运行列表。 */
.run-breadcrumb {
  display: flex; align-items: center; flex-wrap: wrap; gap: 2px;
  margin-bottom: 8px; font-size: var(--fs-xs); color: var(--text-secondary, #595959);
  min-width: 0;
}
.run-breadcrumb .crumb {
  max-width: 14em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 1px 4px; border: none; background: none; border-radius: var(--radius-sm, 4px);
  font-size: var(--fs-xs); color: var(--text-secondary, #595959); cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease;
}
.run-breadcrumb .crumb:hover:not(:disabled) { color: var(--primary-color, #1677ff); background: var(--bg-hover, rgba(0, 0, 0, 0.03)); }
.run-breadcrumb .crumb:disabled { cursor: default; opacity: 0.7; }
.run-breadcrumb .crumb-current { color: var(--text-primary); font-weight: var(--fw-medium); cursor: default; }
.run-breadcrumb .crumb-root { font-weight: var(--fw-medium); }
.run-breadcrumb .crumb-sep { color: var(--text-tertiary, #8c8c8c); flex: none; }
.output-head { display: flex; justify-content: space-between; font-weight: var(--fw-semibold); }
.output-meta { font-size: var(--fs-xs); color: var(--text-tertiary, #8c8c8c); }

/* 多个 run 的并列 tab 条：横向滚动而不是换行 —— 换行会把过程视图挤下去，
   而这里每一格都只是"切到谁"的入口。 */
.output-tabs {
  display: flex; gap: 4px; margin-bottom: 8px;
  overflow-x: auto; scrollbar-width: thin;
}
.output-tab {
  flex: none; display: inline-flex; align-items: center; gap: 5px;
  max-width: 180px; padding: 3px 8px;
  border: var(--hairline-width) solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: transparent; color: var(--text-secondary, #595959);
  font-size: var(--fs-xs); cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.output-tab:hover { background: var(--bg-hover); color: var(--text-primary); }
.output-tab.active { background: var(--primary-bg); border-color: var(--primary); color: var(--primary); }
.output-tab-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 运行中/已结束的状态点：一点颜色差就能看出"还剩几个在跑" */
.output-tab-dot {
  flex: none; width: 6px; height: 6px; border-radius: var(--radius-full);
  background: var(--text-tertiary, #8c8c8c);
}
.output-tab-dot[data-state='live'] { background: var(--primary); animation: sa-pulse var(--dur-pulse) ease-in-out infinite; }
@keyframes sa-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
@media (prefers-reduced-motion: reduce) {
  .output-tab-dot[data-state='live'] { animation: none; }
}
/* 待处理角标：哪个 run 在等我，必须在 tab 上就能看出来 */
.output-tab-pending { flex: none; color: var(--warning, #d97706); font-weight: var(--fw-semibold); }

/* 被行数上限折叠掉的部分：如实说明，而不是静默截断 */
.output-hidden {
  margin-bottom: 6px; padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
  color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs);
}
.output-summary {
  margin: 6px 0; padding: 8px; border-radius: var(--radius-md);
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
  white-space: pre-wrap; max-height: 160px; overflow: auto;
}
.stream { max-height: 320px; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.line { white-space: pre-wrap; line-height: 1.5; }
.line.reasoning { color: var(--text-tertiary, #8c8c8c); font-style: italic; }
.line.notice { color: var(--warning, var(--warning)); }
.line.status, .line.done { color: var(--text-secondary, #595959); font-size: var(--fs-xs); }
.line-tag {
  display: inline-block; margin-inline-end: 4px; padding: 0 4px; border-radius: var(--radius-sm); font-size: var(--fs-xs);
  background: var(--surface-2, rgba(127, 127, 127, 0.12));
}
.stream-empty { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }

/* ── 内部 tab（运行 · 输出 · 用量 · 事件流）── */
.sa-tabs {
  display: flex; gap: 2px; margin: 8px 0 10px;
  padding: 2px; border-radius: var(--radius-lg);
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
}
.sa-tab {
  flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  padding: 4px 8px; border: none; border-radius: var(--radius-md); cursor: pointer;
  background: transparent; color: var(--text-secondary, #595959); font-size: var(--fs-xs);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.sa-tab:hover { color: var(--text-primary, #262626); }
.sa-tab.active {
  background: var(--bg-elevated, var(--on-solid)); color: var(--text-primary, #262626);
  font-weight: var(--fw-semibold); box-shadow: var(--shadow-xs);
}
.sa-tab-count {
  min-width: 16px; padding: 0 4px; border-radius: var(--radius-lg);
  background: var(--surface-2, rgba(127, 127, 127, 0.16));
  font-size: var(--fs-xs); font-variant-numeric: tabular-nums;
}
.sa-empty { padding: 10px 2px; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }

/* 用量表：数字列右对齐 + tabular-nums，便于竖排比对 */
.usage-grid { display: flex; flex-direction: column; }
.usage-row {
  display: grid; grid-template-columns: 1fr auto 56px 56px 48px;
  align-items: center; gap: 6px; padding: 4px 2px;
  border-bottom: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.12));
  font-size: var(--fs-xs);
}
.usage-head { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
.usage-row .num { text-align: end; font-variant-numeric: tabular-nums; }
.u-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.usage-note { margin: 10px 0 0; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }

/* 事件流：原始事件，等宽 + 紧凑行（--font-mono 已含 CJK 字体栈，中文不会掉宋体） */
.events { display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow: auto; }
.ev-row { display: grid; grid-template-columns: 132px 96px 1fr; gap: 6px; font-size: var(--fs-xs); }
.ev-type { color: var(--primary, #1677ff); font-family: var(--font-mono); }
.ev-run { color: var(--text-tertiary, #8c8c8c); font-family: var(--font-mono); }
.ev-text {
  color: var(--text-secondary, #595959);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 产物 tab：按目录分组 + 点击复制（空态复用 .sa-empty） */
.artifacts { display: flex; flex-direction: column; gap: 8px; }
.art-group { display: flex; flex-direction: column; gap: 2px; }
.art-dir { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); font-family: var(--font-mono); }
.art-item {
  padding: 3px 6px; border: none; border-radius: var(--radius-sm); cursor: pointer;
  background: transparent; color: var(--text-secondary, #595959);
  font-size: var(--fs-xs); font-family: var(--font-mono); text-align: start;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.art-item:hover { background: var(--surface-2, rgba(127, 127, 127, 0.08)); color: var(--text-primary, #262626); }
.art-item.copied { color: var(--success, #16a34a); }
/* F1：子 Agent steer 输入条 */
.steer-box {
  display: flex; gap: 6px; padding: 6px 10px;
  border-top: var(--hairline-width) solid var(--border-subtle);
  background: var(--bg-secondary);
}
.steer-input {
  flex: 1; min-width: 0; font-size: var(--fs-xs);
  padding: 4px 8px; border-radius: var(--radius-sm);
  border: var(--hairline-width) solid var(--border-subtle);
  background: var(--bg-card); color: var(--text-primary);
}
.steer-input:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }
.steer-send {
  flex: none; font-size: var(--fs-xs); padding: 4px 10px;
  border-radius: var(--radius-sm); border: none;
  background: var(--primary); color: var(--on-primary); cursor: pointer;
}
.steer-send:disabled { opacity: .5; cursor: default; }
</style>
