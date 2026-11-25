/**
 * 附件 URL 的来源与协议校验（PRD 9.9 / H64）。
 *
 * 缺陷形态：`file_url` / `url` 是**服务端返回的字符串**，`MessageItem` 直接把它塞进
 * `:src` / `:href`。于是：
 *   - `javascript:alert(1)` 一旦被放进 `href`，点一下就执行（渲染上下文不同则更糟）；
 *   - `file:///...` 让页面去读本机路径（Electron 下 `webSecurity` 关掉时真的会读）；
 *   - `http://10.0.0.5/admin` 让浏览者的浏览器去请求内网（CSRF/探活）；
 *   - `//evil.com/x.png` 是**协议相对**地址，容易被"看起来是同源"的说辞糊过去。
 *
 * 规则（默认拒绝，白名单放行）：
 *   1. 站内相对路径：必须以 `/media/` 或 `/v1/` 开头，且不是 `//` 开头（协议相对）；
 *   2. 绝对 URL：scheme 只允许 `https:`（本机回环允许 `http:`，因为桌面壳的本地栈就是
 *      `http://127.0.0.1:PORT`），host 必须命中受信任集合；
 *   3. `blob:` 允许（应用自己 createObjectURL 的预览），`data:` 只允许图片 MIME；
 *   4. 其它（`javascript:`/`file:`/`vbscript:`/未知）一律拒绝，调用方拿到空串后
 *      展示"附件地址不可用"而不是继续渲染。
 */

/** 受信任的对象存储 origin 由调用方注入（部署配置里的 S3/CDN 地址）。 */
export interface AttachmentUrlOptions {
  /** 应用自己的 origin（`location.origin`）；同源绝对 URL 直接放行。 */
  selfOrigin?: string
  /** 额外受信任的 origin（对象存储 / CDN）。 */
  trustedOrigins?: string[]
  /** 是否允许 `http:`（仅回环）。默认 true —— 桌面本地栈就是 http://127.0.0.1。 */
  allowLoopbackHttp?: boolean
}

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])
const SAFE_RELATIVE_PREFIXES = ['/media/', '/v1/']
const SAFE_DATA_PREFIXES = ['data:image/png', 'data:image/jpeg', 'data:image/webp', 'data:image/gif']

function normalizeOrigin(value: string): string {
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
    return url.origin
  } catch {
    return ''
  }
}

function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname.toLowerCase())
}

/**
 * 返回可安全渲染的 URL；不可信则返回空串（调用方必须显式处理"没有地址"）。
 *
 * 注意：**不要**在这里做"尽量修一下"（例如把 `javascript:` 剥掉前缀）——
 * 修出来的地址可能落到另一个攻击者可控的值上，比明确拒绝更难排查。
 */
export function sanitizeAttachmentUrl(raw: unknown, options: AttachmentUrlOptions = {}): string {
  const text = String(raw ?? '').trim()
  if (!text) return ''

  // 1) 协议相对地址：`//evil.com/x` 看起来像站内路径，实际是跨站 —— 先挡掉。
  if (text.startsWith('//')) return ''

  // 2) 站内相对路径。
  if (text.startsWith('/')) {
    return SAFE_RELATIVE_PREFIXES.some(prefix => text.startsWith(prefix)) ? text : ''
  }

  // 3) data: —— 只允许图片（体积与脚本风险都在这里兜）。
  const lower = text.toLowerCase()
  if (lower.startsWith('data:')) {
    return SAFE_DATA_PREFIXES.some(prefix => lower.startsWith(prefix)) ? text : ''
  }

  // 4) blob: —— 应用自己的对象 URL，交给浏览器按 URL 解析。
  if (lower.startsWith('blob:')) return text

  // 5) 绝对 URL：scheme + host 双校验。
  let url: URL
  try {
    url = new URL(text)
  } catch {
    return ''
  }
  const allowLoopbackHttp = options.allowLoopbackHttp !== false
  const trusted = new Set<string>()
  const self = options.selfOrigin ? normalizeOrigin(options.selfOrigin) : ''
  if (self) trusted.add(self)
  for (const origin of options.trustedOrigins ?? []) {
    const normalized = normalizeOrigin(origin)
    if (normalized) trusted.add(normalized)
  }

  if (url.protocol === 'https:') {
    return trusted.has(url.origin) ? url.toString() : ''
  }
  if (url.protocol === 'http:') {
    // 明文只允许回环（桌面本地栈），且仍需在受信任集合里或本身就是 self。
    if (!allowLoopbackHttp || !isLoopbackHost(url.hostname)) return ''
    if (trusted.size && !trusted.has(url.origin)) return ''
    return url.toString()
  }
  return ''
}

export { normalizeOrigin as normalizeAttachmentOrigin, isLoopbackHost }
