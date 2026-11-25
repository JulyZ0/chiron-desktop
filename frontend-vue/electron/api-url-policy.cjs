'use strict'
/**
 * 服务地址的传输安全策略（PRD 9.2 / H50）。
 *
 * 缺陷形态：配置层接受**任意** http(s) 地址，API 客户端随后向它附加 Bearer token，
 * CSP 也会放行该 origin。于是把地址填成 `http://team.example.com` 就等于
 * 让 token、会话 Cookie 与全部私有内容走明文；填成内网地址还会把凭据送给内网服务。
 *
 * 口径：
 *   - loopback（`localhost` / `127.0.0.1` / `[::1]`）允许 http —— 桌面本地栈就是 http；
 *   - 其余主机**必须** https；
 *   - 开发/自建需要 http 时必须显式 `allowInsecure`（默认关，且写进配置才生效）；
 *   - 任何情况下都拒绝非 http(s) scheme（`file:`/`javascript:` 这类绝不该成为 base URL）。
 *
 * 纯函数：保存配置时与建立连接前**各校验一次**，两侧共用同一判据。
 */

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])

function isLoopbackHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/^\[|\]$/g, '')
  if (LOOPBACK_HOSTS.has(host) || LOOPBACK_HOSTS.has(`[${host}]`)) return true
  return /^127\./.test(host)
}

/**
 * 校验并规范化服务地址。
 *
 * @param {string} raw 用户输入/配置文件里的地址
 * @param {{allowInsecure?: boolean}} [options]
 * @returns {{ok: boolean, url?: string, isLoopback?: boolean, reason?: string, message?: string}}
 */
function classifyApiUrl(raw, options = {}) {
  const text = String(raw || '').trim()
  if (!text) return { ok: false, reason: 'empty', message: '服务地址为空' }

  let url
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: 'unparsable', message: `无法解析的服务地址：${text}` }
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return { ok: false, reason: 'scheme', message: `只接受 http(s) 地址，收到 ${url.protocol}` }
  }
  if (url.username || url.password) {
    // 凭据写在 URL 里会进日志、进 shell 历史、进截图。
    return { ok: false, reason: 'credentials-in-url', message: '服务地址不能内嵌用户名/密码' }
  }

  const loopback = isLoopbackHost(url.hostname)
  if (url.protocol === 'http:' && !loopback && options.allowInsecure !== true) {
    return {
      ok: false,
      reason: 'insecure-scheme',
      isLoopback: false,
      message: '远程服务必须使用 HTTPS；如确需明文，请显式开启「允许不安全连接」',
    }
  }

  // 去掉路径/查询：base URL 只保留 origin（各接口自己带路径）。
  return { ok: true, url: url.origin, isLoopback: loopback }
}

/**
 * 是否可以对这个地址发送凭据（token / Cookie / 私有内容）。
 *
 * 与 `classifyApiUrl` 分开：即使有人用 `allowInsecure` 存下了一个明文地址，
 * **凭据仍然不许发**（明文下再"允许"就等于主动泄漏）。
 */
function canSendCredentials(raw) {
  const verdict = classifyApiUrl(raw, { allowInsecure: true })
  if (!verdict.ok) return { ok: false, reason: verdict.reason }
  if (verdict.url.startsWith('https:') || verdict.isLoopback) return { ok: true }
  return { ok: false, reason: 'plaintext-non-loopback' }
}

module.exports = { LOOPBACK_HOSTS, isLoopbackHost, classifyApiUrl, canSendCredentials }
