/**
 * 多会话运行状态（从 `ChatView.vue` 抽出的第二刀）。
 *
 * ## 为什么必须按会话
 *
 * 此前 `loading` / `items` 是**单值**：切会话必须打断上一个会话（清空 items + 无条件置
 * loading），而后台会话的 SSE 并没有被关闭 —— 它的事件会写进**当前**会话的列表（串台）。
 *
 * 现在存储按会话，对外仍是 `items` / `loading` 这两个名字：用 **可写 computed** 代理到
 * 当前会话的切片。于是视图里既有读写点（`items.value` 50 处 + `loading.value` 19 处）
 * 一行都不用改，却天然按会话隔离。
 *
 * ## `gen` 与 `turnSeq`
 *
 * - `gen`：会话运行时被**重建**（重开同一 id、重新建流）时递增。只比 sessionId 不够 ——
 *   同一 id 可能对应前后两次生命周期，旧回调必须靠 gen 才知道"我已经不属于这个会话了"。
 * - `turnSeq`：每提交一轮 +1。gap 重拉要防的是"拉取期间又开跑了一轮（并且已经跑完）"，
 *   那种情形下 loading 已是 false、gen 也没变，只有回合序号能证明"这份快照是上一轮的"。
 *
 * ## 写入作用域
 *
 * `withRun(run, fn)` 在**同步**执行期间把 `items` / `loading` 指向"这条事件所属的会话"。
 * JS 单线程 + Vue 渲染是异步的，所以模板永远看不到这个临时指向（净变化为零）；
 * 换来的是 SSE 回调里二十多处 `items.value…` 不需要改成"按 sid 查表"。
 */
import { computed, reactive, shallowRef } from 'vue'

import type { SubagentEvent } from '../api/subagent'
import type { ChatItem } from '../components/chat/chat-types'
import type { TurnRef, TurnStopState } from './useTurnStop'

export interface SessionRunState {
  /** 该会话的消息列表：切换会话时**不再清空**，后台继续追加 */
  items: ChatItem[]
  /** 该会话是否正在生成（切走不影响它继续跑） */
  loading: boolean
  /** 代际号：该会话运行时被重建时 +1（用于丢弃旧回调） */
  gen: number
  /** 回合序号（H69）：每提交一轮 +1（用于判断 gap 快照属于哪一轮） */
  turnSeq: number
  /**
   * 该会话的**子 Agent 实时事件**（有界缓冲）。
   *
   * 此前是模块级单值，会话 A 的子 Agent 进度会串到会话 B 的观测面板与状态栏角标。
   * 放进 run state 后，"事件属于哪个会话"由 `withRun` 作用域自动决定。
   */
  subagentEvents: SubagentEvent[]
  /** 本会话的流式累积缓冲（assistant 原始文本，整体重算以正确配对 [thinking] 标签）。 */
  streamBuf: string
  streamTextId: string
  streamReasonId: string
  streamStatsId: string
  /**
   * 有票已答复、但唤醒被"会话正在跑"挡下了（见 ChatView 的 `wakeFromInbox`）。
   *
   * 一次答复一批待办时必然发生：第一条唤醒新一轮 ⇒ loading 变 true，后面几条被挡掉。
   * 那些票停在 `answered`，而续跑只消费与本次调用指纹匹配的票 —— 它们永远不会被取走。
   * 记在这里，本轮收尾后补一次唤醒。
   */
  pendingInboxWake?: boolean
  /**
   * 这一轮 run 的现场（H58）：`sessionId` + 提交时的 `client_msg_id`。
   *
   * 必须**捕获**而不是停止时现取：用户按停止前往往已经切走了会话，而取消要打到
   * **发出这一轮**的会话上，不是此刻正在看的那个。
   */
  turn?: TurnRef
  /** 停止请求的现场（H58）：`stopping` = 已下发、等确认；`failed` = 未确认、可重试 */
  stop?: TurnStopState
}

/**
 * `getActiveSessionId` 用 getter 而不是值：会话切换是视图层的输入，组合式函数
 * 在 setup 里只创建一次，必须在读取时取最新值。
 */
/**
 * 会话运行时的保留上限：长会话在侧栏来回切换时，`runtimes` 会按会话持续增长，
 * 每个条目都持有完整消息数组（items）。没有上限时内存只增不减 —— 用户切过
 * 几十个会话后，旧会话的 items 永远不会被回收。
 *
 * 淘汰只在"新会话首次进入"时发生，且绝不淘汰当前会话、运行中的会话、或仍有
 * 子 Agent 事件待消费的会话；被淘汰的会话再次打开时会从服务端重新加载。
 */
export const RUNTIME_CACHE_LIMIT = 8

export function useChatRunState(getActiveSessionId: () => string) {
  /**
   * ⚠️ 必须是**响应式** Map：`items` / `loading` 是从这里派生的 computed，
   * 而流式输出靠 `items.value.push(...)` 追加 —— 底层若不是响应式的，push 不会触发
   * 任何重渲染，表现为"流式期间界面不动、消息结束后一次性跳出全部内容"。
   */
  const runtimes = reactive(new Map<string, SessionRunState>())

  function runOf(sid: string): SessionRunState {
    let run = runtimes.get(sid)
    if (!run) {
      runtimes.set(sid, {
        items: [],
        loading: false,
        gen: 1,
        turnSeq: 0,
        subagentEvents: [],
        streamBuf: '',
        streamTextId: '',
        streamReasonId: '',
        streamStatsId: '',
      })
      // 必须取回 `runtimes.get()` 的**代理**，而不是刚构造的原始对象 ——
      // 原始对象不在响应式系统里，后续 push 不会触发更新。
      run = runtimes.get(sid)!
      evictIdleRuns(RUNTIME_CACHE_LIMIT, sid)
    }
    return run
  }

  function evictIdleRuns(limit: number, protect: string): void {
    if (runtimes.size <= limit) return
    const active = getActiveSessionId()
    for (const [sid, run] of runtimes) {
      if (runtimes.size <= limit) return
      if (sid === protect || sid === active) continue
      // 运行中或还有子 Agent 事件待消费：淘汰会让下一次事件写进一个空状态。
      if (run.loading || run.subagentEvents.length > 0) continue
      runtimes.delete(sid)
    }
  }

  /** 当前视图所属会话的运行时（空会话用占位键，保持既有"无会话"行为）。 */
  const currentRun = computed(() => runOf(getActiveSessionId() || '__none__'))

  /**
   * 主回合仍在跑的会话 id（`runtimes[sid].loading`）。
   *
   * 只是侧栏心跳的**信号之一**：主回合结束但后台子 Agent 还在跑的会话会漏，
   * 那个场景由 ChatView 基于 `subagentLiveEvents` 的集合覆盖，两者取并集。
   */
  const liveRunSessionIds = computed(() => {
    const set = new Set<string>()
    for (const [sid, run] of runtimes) if (run.loading) set.add(sid)
    return set
  })

  /**
   * 写入作用域。必须是 `shallowRef`：`items` / `loading` 是 computed，而 computed
   * 只追踪求值过程中读到的响应式数据 —— 普通变量读不到、也不会令其失效。
   */
  const writingRun = shallowRef<SessionRunState | null>(null)

  function withRun<T>(run: SessionRunState, fn: () => T): T {
    const prev = writingRun.value
    writingRun.value = run
    try {
      return fn()
    } finally {
      writingRun.value = prev
    }
  }

  /** 读/写都优先走作用域：SSE 回写时指向事件所属会话，其余时刻就是当前视图的会话。 */
  const loading = computed<boolean>({
    get: () => (writingRun.value ?? currentRun.value).loading,
    set: (v) => {
      ;(writingRun.value ?? currentRun.value).loading = v
    },
  })

  const items = computed<ChatItem[]>({
    get: () => (writingRun.value ?? currentRun.value).items,
    set: (v) => {
      ;(writingRun.value ?? currentRun.value).items = v
    },
  })

  /** SSE 回调整段跑在 `withRun(...)` 的同步作用域内，因此返回事件所属会话的那一份。 */
  function streamState(): SessionRunState {
    return writingRun.value ?? currentRun.value
  }

  return {
    runtimes,
    runOf,
    currentRun,
    liveRunSessionIds,
    writingRun,
    withRun,
    loading,
    items,
    streamState,
  }
}
