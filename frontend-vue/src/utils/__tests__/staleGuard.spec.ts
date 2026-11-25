/** 代次守卫（PRD 9.2 / H60）。
 *
 * 缺陷形态：组件在 `await` 之后直接 `x.value = result`。只要 await 期间用户切了
 * 会话 / 换了文件 / 重发了请求，旧响应就会盖掉新视图；`finally` 里的
 * `loading = false` 还会把新请求的加载态一起清掉（表现为"点了一下没反应"）。
 *
 * 另有**组件级**回归：`InteractionInbox` 切会话后会显示上一个会话的待办，
 * `ToolResultBlock` 会把上一个工具调用的后续片段拼到当前块上。
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createGenerationGuard, createKeyedGuard } from '../staleGuard'

describe('createGenerationGuard', () => {
  it('取票 → 未 bump 时有效', () => {
    const g = createGenerationGuard()
    const t = g.current()
    expect(g.isCurrent(t)).toBe(true)
  })

  it('bump 之后旧票失效', () => {
    const g = createGenerationGuard()
    const t = g.current()
    g.bump()
    expect(g.isCurrent(t)).toBe(false)
    expect(g.isCurrent(g.current())).toBe(true)
  })

  it('bump 单调递增（多次切换后老票依然无效）', () => {
    const g = createGenerationGuard()
    const t0 = g.current()
    g.bump()
    g.bump()
    g.bump()
    expect(g.isCurrent(t0)).toBe(false)
  })
})

describe('createKeyedGuard', () => {
  it('键不变、代次不变 ⇒ 票有效', () => {
    let key = 's1'
    const g = createKeyedGuard(() => key)
    const t = g.ticket(key)
    expect(g.isValid(t)).toBe(true)
  })

  it('键变了 ⇒ 旧票立刻失效（即使忘了 bump）', () => {
    let key = 's1'
    const g = createKeyedGuard(() => key)
    const t = g.ticket(key)
    key = 's2'
    expect(g.isValid(t)).toBe(false)
  })

  it('键相同但 bump 过 ⇒ 失效（同一会话内的新一轮刷新）', () => {
    const g = createKeyedGuard(() => 's1')
    const t = g.ticket('s1')
    g.bump()
    expect(g.isValid(t)).toBe(false)
  })

  it('空票判为无效（不能让"没有票"当成通过）', () => {
    const g = createKeyedGuard(() => 's1')
    expect(g.isValid(null)).toBe(false)
    expect(g.isValid(undefined)).toBe(false)
  })
})

// ── 组件级回归：这两个部件必须真的接了守卫 ────────────────────────────────
describe('组件接入（源码守卫）', () => {
  const read = (rel: string) => readFileSync(resolve(__dirname, '..', '..', rel), 'utf8')

  it('InteractionInbox：切换会话后不得写入上一个会话的待办', () => {
    const src = read('components/chat/InteractionInbox.vue')
    expect(src).toContain('createKeyedGuard')
    expect(src).toMatch(/if \(!listGuard\.isValid\(ticket\)\) return/)
    // finally 里清 loading 也要验票，否则会清掉新请求的加载态
    expect(src).toMatch(/finally \{\s*\n\s*\/\/[^\n]*\n\s*if \(listGuard\.isValid\(ticket\)\) loadingList\.value = false/)
  })

  it('ToolResultBlock：旧片段不得拼到当前块上', () => {
    const src = read('components/chat/ToolResultBlock.vue')
    expect(src).toContain('createKeyedGuard')
    expect(src).toMatch(/chunkGuard\.ticket/)
    expect(src).toMatch(/if \(!chunkGuard\.isValid\(ticket\)\) return/)
  })
})
