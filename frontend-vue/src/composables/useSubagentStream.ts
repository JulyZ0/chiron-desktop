/**
 * 子 Agent 的实时流式通道（从 `ChatView.vue` 整块抽出，**行为逐字不变**）。
 *
 * 这里装的是"子 Agent 事件怎么到达聊天页"的三段：
 *
 * 1. **会话级常驻 SSE**（`ensureSubagentStream` / `closeSubagentStream`）——
 *    一轮对话的 SSE 在 `turn_done` 就关闭了，而默认**后台派发**的子 Agent
 *    （`run_in_background=true`）通常还在跑：它后续的进度与 `subagent.approval`
 *    （等你批准）没有任何通道到达前端 —— 表现就是"必须刷新页面才能看到子 Agent
 *    的后续请求"（刷新后靠面板的 3 秒轮询把历史补回来）。
 * 2. **50ms 合批**（`SUBAGENT_FLUSH_MS` / `subagentBuffer`）—— 子 Agent 一轮工具
 *    调用会连发几十条事件，逐条写响应式数组会让面板（以及侧栏角标、会话状态点）
 *    每秒重算几十次，这才是"响应式风暴"的来源。50ms 合批后最多 20 次/秒，肉眼
 *    无差别，重算次数降一个量级。
 * 3. **按 run 分组**（`buffer` 以 run 状态对象为键）—— 用户可能在 50ms 内切了会话，
 *    若把缓冲统一写进"当前会话"，A 会话的子 Agent 事件就会串到 B 会话的面板里
 *    （ChatView 此前修过的那个 bug）。记下事件到达时属于哪个 run 状态，切会话也
 *    不会写错地方。
 *
 * ## 单写者约定（通道收敛）
 *
 * 同一会话**只能有一条** subagent SSE。唯一入口是构造时建立的
 * `watch(sessionId, ensureSubagentStream, { immediate: true })`；`ensureSubagentStream`
 * 自身对"同会话已连接"是幂等的（重复调用不新建连接）。因此它**不该**被别处再调 ——
 * 需要新入口时先想清楚"两条连接会各送一份同样的帧"，再决定是不是真的需要。
 * `composables/__tests__/useSubagentStream.spec.ts` 钉住了这条不变量。
 *
 * ## 为什么本轮 SSE 开着时跳过
 *
 * 本轮 `activeSSE` 仍在时，同一条事件会被两个连接各送一份；交给 `activeSSE` 处理
 * 即可，避免面板里出现重复项（`isTurnStreamOpen` 就是那个开关）。
 *
 * ⚠️ 这个开关**按会话**判定（H59）：此前它读的是 ChatView 的全局单句柄，只要**任何**
 * 一个会话的主流动着，所有会话的子 Agent 事件就都被丢掉 —— 后台会话（默认
 * `run_in_background=true`）的子 Agent 进度、审批、提问全都不出现，看起来像"它没在跑"。
 *
 * ## 按 run 去重 + Last-Event-ID 回放（H59）
 *
 * 断线重连时服务端会按 `Last-Event-ID` **补发**断口之后的事件，补发窗口与实时推送天然
 * 重叠（多实例下还会各扇出一份）。不去重的话面板里会出现两张一样的审批卡、两条一样的
 * 状态行。判据是帧 id（`SSEFrameMeta.id`）：同一 run 内见过就丢。游标**按会话**记
 * （`lastIdBySession`）—— 这是"游标按 session 建图"的第二条通道（第一条是 ChatView 的
 * 会话流游标）。
 */
import { computed, watch, type ComputedRef, type WritableComputedRef } from 'vue'
import {
  createSSEConnection,
  SSE_SILENT_TIMEOUT_MS,
  type SSEConnectionOptions,
  type SSEFrameMeta,
  type SSEHandle,
} from '../api'
import type { SubagentEvent } from '../api/subagent'

/** 合批窗口：见文件头 §2 */
export const SUBAGENT_FLUSH_MS = 50

/** 单会话实时事件缓冲上限（超出丢最早的那批） */
export const SUBAGENT_LIVE_MAX = 500

/**
 * 子 Agent 事件落在哪：`SessionRunState` 里与 `items` / `loading` 同层的那一份。
 *
 * 只取用到的字段 —— 接受任何"有 `subagentEvents` 数组"的 run 状态即可，
 * 不把 `SessionRunState` 的类型绑进来（它是 ChatView 的私有类型）。
 */
export interface SubagentRunStateLike {
  subagentEvents: SubagentEvent[]
}

export interface UseSubagentStreamOptions {
  /** 当前选中会话 id（空串 = 无会话，不建连接） */
  sessionId: () => string
  /** 事件归属的 run 状态：写作用域优先，其次当前视图会话 */
  resolveRun: () => SubagentRunStateLike
  /**
   * 该会话本轮主 SSE 是否开着（开着就跳过处理，避免同一帧被两个连接各送一份）。
   *
   * **必须按会话判**：不传参数（旧调用方）时退化为"任一主流开着就跳过"，那正是 H59 要修的
   * 丢事件路径 —— 视图层应当把 sid 传进来。
   */
  isTurnStreamOpen: (sid?: string) => boolean
}

export interface SubagentStream {
  /** 当前会话的子 Agent 实时事件（可写代理到 `run.subagentEvents`，模板/角标直接读） */
  subagentLiveEvents: WritableComputedRef<SubagentEvent[]> | ComputedRef<SubagentEvent[]>
  /** 把一条子 Agent 事件并入所属会话的实时缓冲（两个来源共用，见 `ensureSubagentStream`）。
   *  第二个参数是帧 id（`SSEFrameMeta.id`）：给了就按 run 去重（H59）。 */
  pushSubagentEvent: (raw: unknown, frameId?: string) => void
  /** 立刻落盘待写缓冲（卸载/测试用；正常路径由 50ms 定时器驱动） */
  flushSubagentBuffer: () => void
  /** 为当前会话确保一条常驻连接（同会话重复调用是幂等的） */
  ensureSubagentStream: () => void
  /** 关掉常驻连接并复位会话标记 */
  closeSubagentStream: () => void
  /** 卸载：关连接 + 冲刷未落盘批次 + 清掉定时器 */
  disposeSubagentStream: () => void
}

export function useSubagentStream(options: UseSubagentStreamOptions): SubagentStream {
  /**
   * 按 run 状态对象分组（不是全局数组，理由见文件头 §3）。
   *
   * 放在闭包内而不是模块级：一条通道属于**一个** ChatView 实例，两份状态互不可见 ——
   * 模块级 Map 会让前后两个实例（例如路由来回切换）共用同一份待写缓冲。
   */
  const buffer = new Map<SubagentRunStateLike, SubagentEvent[]>()
  let flushHandle: number | null = null
  let sse: SSEHandle | null = null
  let sseSession = ''
  /** 每个会话最后收到的帧 id：重连时作为 `Last-Event-ID` 回传（**按会话**，H59） */
  const lastIdBySession = new Map<string, string>()
  /** 每个 run 已处理过的帧 id：补发与实时推送重叠时只留第一次（H59） */
  const seenIdsByRun = new Map<string, Set<string>>()
  /** 单 run 去重窗口；超出就整体丢弃重来（宁可重复一帧，也不无限长高） */
  const SEEN_IDS_MAX = 2000

  /** 这个帧 id 在该 run 里是不是第一次见（无 id 的帧一律算新） */
  function isFirstInRun(runId: string, frameId: string): boolean {
    if (!frameId) return true
    let seen = seenIdsByRun.get(runId)
    if (!seen) {
      seen = new Set<string>()
      seenIdsByRun.set(runId, seen)
    }
    if (seen.has(frameId)) return false
    if (seen.size >= SEEN_IDS_MAX) seen.clear()
    seen.add(frameId)
    return true
  }

  /**
   * 实时事件缓冲（有界）：SSE 里的 `subagent.*` 分流到这里，只供侧边栏观测面板消费，
   * 绝不混入主对话流（旁路见 python-engine/app/agent/event_sink.py）。
   *
   * 对外是可写 computed —— 既有读写（push / length / 模板传参）一行都不用改。
   */
  const subagentLiveEvents = computed<SubagentEvent[]>({
    get: () => options.resolveRun().subagentEvents,
    set: (v) => {
      options.resolveRun().subagentEvents = v
    },
  })

  function flushSubagentBuffer() {
    flushHandle = null
    if (!buffer.size) return
    for (const [run, events] of buffer) {
      if (!events.length) continue
      // 一次性拼接 + 截断：整个批次只触发一次响应式更新
      const merged = [...run.subagentEvents, ...events]
      run.subagentEvents = merged.length > SUBAGENT_LIVE_MAX
        ? merged.slice(merged.length - SUBAGENT_LIVE_MAX)
        : merged
    }
    buffer.clear()
  }

  function pushSubagentEvent(raw: unknown, frameId = '') {
    const event = raw as SubagentEvent
    const type = typeof event?.type === 'string' ? event.type : ''
    if (!type.startsWith('subagent.') || !event?.run_id) return
    // 按 run 去重：重连补发与实时推送会给同一帧各送一份（见文件头）
    if (!isFirstInRun(String(event.run_id), frameId)) return
    const run = options.resolveRun()
    const pending = buffer.get(run)
    if (pending) pending.push(event)
    else buffer.set(run, [event])
    if (flushHandle === null) {
      flushHandle = window.setTimeout(flushSubagentBuffer, SUBAGENT_FLUSH_MS)
    }
  }

  function closeSubagentStream() {
    if (sse) {
      sse.close()
      sse = null
    }
    sseSession = ''
  }

  function ensureSubagentStream() {
    const sid = options.sessionId() || ''
    if (!sid) {
      closeSubagentStream()
      return
    }
    // 幂等点：同会话已连接就不再建第二条（见文件头「单写者约定」）
    if (sse && sseSession === sid) return
    closeSubagentStream()
    sseSession = sid
    // H59：重连时带上本会话的游标（服务端据此补发断口之后的事件）；
    // 首次建连没有游标 ⇒ 选项对象保持原样（不带空值头，见 api/index.ts 的说明）。
    const resume = lastIdBySession.get(sid) || ''
    const streamOpts: SSEConnectionOptions = {
      autoReconnect: true,
      // 没有心跳看门狗时，半开连接（休眠/代理掐长连接）不会报错也不会 EOF：
      // 子 Agent 的审批卡与进度会**永久静默**，只有切会话/刷新才恢复。
      heartbeatTimeoutMs: SSE_SILENT_TIMEOUT_MS,
    }
    if (resume) streamOpts.initialLastEventId = resume
    sse = createSSEConnection(
      sid,
      (raw, meta: SSEFrameMeta) => {
        const frameId = meta?.id || ''
        // 游标要**先记**（即使这一帧因为主流开着而被跳过）：游标代表"我已经看到这里"，
        // 不记就会在下次重连时把已经交出去的这段再补一遍。
        if (frameId) lastIdBySession.set(sid, frameId)
        if (!options.isTurnStreamOpen(sid)) pushSubagentEvent(raw, frameId)
      },
      // 连续重连失败（网络长时间不可用/会话已失效）：放弃这条，等下次会话切换或刷新重建
      () => { closeSubagentStream() },
      streamOpts,
    )
  }

  function disposeSubagentStream() {
    closeSubagentStream()
    // 合批缓冲：卸载时先把没落盘的那批冲刷掉（否则最后几十条事件随组件一起消失），
    // 再清掉定时器，避免回调在已卸载的组件上继续写状态。
    if (flushHandle !== null) {
      clearTimeout(flushHandle)
      flushHandle = null
      flushSubagentBuffer()
    }
  }

  // 唯一建连入口：会话切换时重建（immediate：首屏就要有）。
  watch(() => options.sessionId(), () => { ensureSubagentStream() }, { immediate: true })

  return {
    subagentLiveEvents,
    pushSubagentEvent,
    flushSubagentBuffer,
    ensureSubagentStream,
    closeSubagentStream,
    disposeSubagentStream,
  }
}
