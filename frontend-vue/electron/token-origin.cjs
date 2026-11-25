/**
 * 云端 Bearer 凭据与 API origin 的绑定（PRD H79）。
 *
 * ## 缺陷形态
 *
 * 旧实现把 token 裸存进 safeStorage：没有"这份凭据是哪个服务签发的"这一层元数据。
 * 用户登录 A 服务后再把服务地址改成 B，`desktop:set-api-url` 只写 `apiUrl` 并重启窗口，
 * 旧 token 原样留在盘上，下一次启动 `desktop:get-token` 直接把它恢复成"云端登录态" ——
 * 于是 A 的 token 被 Axios / SSE / 重试送到 B，B 拿到别人的凭据，A 的会话也被泄露给
 * 一个不属于它的服务。
 *
 * ## 治法
 *
 * 凭据**只能**以「(token, 规范化 origin)」这一对存取。存取两侧都过这里：
 *   · 写：origin 必须可规范化，否则拒绝落盘（宁可不记住登录，也不存一份说不清归属的凭据）；
 *   · 读：调用方必须给出**当前**期望 origin；不匹配就判为失效、清盘、要求重新登录。
 *
 * 规范化口径固定为 `scheme://host:port`（默认端口显式写出）：
 *   - host 小写（URL 已代劳）；
 *   - http(s) 默认端口补 80/443 ⇒ `https://a.b` 与 `https://a.b:443` 视为同一份凭据；
 *   - 路径/query 不参与：`https://a.b/api` 与 `https://a.b` 是同一个认证域。
 *
 * 纯函数、零 Electron 依赖：单测（`node --test electron/__tests__/*.test.cjs`）直接跑。
 */
'use strict'

/** 显式补全默认端口，避免 `https://a.b` 与 `https://a.b:443` 被当成两个认证域 */
const DEFAULT_PORTS = { 'http:': '80', 'https:': '443' }

/** 落盘信封版本：老版本存的是裸 token（JSON.parse 必然失败 ⇒ 判为 legacy） */
const ENVELOPE_VERSION = 2

/**
 * 把任意地址收敛成 `scheme://host:port`；无法确定认证域时返回空串。
 * 空串是**明确的失败信号**，调用方不得把它当成"同源"。
 */
function normalizeOrigin(raw) {
  const text = String(raw ?? '').trim()
  if (!text) return ''
  let url
  try {
    url = new URL(text)
  } catch {
    return ''
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
  const host = url.hostname.toLowerCase()
  if (!host) return ''
  const port = url.port || DEFAULT_PORTS[url.protocol]
  return `${url.protocol}//${host}:${port}`
}

/** 两个地址是否属于同一个认证域（任一侧不可解析 ⇒ false，fail-closed）。 */
function sameOrigin(a, b) {
  const left = normalizeOrigin(a)
  return Boolean(left) && left === normalizeOrigin(b)
}

/**
 * 生成落盘信封（明文形态；加密由 token-store 的 safeStorage 负责）。
 * 没有可信 origin 就**拒绝**生成 —— 这是"不再产生无归属凭据"的唯一入口。
 */
function encodeEnvelope(input = {}) {
  const token = String(input.token ?? '')
  const origin = normalizeOrigin(input.origin)
  if (!token) throw new Error('云端凭据不能为空')
  if (!origin) throw new Error('缺少可信的 API origin，拒绝保存云端凭据')
  const savedAt = typeof input.savedAt === 'string' && input.savedAt
    ? input.savedAt
    : new Date().toISOString()
  return JSON.stringify({ v: ENVELOPE_VERSION, origin, token, savedAt })
}

/**
 * 解析落盘信封。
 * @returns {{ok:boolean, token?:string, origin?:string, savedAt?:string, reason?:string}}
 *   reason: 'empty' 无文件内容 / 'legacy' 旧版裸 token / 'malformed' 结构损坏 / 'version' 未知版本
 */
function decodeEnvelope(raw) {
  const text = String(raw ?? '')
  if (!text.trim()) return { ok: false, reason: 'empty' }
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    // 旧版直接加密了 token 本身：解密出来是一段非 JSON 文本。它没有 origin 元数据，
    // 无法证明归属 ⇒ 与"损坏"同等对待（见 evaluateStoredToken 的清盘语义）。
    return { ok: false, reason: 'legacy' }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, reason: 'malformed' }
  }
  if (parsed.v !== ENVELOPE_VERSION) return { ok: false, reason: 'version' }
  const token = typeof parsed.token === 'string' ? parsed.token : ''
  const origin = normalizeOrigin(parsed.origin)
  if (!token || !origin) return { ok: false, reason: 'malformed' }
  return {
    ok: true,
    token,
    origin,
    savedAt: typeof parsed.savedAt === 'string' ? parsed.savedAt : '',
  }
}

/**
 * 读取判定：这份盘上凭据能不能交给**当前**这个 origin 用。
 *
 * @param {string} raw 解密后的明文
 * @param {string} expectedOrigin 当前期望的认证域（调用方从 config 解析）
 * @returns {{token:string, clear:boolean, reason:string}}
 *   token 为空即"不恢复"；clear=true 表示应当立刻删除盘上凭据（不可证明归属 / 跨域）。
 *
 * 为什么 `expectedOrigin` 为空时不 clear：那种状态是"这台机器还没配置任何云端地址"，
 * 凭据很可能仍然属于用户稍后会填回来的那个服务；此时只是**不交出** token（返回值空），
 * 不销毁用户的登录态。一旦地址存在且不同，就属于确凿的跨域，必须清盘。
 */
function evaluateStoredToken(raw, expectedOrigin) {
  const expected = normalizeOrigin(expectedOrigin)
  const decoded = decodeEnvelope(raw)
  if (!decoded.ok) {
    // 'empty' 只是没有文件，不存在"旧凭据"可清
    return { token: '', clear: decoded.reason !== 'empty', reason: decoded.reason }
  }
  if (!expected) return { token: '', clear: false, reason: 'no-expected-origin' }
  if (decoded.origin !== expected) return { token: '', clear: true, reason: 'origin-mismatch' }
  return { token: decoded.token, clear: false, reason: 'ok' }
}

module.exports = {
  DEFAULT_PORTS,
  ENVELOPE_VERSION,
  normalizeOrigin,
  sameOrigin,
  encodeEnvelope,
  decodeEnvelope,
  evaluateStoredToken,
}
