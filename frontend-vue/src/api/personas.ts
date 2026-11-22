/**
 * 人格（persona）API。
 *
 * 用户侧只读**启用**的列表（对话里的选择器）；管理侧维护完整人格库。
 * 注意：用户侧列表**不含** `system_prompt` —— 提示词由服务端在提交时按 id 解析，
 * 前端不持有文案（避免展示与生效文案漂移，也不把提示词暴露成可篡改的请求字段）。
 */
import { api } from './index'

export interface Persona {
  id: string
  name: string
  description: string
  system_prompt?: string
  enabled: boolean
  is_default: boolean
  preset?: string
  sort_order: number
  created_at?: string
  updated_at?: string
}

export interface PersonaDraft {
  name: string
  description: string
  system_prompt: string
  enabled: boolean
  is_default: boolean
  sort_order: number
}

function normalize(raw: unknown): Persona {
  const p = (raw ?? {}) as Record<string, unknown>
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')
  return {
    id: str(p.id),
    name: str(p.name),
    description: str(p.description),
    system_prompt: p.system_prompt === undefined ? undefined : str(p.system_prompt),
    enabled: p.enabled !== false,
    is_default: p.is_default === true,
    preset: str(p.preset) || undefined,
    sort_order: typeof p.sort_order === 'number' ? p.sort_order : 0,
    created_at: str(p.created_at) || undefined,
    updated_at: str(p.updated_at) || undefined,
  }
}

/** 对话内可选人格（仅启用条目）。 */
export async function listPersonas(): Promise<Persona[]> {
  const { data } = await api.get('/v1/personas')
  const list = data?.data?.personas
  if (!Array.isArray(list)) return []
  return list.map(normalize).filter(p => p.id)
}

/** 管理台：完整人格库（含停用）。 */
export async function listAdminPersonas(): Promise<Persona[]> {
  const { data } = await api.get('/v1/admin/personas')
  const list = data?.data?.personas
  if (!Array.isArray(list)) return []
  return list.map(normalize).filter(p => p.id)
}

export async function createPersona(draft: PersonaDraft): Promise<Persona> {
  const { data } = await api.post('/v1/admin/personas', draft)
  return normalize(data?.data)
}

export async function updatePersona(id: string, draft: Partial<PersonaDraft>): Promise<Persona> {
  const { data } = await api.put(`/v1/admin/personas/${encodeURIComponent(id)}`, draft)
  return normalize(data?.data)
}

export async function deletePersona(id: string): Promise<number> {
  const { data } = await api.delete(`/v1/admin/personas/${encodeURIComponent(id)}`)
  return typeof data?.data?.deleted === 'number' ? data.data.deleted : 0
}

export async function setDefaultPersona(id: string): Promise<Persona> {
  const { data } = await api.post(`/v1/admin/personas/${encodeURIComponent(id)}/default`)
  return normalize(data?.data)
}
