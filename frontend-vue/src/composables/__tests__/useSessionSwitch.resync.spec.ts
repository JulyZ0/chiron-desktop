/**
 * gap 重拉的**代次 CAS**（H69）。
 *
 * 起因：`stream_gap`（服务端缓冲被裁 / 流过 TTL）后要整段重拉历史，而重拉是异步的 ——
 * 拉取期间该会话可能又开跑了**新一轮，并且已经跑完**。此时旧的判据 `if (run.loading) return`
 * 已经失效（loading 回到 false），于是这份**旧快照**照样落盘，把新一轮刚产出的内容整段盖掉：
 * 用户看到"刚跑完的结果忽然不见了"，刷新后才又出现。
 *
 * 修法：拉取**之前**取现场（`gen` + `turnSeq`），提交前做 CAS（`canCommitResync`）。
 * 这里既钉纯函数，也钉**真的接线**（`resyncSessionHistory` 确实调用了它）——
 * 只测纯函数证明不了"调用点在正确的时机、用对了那份快照"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

vi.mock('../../api', () => ({
  api: { get: vi.fn() },
}))

import { api } from '../../api'
import {
  canCommitResync,
  resyncSnapshot,
  useSessionSwitch,
  type ReplayRunStateLike,
} from '../useSessionSwitch'
import type { ChatItem } from '../../components/chat/chat-types'

const getMock = vi.mocked(api.get)

/** 后端给的一页历史（只为让 `mergeHistory` 产出条目） */
const HISTORY_PAGE = {
  data: {
    messages: [
      { id: 'm1', role: 'user', content: '旧问题', created_at: '2026-11-24T10:00:00Z' },
      { id: 'm2', role: 'assistant', content: '旧回答', created_at: '2026-11-24T10:00:01Z' },
    ],
    tool_calls: [],
  },
}

function makeRun(): ReplayRunStateLike {
  return { loading: false, items: [], gen: 1, turnSeq: 3 }
}

function setup() {
  const run = makeRun()
  const scrollToBottom = vi.fn()
  const sessionItems = () => run.items
  const stream = useSessionSwitch({
    sessionId: () => 's1',
    items: sessionItems,
    setItems: (items: ChatItem[]) => { run.items = items },
    runOf: () => run,
    isActiveSession: (sid) => sid === 's1',
    scrollToBottom,
  })
  return { run, stream, scrollToBottom }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('纯函数：现场与 CAS', () => {
  it('快照取到代次与回合序号（缺省按 0）', () => {
    expect(resyncSnapshot({ loading: false, items: [] })).toEqual({ gen: 0, turnSeq: 0 })
    expect(resyncSnapshot({ loading: false, items: [], gen: 2, turnSeq: 7 }))
      .toEqual({ gen: 2, turnSeq: 7 })
  })

  it('什么都没变 ⇒ 允许落盘', () => {
    const run = makeRun()
    expect(canCommitResync(run, resyncSnapshot(run))).toBe(true)
  })

  it('会话仍在跑 ⇒ 拒绝（旧判据，保留）', () => {
    const run = makeRun()
    const snap = resyncSnapshot(run)
    run.loading = true
    expect(canCommitResync(run, snap)).toBe(false)
  })

  it('新一轮已开跑并结束（turnSeq 变了、loading 已回 false）⇒ 拒绝', () => {
    const run = makeRun()
    const snap = resyncSnapshot(run)
    run.turnSeq = 4
    run.loading = false // ← 旧判据在这里完全看不出来
    expect(canCommitResync(run, snap)).toBe(false)
  })

  it('运行时被重建（gen 变了）⇒ 拒绝', () => {
    const run = makeRun()
    const snap = resyncSnapshot(run)
    run.gen = 2
    expect(canCommitResync(run, snap)).toBe(false)
  })
})

describe('接线：resyncSessionHistory 在提交前做 CAS', () => {
  it('拉取期间跑完新一轮 ⇒ 旧快照不落盘（不覆盖新内容）', async () => {
    const { run, stream } = setup()
    // 新一轮已经产出的内容（拉取期间写入）
    const fresh: ChatItem = { kind: 'text', id: 'live-1', role: 'assistant', content: '新一轮的答复' }

    let release: (value: unknown) => void = () => {}
    getMock.mockImplementation(() => new Promise((resolve) => { release = resolve }) as never)

    const pending = stream.resyncSessionHistory('s1')

    // 拉取还没回来：新一轮开跑并**已经结束**
    run.turnSeq += 1
    run.loading = false
    run.items = [fresh]

    release(HISTORY_PAGE)
    await pending

    // 旧快照没有覆盖新内容（旧实现这里会只剩 m1/m2 —— 新一轮的正文凭空消失）
    expect(run.items).toEqual([fresh])
  })

  it('什么都没变 ⇒ 正常用历史替换，并滚到底', async () => {
    const { run, stream, scrollToBottom } = setup()
    getMock.mockResolvedValue(HISTORY_PAGE as never)

    await stream.resyncSessionHistory('s1')

    expect(run.items.length).toBe(2)
    expect(run.items.map((i) => (i.kind === 'text' ? i.content : '')).join('|')).toContain('旧回答')
    expect(scrollToBottom).toHaveBeenCalled()
  })

  it('拉取期间会话还在跑 ⇒ 不落盘（旧判据同样遵守）', async () => {
    const { run, stream } = setup()
    let release: (value: unknown) => void = () => {}
    getMock.mockImplementation(() => new Promise((resolve) => { release = resolve }) as never)

    const pending = stream.resyncSessionHistory('s1')
    run.loading = true
    release(HISTORY_PAGE)
    await pending

    expect(run.items).toEqual([])
  })

  it('scheduleGapResync：本轮在跑就记账，收尾后才重拉（那条路径也走 CAS）', async () => {
    const { run, stream } = setup()
    getMock.mockResolvedValue(HISTORY_PAGE as never)

    run.loading = true
    stream.scheduleGapResync('s1')
    expect(getMock).not.toHaveBeenCalled() // 记账，不立刻覆盖

    run.loading = false
    run.turnSeq += 1 // 这一轮结束（回合推进）
    stream.flushGapResync('s1')
    await Promise.resolve()
    await Promise.resolve()

    // 重拉发生在这之后：现场取自**收尾时**，因此能正常落盘
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(run.items.length).toBe(2)
  })
})
