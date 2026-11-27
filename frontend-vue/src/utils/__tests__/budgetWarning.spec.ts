import { describe, expect, it } from 'vitest'
import { evaluateBudget } from '../budgetWarning'

describe('evaluateBudget', () => {
  const base = { user_used_cents: 0, user_limit_cents: 0, tenant_used_cents: 0, tenant_limit_cents: 0 }

  it('未设上限（0）不提醒', () => {
    expect(evaluateBudget(base)).toEqual([])
  })

  it('79% 不提醒，80% 提醒一次', () => {
    expect(evaluateBudget({ ...base, user_used_cents: 79, user_limit_cents: 100 })).toEqual([])
    const w = evaluateBudget({ ...base, user_used_cents: 80, user_limit_cents: 100 })
    expect(w).toHaveLength(1)
    expect(w[0]).toMatchObject({ scope: 'user', level: 80, pct: 80 })
  })

  it('超限同时给 80 与 100 两档（各自只提醒一次由调用方负责）', () => {
    const w = evaluateBudget({ ...base, user_used_cents: 120, user_limit_cents: 100 })
    expect(w.map(x => x.level)).toEqual([80, 100])
    expect(w[1].pct).toBe(120)
  })

  it('个人与团队分别判定', () => {
    const w = evaluateBudget({
      user_used_cents: 10, user_limit_cents: 100,
      tenant_used_cents: 90, tenant_limit_cents: 100,
    })
    expect(w).toHaveLength(1)
    expect(w[0].scope).toBe('tenant')
  })
})
