/**
 * 历史待办 ↔ 实时票据的**统一对账**（H61）。
 *
 * 钉四件事，各自对应一个可见的"状态失真"：
 *   1. **权威到期**：只认服务端 `expires_at`（前端自己的 300s 只是兜底展示）；
 *   2. **权威 free-text**：`request.allow_free_text === false` 就不能给输入框；
 *   3. **同一张票只出现一次**：历史行被实时票覆盖时标 `live`，收件箱不再给第二个答复入口；
 *   4. **角标取并集**：历史 ∪ 实时按票去重 —— 不是"各说各话"，也不是"取较大者"。
 */
import { describe, expect, it } from 'vitest'
import type { PendingInteraction } from '../../../api/interactions'
import {
  allowFreeTextOf,
  interactionExpired,
  pendingUnionCount,
  reconcilePending,
} from '../pendingReconcile'

function row(over: Partial<PendingInteraction> & { id: string }): PendingInteraction {
  return { kind: 'approval', status: 'pending', ...over } as PendingInteraction
}

describe('权威到期（expires_at）', () => {
  const now = Date.parse('2026-11-24T10:00:00Z')

  it('服务端说已过期 ⇒ 过期（哪怕前端刚拉到它）', () => {
    expect(interactionExpired('2026-11-24T09:59:00Z', now)).toBe(true)
  })

  it('服务端说过期还早 ⇒ 不过期', () => {
    expect(interactionExpired('2026-11-24T10:05:00Z', now)).toBe(false)
  })

  it('缺字段或解析不出来 ⇒ **不算**过期（宁可多给一次机会）', () => {
    expect(interactionExpired(undefined, now)).toBe(false)
    expect(interactionExpired('', now)).toBe(false)
    expect(interactionExpired('not-a-time', now)).toBe(false)
  })
})

describe('权威 free-text（allow_free_text）', () => {
  it('服务端明确 false ⇒ 不给自由输入', () => {
    expect(allowFreeTextOf(row({ id: 'i1', request: { allow_free_text: false } }))).toBe(false)
  })

  it('缺省 ⇒ 允许（与引擎 ask_user 默认一致）', () => {
    expect(allowFreeTextOf(row({ id: 'i1', request: { question: '选一个' } }))).toBe(true)
  })
})

describe('统一对账（reconcilePending）', () => {
  it('历史行被实时票覆盖 ⇒ 标 live（收件箱据此不再出第二张卡）', () => {
    const history = [row({ id: 'i1', tool_call_id: 'call_1' }), row({ id: 'i9' })]
    const rows = reconcilePending(history, [{ id: 'call_1', kind: 'approval', toolCallId: 'call_1' }])

    expect(rows).toHaveLength(2) // 历史是读视图，行都在
    expect(rows.find((r) => r.id === 'i1')!.live).toBe(true)
    expect(rows.find((r) => r.id === 'i9')!.live).toBe(false)
  })

  it('对齐键接受 tool_call_id（审批/提问在库里的两个键指同一张票）', () => {
    const history = [row({ id: 'row-7', tool_call_id: 'call_7' })]
    // 实时票的主键就是 tool_call_id
    const rows = reconcilePending(history, [{ id: 'call_7', kind: 'approval' }])
    expect(rows[0]!.live).toBe(true)
  })

  it('到期用权威字段逐行判定；缺字段的行保持不过期', () => {
    const now = Date.parse('2026-11-24T10:00:00Z')
    const rows = reconcilePending(
      [
        row({ id: 'old', expires_at: '2026-11-24T09:00:00Z' }),
        row({ id: 'new', expires_at: '2026-11-24T11:00:00Z' }),
        row({ id: 'none' }),
      ],
      [],
      now,
    )
    expect(rows.map((r) => r.expired)).toEqual([true, false, false])
  })
})

describe('角标读数（pendingUnionCount）', () => {
  it('历史与实时指向同一张票 ⇒ 只算一次', () => {
    const history = [row({ id: 'i1', tool_call_id: 'call_1' })]
    const live = [{ id: 'call_1', kind: 'approval' as const }]
    expect(pendingUnionCount(history, live)).toBe(1)
  })

  it('各自独有的票都算上（实时只有当前会话、历史可能还没落库完）', () => {
    const history = [row({ id: 'i1', tool_call_id: 'call_1' }), row({ id: 'i2' })]
    const live = [
      { id: 'call_1', kind: 'approval' as const },
      { id: 'call_9', kind: 'question' as const },
    ]
    expect(pendingUnionCount(history, live)).toBe(3) // i2 + call_1 + call_9
  })

  it('两边都空 ⇒ 0（角标整块不渲染）', () => {
    expect(pendingUnionCount([], [])).toBe(0)
  })
})
