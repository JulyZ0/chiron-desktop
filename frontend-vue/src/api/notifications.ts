/**
 * 站内通知 API（网关 `internal/api/notifications.go`）。
 *
 * ## 只有读与"标已读"
 *
 * 通知的**产生**不经过 HTTP：它来自系统自己看到的事件（子 Agent 失败、分享被
 * 管理员撤销）。所以这里没有 `createNotification` —— 让客户端能造通知，等于
 * 允许任何人给自己灌一条"支付成功"，那正是 SaaS 残留文案给人的错觉。
 *
 * ## payload 是自由 JSON，不是渲染结果
 *
 * 服务端只存 `kind` + `payload`，文案由 `locales/<lang>/notifications.ts` 按**当前
 * 界面语言**渲染。因此本文件不做任何文案拼接：字段原样交给页面，页面负责取
 * `payload.profile` / `payload.title` 之类并套模板。新增 kind 必须同时加三份文案。
 */
import { api } from './index'

/** 与后端 `NotifSubagentFailed` / `NotifShareRevoked` 一一对应。 */
export type NotificationKind = 'subagent_failed' | 'share_revoked' | string

export interface NotificationItem {
  id: string
  kind: NotificationKind
  /** kind 相关的原始字段（profile / status / error_code / title / share_id …） */
  payload: Record<string, unknown>
  /** 关联会话；空串表示这条通知不指向某个会话 */
  session_id?: string
  read: boolean
  read_at?: string
  created_at: string
}

export interface NotificationList {
  items: NotificationItem[]
  /** 服务端顺手算出的未读数，切筛选时不必再发一次请求 */
  unread_count: number
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/**
 * 归一化一行。
 *
 * 服务端契约已经稳定，这里仍然逐字段兜底：一条通知渲染失败不该让整页变成
 * 错误态 —— 而 `kind` 是我们唯一能安全降级的字段（页面会退回显示原始 key）。
 */
function normalizeItem(raw: unknown): NotificationItem | null {
  const r = (raw ?? {}) as Record<string, unknown>
  const id = str(r.id)
  if (!id) return null
  const payload = (r.payload ?? {}) as Record<string, unknown>
  return {
    id,
    kind: str(r.kind),
    payload: payload && typeof payload === 'object' ? payload : {},
    session_id: str(r.session_id),
    read: r.read === true,
    read_at: str(r.read_at) || undefined,
    created_at: str(r.created_at),
  }
}

/**
 * 列出我的通知。
 *
 * `unreadOnly` 对应后端 `?unread=1`；`limit` 由后端夹紧到 200（越界回落到默认，
 * 不报 400 —— 显示条数不值得把整页打成错误态）。
 */
export async function listNotifications(
  options: { unreadOnly?: boolean; limit?: number } = {},
): Promise<NotificationList> {
  const params: Record<string, string> = {}
  if (options.unreadOnly) params.unread = '1'
  if (options.limit) params.limit = String(options.limit)
  const { data } = await api.get('/v1/notifications', { params })
  const payload = data?.data ?? {}
  const rawItems: unknown[] = Array.isArray(payload.items) ? payload.items : []
  return {
    items: rawItems.map(normalizeItem).filter((it): it is NotificationItem => it !== null),
    unread_count: typeof payload.unread_count === 'number' ? payload.unread_count : 0,
  }
}

/** 只取未读数（角标轮询用）。单独一条 `COUNT(*)`，不拉全量行。 */
export async function fetchUnreadCount(): Promise<number> {
  const { data } = await api.get('/v1/notifications/unread')
  const n = data?.data?.unread_count
  return typeof n === 'number' ? n : 0
}

/**
 * 标为已读：`{ ids }` 指定几条，或 `{ all: true }` 一把清空。
 *
 * 幂等 —— 已读的再标一次仍返回成功。返回实际更新条数，界面用它决定要不要提示
 * "已标为 N 条已读"（N 为 0 时没什么可说的）。
 */
export async function markNotificationsRead(
  body: { ids?: string[]; all?: boolean },
): Promise<number> {
  const { data } = await api.post('/v1/notifications/read', body)
  const n = data?.data?.updated
  return typeof n === 'number' ? n : 0
}

/** 永久删除单条通知。返回实际删除条数（0 = 已不存在 / 不属于我）。 */
export async function deleteNotification(id: string): Promise<number> {
  const { data } = await api.delete(`/v1/notifications/${encodeURIComponent(id)}`)
  const n = data?.data?.deleted
  return typeof n === 'number' ? n : 0
}

/**
 * 批量清理。默认 `read` = 只删已读（一次误触不该把没看过的通知丢掉）。
 * 返回实际删除条数。
 */
export async function clearNotifications(scope: 'read' | 'unread' | 'all' = 'read'): Promise<number> {
  const { data } = await api.delete('/v1/notifications', { params: { read: scope } })
  const n = data?.data?.deleted
  return typeof n === 'number' ? n : 0
}
