import { describe, expect, it } from 'vitest'
import { RUNTIME_CACHE_LIMIT, useChatRunState } from '../useChatRunState'

describe('useChatRunState 的会话运行态淘汰', () => {
  it('空闲会话超过上限后按插入顺序淘汰', () => {
    let active = ''
    const { runOf, runtimes } = useChatRunState(() => active)
    for (let i = 0; i < RUNTIME_CACHE_LIMIT + 6; i++) {
      runOf(`s${i}`)
    }
    expect(runtimes.size).toBe(RUNTIME_CACHE_LIMIT)
    // 最早的会被淘汰，刚创建的保留
    expect(runtimes.has('s0')).toBe(false)
    expect(runtimes.has(`s${RUNTIME_CACHE_LIMIT + 5}`)).toBe(true)
  })

  it('当前会话与运行中的会话不被淘汰', () => {
    let active = 'busy'
    const { runOf, runtimes } = useChatRunState(() => active)
    const busy = runOf('busy')
    busy.loading = true
    for (let i = 0; i < RUNTIME_CACHE_LIMIT + 4; i++) {
      runOf(`idle${i}`)
    }
    expect(runtimes.has('busy')).toBe(true)
    expect(runtimes.size).toBeLessThanOrEqual(RUNTIME_CACHE_LIMIT + 1)
  })
})
