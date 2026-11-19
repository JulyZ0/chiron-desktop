/**
 * 分享链接的唯一出口。
 *
 * 事故形态：桌面端页面跑在 `file://` 上（Electron `loadFile`），用
 * `window.location.origin` 拼出来的是 `file:///s/<token>` —— 看起来像链接，
 * 换台机器打开就是「找不到文件」。根因不是路径写错，而是**客户端不知道自己的
 * 对外地址**，所以链接以后端产出为准（`internal/api/share_link.go`），前端只兜底。
 *
 * 规则：
 * 1. 后端给了 `url` 且是绝对 http(s) 地址 → 用它（PUBLIC_BASE_URL 或网关按请求推导）；
 * 2. 没给 url、但页面本身是 http(s) → 用当前 origin 拼（与引入前一致）；
 * 3. 其它情况（`file://`、空 origin、后端给了相对路径）→ **抛错**，绝不返回相对路径。
 *
 * 前缀统一为 `/s/`：`/s/:token` 与旧 `/share/:id` 都由路由提供，旧链接保持可用。
 */
export const SHARE_PATH_PREFIX = '/s/'

/** 站内分享路径（相对，用于 router.push / <a href>）。 */
export function sharePath(token: string): string {
  return `${SHARE_PATH_PREFIX}${encodeURIComponent(token)}`
}

/** 分享链接不可用时的原因（调用方据此选提示文案，不必解析字符串）。 */
export type ShareLinkErrorReason =
  /** 后端没给 url，且当前页面不是 http(s)（桌面端 file:// 是典型场景） */
  | 'not-configured'
  /** 后端给了 url，但不是绝对 http(s) 地址（相对路径在这里等同于坏链） */
  | 'not-absolute'

/** 分享链接不可用。message 只用于日志/兜底展示，UI 走 reason + i18n。 */
export class ShareLinkUnavailableError extends Error {
  readonly reason: ShareLinkErrorReason

  constructor(reason: ShareLinkErrorReason) {
    super(
      reason === 'not-absolute'
        ? 'share url from server is not an absolute http(s) url'
        : 'share url unavailable: page is not served over http(s) and server did not provide one',
    )
    this.name = 'ShareLinkUnavailableError'
    this.reason = reason
  }
}

/** 绝对 http(s) 地址判定；其余（相对路径、file:、javascript: …）一律视为不可用。 */
export function isAbsoluteHttpUrl(value: string): boolean {
  return /^https?:\/\/[^\s]+$/i.test(value)
}

/**
 * 解析最终要展示/复制的分享链接。
 *
 * @param provided 后端返回的 `url`（可能是 undefined / 空串 / 相对路径）
 * @param origin   当前页面的 origin（浏览器里就是 `window.location.origin`）
 * @param token    分享 token，仅在没有 provided 时用于按 origin 兜底拼装
 * @throws ShareLinkUnavailableError 无法产出可达链接时
 */
export function resolveShareUrl(
  provided: string | null | undefined,
  origin: string,
  token: string,
): string {
  const serverUrl = (provided ?? '').trim()
  if (serverUrl) {
    if (!isAbsoluteHttpUrl(serverUrl)) throw new ShareLinkUnavailableError('not-absolute')
    return serverUrl
  }

  const base = (origin ?? '').trim().replace(/\/+$/, '')
  if (!isAbsoluteHttpUrl(base)) throw new ShareLinkUnavailableError('not-configured')

  const id = (token ?? '').trim()
  if (!id) throw new ShareLinkUnavailableError('not-configured')
  return `${base}${sharePath(id)}`
}

/**
 * 安全版：拿不到可达链接时返回空串（模板里直接绑定用，不能让它抛）。
 * 需要区分原因的地方用 `resolveShareUrl`。
 */
export function safeShareUrl(
  provided: string | null | undefined,
  origin: string,
  token: string,
): string {
  try {
    return resolveShareUrl(provided, origin, token)
  } catch {
    return ''
  }
}

/**
 * 仅按 token 拼链接（兼容旧调用点）。
 *
 * 没有后端 url 可用的场景才走这里；`file://` 下同样返回空串 —— 调用方应优先用
 * `resolveShareUrl(serverUrl, ...)`。仍保留 `origin` 注入以便单测与站内跳转。
 */
export function shareUrl(token: string, origin?: string): string {
  const t = String(token ?? '').trim()
  if (!t) return ''
  const base = origin ?? (typeof window !== 'undefined' ? window.location.origin : '')
  return safeShareUrl(undefined, base, t)
}
