/**
 * 会话切换的「历史回放」侧（从 `ChatView.vue` 抽出，行为逐字不变）。
 *
 * 一个会话的 transcript 有三个来源：SSE 实时帧、首屏拉回的历史页、翻页/补拉回来的旧页。
 * 本模块管后两者 —— 也就是**事实源是消息列表，不是事件流**的那一半：
 *
 * 1. **分页状态与翻页**（`loadEarlier`）—— 触顶加载更早的消息。两个游标（消息 / 工具调用）
 *    必须各自推进：两张表 `created_at` 分布不同，共用一个游标会漏取夹在两条消息之间的
 *    工具调用。竞态守卫（`switchSeq`）与"可重试 vs 真的到底"的区分也在这里 ——
 *    把 5xx/超时当成到底会让一次网络抖动永久关闭分页。
 * 2. **整段重拉**（`resyncSessionHistory` / `scheduleGapResync`）—— 服务端说"断线期间的
 *    推送补不齐"时，界面不能停在断线前那一刻；但本轮还在流式输出时立刻覆盖会把这段累积
 *    冲掉，所以先记入 `gapPendingResync`，等 `done` 收尾后由 `flushGapResync` 补。
 * 3. **历史 → 条目投影**（`projectHistory`）—— 纯函数：正在跑的会话**不覆盖**，
 *    否则"切走再切回"的瞬间会把这段时间里增长的内容冲掉。
 *
 * ## 为什么 `switchSeq` 在这里
 *
 * 它是所有回放路径的竞态守卫。会话切换（ChatView 的 `switchSession`）负责**递增**它，
 * 翻页与重拉只负责**比对**。把递增也搬进来的话，ChatView 就得在切会话的何处调用、以及
 * 与 `initialLoading` 复位的先后顺序上重新推理一遍 —— 那已经不属于"回放"。
 * 所以这里只提供 `beginSwitch()` / `isStale(seq)` 两个动词。
 */
import { ref, watch, nextTick, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import { notify } from '../utils/notify'
import { errorStatus } from '../utils/apiError'
import { mergeHistory } from '../components/chat/chat-history'
import type { ChatItem, HistoryMessage, HistoryToolCall } from '../components/chat/chat-types'

/** 首屏只加载最新的一页：历史动辄上千条，全量拉回会让首屏等上好几秒 */
export const HISTORY_PAGE_SIZE = 50

/**
 * 会话运行时里本模块用到的两个字段。
 *
 * 只取用到的部分 —— 接受任何"有 `loading` / `items`"的 run 状态即可，
 * 不把 `SessionRunState` 的类型绑进来（它是 ChatView 的私有类型）。
 */
export interface ReplayRunStateLike {
  loading: boolean
  items: ChatItem[]
  /** 运行代际：该会话的运行时被**重建**（重开同一 id、重新建流）时递增 */
  gen?: number
  /**
   * 回合序号：每提交一轮 +1（H69）。
   *
   * 只比 `loading` 不够 —— 拉取期间新一轮可能**已经跑完**（`loading` 回到 false），
   * 此时旧快照会把这一轮刚产出的内容整段覆盖。代际也不够：它是"运行时重建"，不是"新回合"。
   */
  turnSeq?: number
}

/** 重拉开始时的现场（提交 CAS 前要比对的那份） */
export interface ResyncSnapshot {
  gen: number
  turnSeq: number
}

/** 取现场：拉取**之前**调用 */
export function resyncSnapshot(run: ReplayRunStateLike): ResyncSnapshot {
  return { gen: run.gen ?? 0, turnSeq: run.turnSeq ?? 0 }
}

/**
 * 提交 CAS（**纯函数**，单测直接钉）：这份快照还能不能落盘？
 *
 * 三个否：会话仍在跑（`loading`，与旧行为一致）、运行时代际变了、回合序号变了
 * （后两条覆盖"新一轮已结束"这个 loading 看不出来的情形 —— H69 的核心）。
 */
export function canCommitResync(run: ReplayRunStateLike, snapshot: ResyncSnapshot): boolean {
  if (run.loading) return false
  if ((run.gen ?? 0) !== snapshot.gen) return false
  return (run.turnSeq ?? 0) === snapshot.turnSeq
}

/** 一页历史的元信息（消息 + 工具调用两个游标互不相同） */
export interface HistoryPageMeta {
  cursor?: string
  has_more?: boolean
  tool_cursor?: string
  tool_calls_partial?: boolean
}

/** 可重试的错误：网络抖动、超时、服务端 5xx。它们不代表"历史到此为止"。 */
export function isRetriableLoadError(error: unknown): boolean {
  const status = errorStatus(error)
  if (status === undefined) return true // 网络错误 / 超时：没有状态码
  return status >= 500 || status === 408 || status === 429
}

/**
 * 历史页 → 条目（**纯函数**）。
 *
 * 该会话正在生成（或流里已累积过内容）时返回 `null`：调用方据此"不覆盖"。
 * 判据必须与 SSE 回写同源 —— 这里只看 run 状态，不看模板状态。
 */
export function projectHistory(
  run: ReplayRunStateLike,
  messages: readonly HistoryMessage[] | null | undefined,
  toolCalls: readonly HistoryToolCall[] | null | undefined,
): ChatItem[] | null {
  if (!messages) return null
  if (run.loading || run.items.length !== 0) return null
  return mergeHistory(messages, toolCalls || [])
}

export interface UseSessionSwitchOptions {
  /** 当前视图所属会话 id（空串 = 无会话） */
  sessionId: () => string
  /** 当前会话的条目（读） */
  items: () => ChatItem[]
  /** 当前会话的条目（写：整体替换） */
  setItems: (items: ChatItem[]) => void
  /** 取某会话的运行时切片（翻页守卫与整段重拉都要看它） */
  runOf: (sid: string) => ReplayRunStateLike
  /** 是否就是当前视图所属会话（重拉后决定要不要滚到底） */
  isActiveSession: (sid: string) => boolean
  /** 滚到底（单写者：由 ChatView 交给 MessageList） */
  scrollToBottom: () => void
}

export interface SessionSwitch {
  /** 模板/渲染用：首屏加载中 */
  initialLoading: Ref<boolean>
  /** 模板/渲染用：还有更早的消息 */
  hasMore: Ref<boolean>
  /** 模板/渲染用：正在翻页 */
  loadingEarlier: Ref<boolean>
  /** 模板/渲染用：上次翻页失败（可重试） */
  earlierLoadFailed: Ref<boolean>
  /** 切会话：递增竞态守卫并清空分页状态，返回本次切会话的序号 */
  beginSwitch: () => number
  /** 该序号是否已被后来的切会话取代（被取代的回调必须整体作废） */
  isStale: (seq: number) => boolean
  /** 收尾：复位首屏加载标记（只在仍是最新一次切会话时） */
  endSwitch: (seq: number) => void
  /** 把首屏/翻页拉回的一页元信息落到分页状态上 */
  applyPageMeta: (data: HistoryPageMeta) => void
  /** 触顶加载更早的消息 */
  loadEarlier: () => Promise<void>
  /** 重拉某会话的历史并替换该会话的条目（事实源是消息列表） */
  resyncSessionHistory: (sid: string) => Promise<void>
  /** 收到 `stream_gap`：本轮在跑就先记账，否则立刻重拉 */
  scheduleGapResync: (sid: string) => void
  /** 本轮收尾后补发被挡下的那次重拉（`done` / 出错 / 停止都要走） */
  flushGapResync: (sid: string) => void
}

export function useSessionSwitch(options: UseSessionSwitchOptions): SessionSwitch {
  const { t } = useI18n()

  const hasMore = ref(false)
  const earliestCursor = ref('')
  // 工具调用**自己的**游标：与消息游标分开推进。两张表 created_at 分布不同，
  // 共用一个游标会漏取夹在两条消息之间的工具调用。
  const earliestToolCursor = ref('')
  const toolCallsPartial = ref(false)
  const loadingEarlier = ref(false)
  const earlierLoadFailed = ref(false)
  const initialLoading = ref(false)
  const switchSeq = ref(0)

  // 工具查询失败只提示一次：状态在每次翻页时都会重算，直接在赋值处 notify 会
  // 变成"每滚一屏弹一次"。这里只对 false→true 的翻转做提示。
  watch(toolCallsPartial, (now, before) => {
    if (now && !before) notify.warning(t('chat.tool_calls_partial_warning'))
  })

  /** 收到 `stream_gap` 后待补拉的会话（本轮还在流式输出时不能立刻覆盖，见下）。 */
  const gapPendingResync = new Set<string>()

  function applyPageMeta(data: HistoryPageMeta) {
    earliestCursor.value = data.cursor || ''
    hasMore.value = !!data.has_more
    earliestToolCursor.value = data.tool_cursor || ''
    toolCallsPartial.value = !!data.tool_calls_partial
  }

  function beginSwitch(): number {
    const mySeq = ++switchSeq.value
    hasMore.value = false; earliestCursor.value = ''; earliestToolCursor.value = ''
    loadingEarlier.value = false
    earlierLoadFailed.value = false
    toolCallsPartial.value = false
    initialLoading.value = true
    return mySeq
  }

  function isStale(seq: number): boolean {
    return seq !== switchSeq.value
  }

  function endSwitch(seq: number) {
    if (seq === switchSeq.value) initialLoading.value = false
  }

  async function loadEarlier() {
    if (loadingEarlier.value || !hasMore.value || !options.sessionId()) return
    // cursor 为空却 hasMore 为真 = 状态自相矛盾：再请求也拿不到"更早"的锚点。
    // 早退会让人以为"到底了"而实际没有，所以显式结束并留痕，不留死锁。
    if (!earliestCursor.value) {
      console.warn('[chat] hasMore=true but cursor is empty; ending pagination', {
        sessionId: options.sessionId(),
      })
      hasMore.value = false
      return
    }

    // 竞态守卫：慢响应期间用户可能已经切走。没有这一行，会话 A 的历史会被拼进
    // 会话 B 的列表里 —— 刷新才消失，无从解释。
    const mySeq = switchSeq.value
    const mySession = options.sessionId()
    loadingEarlier.value = true
    earlierLoadFailed.value = false
    try {
      // 两个游标各自带上：消息与工具调用是两张表，游标必须独立推进。
      // 只发一个会让另一张表要么重复取、要么漏取（夹在两条消息之间的工具调用最容易漏）。
      let url = `/v1/conversations/${encodeURIComponent(mySession)}?limit=${HISTORY_PAGE_SIZE}&before=${encodeURIComponent(earliestCursor.value)}`
      if (earliestToolCursor.value) {
        url += `&tool_before=${encodeURIComponent(earliestToolCursor.value)}`
      }
      const res = await api.get(url)
      if (mySeq !== switchSeq.value || mySession !== options.sessionId()) return
      const data = res.data?.data || res.data
      // 工具查询失败时服务端会带 tool_calls_partial：列表不完整，但消息仍可用。
      // 标记它，让界面能提示"部分工具记录未加载"，而不是让人以为工具根本没执行过。
      toolCallsPartial.value = !!data?.tool_calls_partial
      if (data?.messages?.length) {
        const earlier = mergeHistory(data.messages, data.tool_calls || [])
        // 合并前按 id 去重：游标是 created_at|id 复合值，服务端与前端对时区/精度的
        // 处理一旦有差异就会返回重叠页，同一条目会被渲染两次（像是执行了两次工具）。
        const seen = new Set(options.items().map(i => i.id))
        const fresh = earlier.filter(i => !seen.has(i.id))
        // 头部插入后由 MessageList 按锚点还原视口（单写者），此处不碰 DOM
        if (fresh.length) options.setItems([...fresh, ...options.items()])
        earliestCursor.value = data.cursor || ''
        hasMore.value = !!data.has_more
        earliestToolCursor.value = data.tool_cursor || ''
        if (hasMore.value && !earliestCursor.value) {
          console.warn('[chat] server says has_more but sent no cursor; stopping', { sessionId: mySession })
          hasMore.value = false
        }
      } else {
        hasMore.value = false
      }
    } catch (error) {
      if (mySeq !== switchSeq.value || mySession !== options.sessionId()) return
      // 区分"真的没有更多"与"这次没取到"：把 5xx/超时一律当成到底，会让一次网络抖动
      // 永久关闭分页（hasMore=false 之后再也不会重试），用户看到的历史凭空少了一截。
      if (isRetriableLoadError(error)) {
        earlierLoadFailed.value = true
        hasMore.value = true // 保留可重试资格
        console.warn('[chat] load earlier failed (retriable)', { sessionId: mySession, error })
      } else {
        hasMore.value = false
      }
    } finally {
      if (mySeq === switchSeq.value) loadingEarlier.value = false
    }
  }

  /** 重拉某会话的历史并替换该会话的条目。事实源是消息列表，不是事件流。 */
  async function resyncSessionHistory(sid: string) {
    // 现场在**发请求之前**取：拉取期间该会话可能又开跑一轮（甚至已经跑完）。
    const snapshot = resyncSnapshot(options.runOf(sid))
    try {
      const res = await api.get(`/v1/conversations/${sid}?limit=${HISTORY_PAGE_SIZE}`)
      const data = res.data?.data || res.data
      if (!data?.messages) return
      const run = options.runOf(sid)
      // CAS：拉取期间该会话可能又开跑了新一轮 —— 那时历史是旧的，覆盖会把新内容冲掉。
      // **只看 `run.loading` 是 H69 修掉的那个缺口**：新一轮若已结束，loading 已经回到 false，
      // 旧快照照样会把这一轮的产出整段盖掉（用户看到"刚跑完的结果忽然不见了"）。
      if (!canCommitResync(run, snapshot)) return
      run.items = mergeHistory(data.messages, data.tool_calls || [])
      if (options.isActiveSession(sid)) {
        await nextTick()
        options.scrollToBottom()
      }
    } catch {
      // 拉不到就保持现状（不用半截数据覆盖）：缺口会随下一次正常加载补齐
    }
  }

  /**
   * 服务端说"断线期间的推送补不齐"（会话流被 MaxLen 裁到最近 200 条、或流已过 TTL 整条消失）。
   *
   * 这不是错误，不该弹窗 —— 此刻事实源是消息列表，重拉即恢复。**必须重拉**：不拉的话界面就
   * 停在断线前那一刻，用户回来时以为任务还在跑，或者以为它根本没跑过。
   */
  function scheduleGapResync(sid: string) {
    if (!sid) return
    if (options.runOf(sid).loading) {
      // 本轮还在流式输出：立刻用历史覆盖会把这段累积冲掉（与 switchSession 同款判断）。
      // 记下来，等本轮 `done` 收尾后再补 —— 那时实时与历史都已定稿。
      gapPendingResync.add(sid)
      return
    }
    void resyncSessionHistory(sid)
  }

  /** 本轮收尾：账上若还挂着这个会话，就补拉一次（`done` / 出错 / 停止都要走） */
  function flushGapResync(sid: string) {
    if (gapPendingResync.delete(sid)) void resyncSessionHistory(sid)
  }

  return {
    initialLoading,
    hasMore,
    loadingEarlier,
    earlierLoadFailed,
    beginSwitch,
    isStale,
    endSwitch,
    applyPageMeta,
    loadEarlier,
    resyncSessionHistory,
    scheduleGapResync,
    flushGapResync,
  }
}
