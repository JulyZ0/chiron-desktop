/**
 * F12 的纯判据：给定当日费用/上限，算出该提醒哪些阈值。
 *
 * 阈值固定 80% / 100%，不自动中断（是否拒绝由服务端 cost guard 决定）。
 * 抽成纯函数是因为"提醒了却算错百分比"很难在界面上发现：用户只会在
 * 被 429 拦下时才发现预算早就不够了。
 */
export interface BudgetSnapshot {
  user_used_cents: number
  user_limit_cents: number
  tenant_used_cents: number
  tenant_limit_cents: number
}

export interface BudgetWarning {
  scope: 'user' | 'tenant'
  level: 80 | 100
  pct: number
  usedCents: number
  limitCents: number
}

export function evaluateBudget(b: BudgetSnapshot): BudgetWarning[] {
  const out: BudgetWarning[] = []
  const pairs: Array<{ scope: 'user' | 'tenant'; used: number; limit: number }> = [
    { scope: 'user', used: b.user_used_cents, limit: b.user_limit_cents },
    { scope: 'tenant', used: b.tenant_used_cents, limit: b.tenant_limit_cents },
  ]
  for (const p of pairs) {
    if (p.limit <= 0) continue // 0 = 未设置/不限
    const pct = Math.floor((p.used / p.limit) * 100)
    if (pct >= 80) {
      out.push({ scope: p.scope, level: 80, pct, usedCents: p.used, limitCents: p.limit })
    }
    if (pct >= 100) {
      out.push({ scope: p.scope, level: 100, pct, usedCents: p.used, limitCents: p.limit })
    }
  }
  return out
}
