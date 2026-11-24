/**
 * H58：「停止生成」必须到达后端，且**未确认不得当成已停止**。
 *
 * 旧实现（`ChatView.vue::stopGeneration`）只关本地 SSE、置 `loading=false` 并给最后
 * 一条助手消息打 `stopped` 标记 —— 服务端完全不知道用户喊停，run 继续跑工具、
 * 继续写文件、继续计费。这里把"停止"建模成一次需要确认的请求，并钉住四条不变式：
 *
 *   1. 取消打到**这一轮 run 所在**的会话（`turn.sessionId`），不是"此刻正在看的会话"；
 *   2. 服务端确认前不调 `onStopped`（不收 loading、不关流、不标 stopped）；
 *   3. `stopping` 期间重复点击不重复下发；`failed` 之后再点就是重试；
 *   4. 没有会话 / 没有在跑的轮次时不发请求。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { requestTurnStop, STOP_NOT_CONFIRMED } from '../useTurnStop'
import type { TurnRef, TurnStopState, TurnStopDeps } from '../useTurnStop'

function makeDeps(overrides: Partial<TurnStopDeps> = {}) {
  const turns = new Map<string, TurnRef>()
  const stops = new Map<string, TurnStopState>()
  const onStopped = vi.fn()
  const onFailed = vi.fn()
  const interrupt = vi.fn(async () => true)
  const deps: TurnStopDeps = {
    activeSessionId: () => 's1',
    turnOf: (sid) => turns.get(sid),
    stopOf: (sid) => stops.get(sid),
    setStop: (sid, stop) => {
      if (stop === undefined) stops.delete(sid)
      else stops.set(sid, stop)
    },
    clearTurn: (sid) => {
      turns.delete(sid)
    },
    interrupt,
    onStopped,
    onFailed,
    ...overrides,
  }
  return { deps, turns, stops, onStopped, onFailed, interrupt }
}

describe('requestTurnStop (H58)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('没有活动会话时不发请求', async () => {
    const { deps, interrupt } = makeDeps({ activeSessionId: () => '' })
    expect(await requestTurnStop(deps)).toBe(false)
    expect(interrupt).not.toHaveBeenCalled()
  })

  it('确认后收尾一次，并清掉本轮现场', async () => {
    const { deps, turns, stops, onStopped, interrupt } = makeDeps()
    turns.set('s1', { sessionId: 's1', clientMsgId: 'm1' })

    expect(await requestTurnStop(deps)).toBe(true)

    expect(interrupt).toHaveBeenCalledWith('s1')
    expect(onStopped).toHaveBeenCalledTimes(1)
    expect(onStopped).toHaveBeenCalledWith('s1')
    expect(stops.has('s1')).toBe(false)
    expect(turns.has('s1')).toBe(false)
  })

  it('取消打到这一轮所在的会话，而不是此刻正在看的会话', async () => {
    // 用户在 s1 发起，随后切到 s2，再按停止
    const { deps, turns, interrupt } = makeDeps({ activeSessionId: () => 's2' })
    turns.set('s2', { sessionId: 's1', clientMsgId: 'm1' })

    await requestTurnStop(deps)

    expect(interrupt).toHaveBeenCalledWith('s1')
  })

  it('服务端未确认时不当成已停止，并留下可分诊的失败现场', async () => {
    const { deps, turns, stops, onStopped, onFailed } = makeDeps({
      interrupt: vi.fn(async () => false),
    })
    turns.set('s1', { sessionId: 's1', clientMsgId: 'm1' })

    expect(await requestTurnStop(deps)).toBe(false)

    expect(onStopped).not.toHaveBeenCalled()
    expect(onFailed).toHaveBeenCalledWith('failed', 's1')
    expect(stops.get('s1')).toMatchObject({
      phase: 'failed',
      error: STOP_NOT_CONFIRMED,
      attempts: 1,
    })
  })

  it('interrupt 抛错也算未确认', async () => {
    const { deps, turns, onStopped, onFailed } = makeDeps({
      interrupt: vi.fn(async () => {
        throw new Error('network')
      }),
    })
    turns.set('s1', { sessionId: 's1', clientMsgId: 'm1' })

    expect(await requestTurnStop(deps)).toBe(false)
    expect(onStopped).not.toHaveBeenCalled()
    expect(onFailed).toHaveBeenCalledTimes(1)
  })

  it('等待确认期间重复点击不重复下发', async () => {
    let release: (v: boolean) => void = () => {}
    const interrupt = vi.fn(
      () => new Promise<boolean>((resolve) => { release = resolve }),
    )
    const { deps, turns, onStopped } = makeDeps({ interrupt })
    turns.set('s1', { sessionId: 's1', clientMsgId: 'm1' })

    const first = requestTurnStop(deps)
    expect(await requestTurnStop(deps)).toBe(false) // 第二次直接拒绝
    release(true)
    expect(await first).toBe(true)

    expect(interrupt).toHaveBeenCalledTimes(1)
    expect(onStopped).toHaveBeenCalledTimes(1)
  })

  it('failed 之后再次点击是重试，并累加 attempts', async () => {
    const interrupt = vi
      .fn<() => Promise<boolean>>()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const { deps, turns, stops, onStopped } = makeDeps({ interrupt })
    turns.set('s1', { sessionId: 's1', clientMsgId: 'm1' })

    expect(await requestTurnStop(deps)).toBe(false)
    expect(stops.get('s1')?.attempts).toBe(1)

    expect(await requestTurnStop(deps)).toBe(true)
    expect(interrupt).toHaveBeenCalledTimes(2)
    expect(onStopped).toHaveBeenCalledTimes(1)
  })

  it('没有本轮现场时退回当前会话（别的客户端发起的 run）', async () => {
    const { deps, interrupt } = makeDeps()
    await requestTurnStop(deps)
    expect(interrupt).toHaveBeenCalledWith('s1')
  })
})
