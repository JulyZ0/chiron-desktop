/** 服务地址的传输安全策略（前端侧，PRD 9.2 / H50）。
 *
 * 与主进程 `electron/api-url-policy.cjs` **同口径**：配置保存时校验一次，
 * 这里在"建立连接前"再校验一次 —— 两次都必要：
 *   - 只在保存时校验：配置被手工改过（`config.json` 是用户可手改的）就绕过了；
 *   - 只在连接时校验：token 已经可能被写进磁盘上的旧地址。
 *
 * 另一条独立约束：**凭据不许发给明文非回环地址**。即使用户显式开启"允许不安全
 * 连接"（自建内网 http），token/Cookie 也不发 —— 那里本来就是明文可嗅探的。
 */

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1'])

export function isLoopbackHostname(hostname: string): boolean {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '')
  if (LOOPBACK_HOSTS.has(host)) return true
  return /^127\./.test(host)
}

export interface ApiUrlVerdict {
  ok: boolean
  url?: string
  isLoopback?: boolean
  reason?: string
  /**
   * 失败原因的 **i18n key**（`apiUrl.*`）。
   *
   * 刻意不在这里拼中文：本仓的 i18n 契约（`scripts/check-i18n.mjs`）要求用户可见
   * 文案走 `t()`，工具模块里的字面量过不了门禁，也无法被翻译。
   */
  messageKey?: string
}

export function classifyApiUrl(raw: string, options: { allowInsecure?: boolean } = {}): ApiUrlVerdict {
  const text = String(raw || '').trim()
  if (!text) return { ok: false, reason: 'empty', messageKey: 'apiUrl.empty' }

  let url: URL
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: 'unparsable', messageKey: 'apiUrl.unparsable' }
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return { ok: false, reason: 'scheme', messageKey: 'apiUrl.scheme' }
  }
  if (url.username || url.password) {
    return { ok: false, reason: 'credentials-in-url', messageKey: 'apiUrl.credentialsInUrl' }
  }

  const loopback = isLoopbackHostname(url.hostname)
  if (url.protocol === 'http:' && !loopback && options.allowInsecure !== true) {
    return {
      ok: false,
      reason: 'insecure-scheme',
      isLoopback: false,
      messageKey: 'apiUrl.insecure',
    }
  }
  return { ok: true, url: url.origin, isLoopback: loopback }
}

/** 能否向该地址发送凭据（token / Cookie / 私有内容）。 */
export function canSendCredentials(raw: string): { ok: boolean; reason?: string } {
  const verdict = classifyApiUrl(raw, { allowInsecure: true })
  if (!verdict.ok) return { ok: false, reason: verdict.reason }
  const url = String(verdict.url || '')
  if (url.startsWith('https:') || verdict.isLoopback) return { ok: true }
  return { ok: false, reason: 'plaintext-non-loopback' }
}
