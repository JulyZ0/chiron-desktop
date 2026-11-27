/** F12：当前用户的日费用预算读数（只读；管理端配额是唯一写入口）。 */
import { api } from './index'

export interface UsageBudget {
  user_used_cents: number
  user_limit_cents: number
  tenant_used_cents: number
  tenant_limit_cents: number
}

export async function getUsageBudget(): Promise<UsageBudget> {
  const { data } = await api.get('/v1/usage/budget')
  const p = data?.data ?? {}
  return {
    user_used_cents: Number(p.user_used_cents ?? 0) || 0,
    user_limit_cents: Number(p.user_limit_cents ?? 0) || 0,
    tenant_used_cents: Number(p.tenant_used_cents ?? 0) || 0,
    tenant_limit_cents: Number(p.tenant_limit_cents ?? 0) || 0,
  }
}
