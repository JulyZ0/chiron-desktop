'use strict'
/**
 * 更新清单与安装器的真实性门禁（PRD 9.2 / H45）。
 *
 * 缺陷形态：
 *   1. 清单允许**空 `sha512`**；主进程只在摘要非空时才校验，随后直接启动安装器 ——
 *      也就是说"没有摘要"等于"免检"；
 *   2. 缓存包没有摘要也会被再次启动（下载后校验通过一次，之后不再看）；
 *   3. 没有任何**发布者签名**：只要清单来源被换掉（镜像、劫持、被改的 release），
 *      安装器就是"看起来像官方"的任意二进制；
 *   4. 清单里的文件名/大小/架构不做 schema 校验，重定向也不限制域名。
 *
 * 这一层是纯函数，便于把每条判据单独钉住。口径是**默认拒绝**：
 * 缺字段、超范围、域名不对、摘要不匹配、无签名 —— 一律拒绝执行安装器。
 */
const crypto = require('node:crypto')

/** 允许的清单下载域（重定向后也必须落在这里）。 */
const ALLOWED_MANIFEST_HOSTS = ['github.com', 'api.github.com', 'objects.githubusercontent.com']

/** sha512 base64 解码后必须是 64 字节。 */
const SHA512_BYTES = 64

function isAllowedHost(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ''))
    if (url.protocol !== 'https:') return false
    const host = url.hostname.toLowerCase()
    return ALLOWED_MANIFEST_HOSTS.includes(host) || host.endsWith('.githubusercontent.com')
  } catch {
    return false
  }
}

/** 版本必须是可解析的 semver（拒绝 `latest`、空串、带路径分隔符的"版本"）。 */
function isValidVersion(version) {
  const text = String(version || '').trim().replace(/^v/i, '')
  if (!text) return false
  if (text.includes('/') || text.includes('\\') || text.includes('..')) return false
  return /^\d+(\.\d+){0,2}(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/.test(text)
}

/**
 * 校验清单结构（H45）。返回 `{ok, manifest?|errors}`。
 *
 * 为什么要求"文件名与 url 一致"：安装器落盘用的是 `file.url` 的名字，
 * 两者不一致时，后面"启动这个文件"到底启动了什么就说不清了。
 */
function validateLatestManifest(parsed, options = {}) {
  const errors = []
  const platform = String(options.platform || process.platform)
  const arch = String(options.arch || process.arch)

  if (!parsed || typeof parsed !== 'object') {
    return { ok: false, errors: ['manifest-missing'] }
  }
  const version = String(parsed.version || '').trim()
  if (!isValidVersion(version)) errors.push('version-invalid')

  const file = parsed.file || null
  if (!file || typeof file.url !== 'string' || !file.url) {
    errors.push('installer-missing')
    return { ok: false, errors }
  }

  const url = String(file.url)
  // electron-builder 的 latest.yml 里 `files[].url` **只是文件名**（下载地址由
  // 客户端用可信 repo/tag 拼出来）；只有显式写了绝对 URL 时才做域名白名单。
  // 相对名同样不能带路径分隔符 —— 否则拼接后会跳出 release 目录。
  const absolute = /^[a-z][a-z0-9+.-]*:/i.test(url)
  if (absolute && !isAllowedHost(url)) errors.push('installer-host-not-allowed')
  const name = url.split('?')[0].split('/').pop() || ''
  if (platform === 'win32' && !/\.(exe|msi)$/i.test(name)) errors.push('installer-extension')
  if (url.includes('..') || url.includes('\\') || name.includes('/') || name.includes('\\')) {
    errors.push('installer-name-unsafe')
  }

  // 架构：清单里带 x64/arm64 标记时必须与当前进程一致，避免装错包。
  const lower = name.toLowerCase()
  const wantsArm = arch === 'arm64'
  if (lower.includes('arm64') && !wantsArm) errors.push('arch-mismatch')
  if (lower.includes('x64') && wantsArm) errors.push('arch-mismatch')

  // 大小是**可选**字段：electron-builder 26 起 `files[]` 只写 `url` + `sha512`
  // （见 app-builder-lib `out/publish/updateInfoBuilder.js::createUpdateInfo`），
  // 真实清单里根本没有 size。缺省时不算错误 —— 完整性由强制的 sha512 兜底；
  // 但**显式写了**非正整数（0、负数、小数、垃圾字符串）仍然拒绝。
  const hasSize =
    file.size !== undefined && file.size !== null && String(file.size).trim() !== ''
  let size = null
  if (hasSize) {
    size = Number(file.size)
    if (!Number.isInteger(size) || size <= 0) errors.push('size-invalid')
  }

  const sha512 = String(file.sha512 || '').trim()
  if (!sha512) {
    // 这一条是本条的核心：没有摘要不是"跳过校验"，而是拒绝。
    errors.push('sha512-missing')
  } else if (!isValidSha512(sha512)) {
    errors.push('sha512-invalid')
  }

  if (errors.length) return { ok: false, errors }
  return {
    ok: true,
    manifest: {
      version,
      file: { url, size, sha512 },
      notes: typeof parsed.notes === 'string' ? parsed.notes : '',
    },
  }
}

function isValidSha512(value) {
  const text = String(value || '').trim().replace(/-/g, '+').replace(/_/g, '/')
  let buf
  try {
    buf = Buffer.from(text, 'base64')
  } catch {
    return false
  }
  return buf.length === SHA512_BYTES
}

/** 计算文件 sha512（与 electron-builder 清单里的 `sha512` 同口径：base64）。 */
function sha512Base64(buffer) {
  return crypto.createHash('sha512').update(buffer).digest('base64')
}

/** 摘要比对：缺任一即 false（fail-closed，绝不"没有摘要就算通过"）。 */
function digestMatches(buffer, expected) {
  const want = String(expected || '').trim()
  if (!want) return false
  const got = sha512Base64(buffer)
  const a = Buffer.from(got.replace(/-/g, '+').replace(/_/g, '/'), 'base64')
  const b = Buffer.from(want.replace(/-/g, '+').replace(/_/g, '/'), 'base64')
  if (a.length !== b.length) return false
  return crypto.timingSafeEqual(a, b)
}

/**
 * 校验发布者签名（Ed25519，H45）。
 *
 * @param {Buffer|string} payload 被签名的**原始字节**（清单正文 / 安装器字节）
 * @param {string} signatureBase64 签名的 base64
 * @param {string} publicKeyBase64 内嵌公钥（SPKI DER 的 base64）
 * @returns {{ok:boolean, reason?:string}}
 *
 * 没有公钥时返回 `no-public-key` —— 这是**预期**的 fail-closed：
 * 发布签名根尚未落地（PRD 9.4 决策 4），此时"检查更新"可用、"执行安装器"不可用。
 */
function verifySignature(payload, signatureBase64, publicKeyBase64) {
  const key = String(publicKeyBase64 || '').trim()
  if (!key) return { ok: false, reason: 'no-public-key' }
  const sig = String(signatureBase64 || '').trim()
  if (!sig) return { ok: false, reason: 'missing-signature' }
  try {
    const publicKey = crypto.createPublicKey({
      key: Buffer.from(key, 'base64'),
      format: 'der',
      type: 'spki',
    })
    const body = Buffer.isBuffer(payload) ? payload : Buffer.from(String(payload), 'utf8')
    const ok = crypto.verify(null, body, publicKey, Buffer.from(sig, 'base64'))
    return ok ? { ok: true } : { ok: false, reason: 'bad-signature' }
  } catch (err) {
    return { ok: false, reason: `signature-error:${(err && err.message) || err}` }
  }
}

/**
 * 启动安装器前的**完整**门禁（H45）。
 *
 * 顺序即语义：先结构、再摘要、最后签名。任一步失败都返回可读原因，
 * 让界面能说清"为什么这个包不能装"，而不是静默启动一个未验证的二进制。
 */
function gateInstallerLaunch(input = {}) {
  const buffer = input.buffer
  if (!buffer || !Buffer.isBuffer(buffer) || buffer.length === 0) {
    return { ok: false, reason: 'installer-missing' }
  }
  const manifest = input.manifest || {}
  const expected = String((manifest.file && manifest.file.sha512) || '')
  if (!expected) return { ok: false, reason: 'sha512-missing' }
  if (!digestMatches(buffer, expected)) return { ok: false, reason: 'sha512-mismatch' }

  const sig = verifySignature(buffer, input.installerSignature, input.publicKey)
  if (!sig.ok) return { ok: false, reason: sig.reason }
  return { ok: true }
}

module.exports = {
  ALLOWED_MANIFEST_HOSTS,
  isAllowedHost,
  isValidVersion,
  isValidSha512,
  sha512Base64,
  digestMatches,
  validateLatestManifest,
  verifySignature,
  gateInstallerLaunch,
}
