/**
 * 「这个会话还在动」的三份派生视图（从 `ChatView.vue` 抽出，行为不变）。
 *
 * 状态栏角标、会话地图的「运行中」脉冲、侧栏会话行的心跳点，看起来是三处独立显示，
 * 其实是同一个问题的三个视角：**有没有未终态的子 Agent run**。此前它们各自在
 * ChatView 里遍历一遍实时事件 —— 规则一旦分叉，就会出现"角标说在跑、地图说没跑"。
 *
 * ## 为什么只读实时事件，不等面板/轮询
 *
 * 子 Agent 事件的通道是会话级常驻 SSE（见 `useSubagentStream`），它不依赖观测面板
 * 是否打开、也不等侧栏 3 秒轮询，所以"有子 Agent 在跑"永远看得出来。代价是它**只
 * 覆盖子 Agent**：主回合在跑但还没派发子 Agent 时会漏 —— 那份信号由 `liveRunSessionIds`
 * （`runtimes[sid].loading`）提供，两者在 `sidebarRunningIds` 里取并集。
 *
 * ## 口径
 *
 * 见到某 run 即视为运行中，收到 `subagent.done` 才移除。多个 run 并发时会话始终保持
 * 标记 —— 与「是否还有未终态的 run」同义。
 */
import { computed, type ComputedRef } from 'vue'
import type { SubagentEvent } from '../api/subagent'

/** 实时事件里本模块用到的字段（`session_id` 是服务端附带的会话归属） */
type ActivityEvent = Partial<SubagentEvent> & { session_id?: string }

export interface UseSessionActivityOptions {
  /** 当前会话的子 Agent 实时事件（`useSubagentStream().subagentLiveEvents`） */
  subagentLiveEvents: () => readonly (SubagentEvent | ActivityEvent)[]
  /** 主回合仍在跑的会话 id 集合（`runtimes[sid].loading`） */
  liveRunSessionIds: () => Set<string>
}

/**
 * 运行中的子 Agent 数（状态栏角标）。
 *
 * 按 `run_id` 去重：同一个 run 会连发几十条进度事件，数事件条数没有意义。
 */
export function activeSubagentRunCount(events: readonly (SubagentEvent | ActivityEvent)[]): number {
  const active = new Set<string>()
  for (const e of events) {
    const id = e?.run_id
    if (!id) continue
    if (e.type === 'subagent.done') active.delete(id)
    else active.add(id)
  }
  return active.size
}

/**
 * 还有未终态子 Agent run 的会话 id（会话地图/侧栏的「运行中」）。
 *
 * 缺 `session_id` 的事件直接跳过：它属于别的会话，或来自没有会话归属的旧帧，
 * 都不能拿来给某个会话打点。
 */
export function runningSessionIdList(events: readonly (SubagentEvent | ActivityEvent)[]): string[] {
  const open = new Set<string>()
  for (const raw of events) {
    const e = raw as ActivityEvent
    const sid = e?.session_id
    if (!sid || !e?.run_id) continue
    if (e.type === 'subagent.done') open.delete(sid)
    else open.add(sid)
  }
  return [...open]
}

export interface SessionActivity {
  /** 状态栏角标：运行中的子 Agent 数 */
  subagentActiveCount: ComputedRef<number>
  /** 会话地图：还有未终态 run 的会话 id */
  runningSessionIds: ComputedRef<string[]>
  /** 侧栏心跳：主回合在跑 ∪ 子 Agent 在跑 */
  sidebarRunningIds: ComputedRef<Set<string>>
}

export function useSessionActivity(options: UseSessionActivityOptions): SessionActivity {
  const subagentActiveCount = computed(() => activeSubagentRunCount(options.subagentLiveEvents()))

  const runningSessionIds = computed<string[]>(() =>
    runningSessionIdList(options.subagentLiveEvents()),
  )

  /**
   * 两个信号**必须取并集**：只跑主回合时实时事件是空的；主回合结束但后台子 Agent
   * 仍在跑时 `runtimes.loading` 已为 false。任一单独用都会漏掉另一半场景。
   */
  const sidebarRunningIds = computed(() => {
    const set = new Set<string>(runningSessionIds.value)
    for (const sid of options.liveRunSessionIds()) set.add(sid)
    return set
  })

  return { subagentActiveCount, runningSessionIds, sidebarRunningIds }
}
