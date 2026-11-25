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
  message?: string
}

export function classifyApiUrl(raw: string, options: { allowInsecure?: boolean } = {}): ApiUrlVerdict {
  const text = String(raw || '').trim()
  if (!text) return { ok: false, reason: 'empty', message: '服务地址为空' }

  let url: URL
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: 'unparsable', message: `无法解析的服务地址：${text}` }
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return { ok: false, reason: 'scheme', message: `只接受 http(s) 地址，收到 ${url.protocol}` }
  }
  if (url.username || url.password) {
    return { ok: false, reason: 'credentials-in-url', message: '服务地址不能内嵌用户名/密码' }
  }

  const loopback = isLoopbackHostname(url.hostname)
  if (url.protocol === 'http:' && !loopback && options.allowInsecure !== true) {
    return {
      ok: false,
      reason: 'insecure-scheme',
      isLoopback: false,
      message: '远程服务必须使用 HTTPS；如确需明文，请显式开启「允许不安全连接」',
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
