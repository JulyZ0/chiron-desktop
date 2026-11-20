/**
 * 「这个会话还在动」的派生规则（`useSessionActivity`）。
 *
 * 三处显示（状态栏角标 / 会话地图运行中 / 侧栏心跳点）此前各遍历一遍实时事件，
 * 规则一旦分叉就会自相矛盾。这里钉住口径：
 * - 见到某 run 即运行中，`subagent.done` 才移除；同一 run 的几十条进度只算一个；
 * - 会话标记按 `session_id`，缺归属的帧不打点；
 * - 侧栏心跳 = 子 Agent 在跑 ∪ 主回合在跑（各有盲区，必须取并集）。
 */
import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import type { SubagentEvent } from '../../api/subagent'
import {
  activeSubagentRunCount,
  runningSessionIdList,
  useSessionActivity,
} from '../useSessionActivity'

function evt(type: string, run_id = 'r1', session_id?: string): SubagentEvent {
  return { type, run_id, ...(session_id ? { session_id } : {}) } as SubagentEvent
}

describe('activeSubagentRunCount', () => {
  it('同一 run 的多条进度只算一个', () => {
    const events = [evt('subagent.start'), evt('subagent.progress'), evt('subagent.tool_result')]
    expect(activeSubagentRunCount(events)).toBe(1)
  })

  it('done 只移除对应的那一个 run', () => {
    const events = [evt('subagent.start', 'a'), evt('subagent.start', 'b'), evt('subagent.done', 'a')]
    expect(activeSubagentRunCount(events)).toBe(1)
    expect(activeSubagentRunCount([...events, evt('subagent.done', 'b')])).toBe(0)
  })

  it('缺 run_id 的帧不计入', () => {
    expect(activeSubagentRunCount([{ type: 'subagent.progress' } as SubagentEvent])).toBe(0)
  })
})

describe('runningSessionIdList', () => {
  it('按会话聚合：同一会话多个 run 仍只出现一次', () => {
    const events = [
      evt('subagent.start', 'a', 's1'),
      evt('subagent.start', 'b', 's1'),
      evt('subagent.start', 'c', 's2'),
    ]
    expect(runningSessionIdList(events).sort()).toEqual(['s1', 's2'])
  })

  it('done 收掉会话标记，且不影响其他会话', () => {
    const events = [
      evt('subagent.start', 'a', 's1'),
      evt('subagent.start', 'c', 's2'),
      evt('subagent.done', 'a', 's1'),
    ]
    expect(runningSessionIdList(events)).toEqual(['s2'])
  })

  it('缺 session_id 的帧不打点（别人的 run 不能算到本会话头上）', () => {
    expect(runningSessionIdList([evt('subagent.start', 'a')])).toEqual([])
  })
})

describe('useSessionActivity', () => {
  it('侧栏心跳取并集：子 Agent 在跑或主回合在跑都算', () => {
    const events = [evt('subagent.start', 'a', 's1')]
    const busy = new Set(['s2'])
    const { sidebarRunningIds, subagentActiveCount } = useSessionActivity({
      subagentLiveEvents: () => events,
      liveRunSessionIds: () => busy,
    })

    expect(sidebarRunningIds.value).toEqual(new Set(['s1', 's2']))
    expect(subagentActiveCount.value).toBe(1)
  })

  it('子 Agent 结束后，只剩主回合的会话仍在心跳里', () => {
    // 用 ref 承载事件：computed 只依赖响应式数据，普通数组变量改了它是看不见的。
    const events = ref<SubagentEvent[]>([evt('subagent.start', 'a', 's1')])
    const busy = new Set<string>()
    const activity = useSessionActivity({
      subagentLiveEvents: () => events.value,
      liveRunSessionIds: () => busy,
    })
    expect(activity.sidebarRunningIds.value.has('s1')).toBe(true)

    events.value = [evt('subagent.done', 'a', 's1')]
    expect(activity.sidebarRunningIds.value.has('s1')).toBe(false)
    expect(activity.subagentActiveCount.value).toBe(0)
  })
})
