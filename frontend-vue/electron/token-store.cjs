/**
 * 桌面端 Bearer token 的加密存储。
 *
 * 不放 localStorage：Electron 的 file:// 页面里任何 XSS 都能读 localStorage。
 * 这里用系统凭据库（Windows DPAPI / macOS Keychain / Linux libsecret）加密后
 * 落到 userData/token.bin。safeStorage 不可用时返回失败，token 只保留在当前
 * 进程内存中，绝不退化成明文持久化。
 *
 * Electron 45 起同步的 isEncryptionAvailable / encryptString / decryptString
 * 已弃用，这里统一使用 async API；decryptStringAsync 在密钥轮换后会返回
 * shouldReEncrypt，此时立刻用新密钥重写一次。
 *
 * ## H79：凭据必须带 origin
 *
 * 盘上存的不再是裸 token，而是 `token-origin.cjs` 的 v2 信封
 * `{ v: 2, origin, token, savedAt }`。读写两侧都由那个模块判定：
 *   · 写：没有可信 origin 就拒绝落盘；
 *   · 读：调用方必须传入**当前**期望 origin，不匹配即清盘并返回空串。
 * 旧版本留下的裸 token 没有 origin 元数据，无法证明归属 ⇒ 与"损坏"同样清盘，
 * 用户重新登录一次即可（安全代价一次性，远小于把 A 的凭据发给 B）。
 */
const { app, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const tokenOrigin = require('./token-origin.cjs')

function tokenPath() {
  return path.join(app.getPath('userData'), 'token.bin')
}

/** 删盘（不存在也成功）：清理失败不应把调用方拖成异常路径。 */
async function removeStoredToken() {
  try {
    await fs.promises.rm(tokenPath(), { force: true })
    return true
  } catch {
    return false
  }
}

/**
 * 读取凭据并给出判定（给主进程记日志用）。
 *
 * @param {string} expectedOrigin 当前期望的 API 认证域（`scheme://host:port`）
 * @returns {Promise<{token:string, cleared:boolean, reason:string}>}
 */
async function readTokenResult(expectedOrigin) {
  try {
    if (!(await safeStorage.isAsyncEncryptionAvailable())) {
      return { token: '', cleared: false, reason: 'encryption-unavailable' }
    }
    const encrypted = await fs.promises.readFile(tokenPath())
    const { result, shouldReEncrypt } = await safeStorage.decryptStringAsync(encrypted)
    const verdict = tokenOrigin.evaluateStoredToken(result, expectedOrigin)
    if (verdict.clear) {
      await removeStoredToken()
      return { token: '', cleared: true, reason: verdict.reason }
    }
    if (shouldReEncrypt && verdict.token) {
      // 密钥轮换：按**盘上记录的** origin 重写，不要用调用方传入的那个。
      const decoded = tokenOrigin.decodeEnvelope(result)
      if (decoded.ok) await writeToken(decoded.token, decoded.origin)
    }
    return { token: verdict.token, cleared: false, reason: verdict.reason }
  } catch (err) {
    if (err && err.code === 'ENOENT') return { token: '', cleared: false, reason: 'empty' }
    return { token: '', cleared: false, reason: 'error' }
  }
}

/** 兼容旧调用点的字符串形态：只返回 token，判定细节见 `readTokenResult`。 */
async function readToken(expectedOrigin) {
  return (await readTokenResult(expectedOrigin)).token
}

/**
 * 写入凭据。空 token = 删除。
 *
 * @param {string} token
 * @param {string} origin 这份凭据的目标 API 认证域；空/不可解析 ⇒ 拒绝保存
 * @returns {Promise<boolean>}
 */
async function writeToken(token, origin) {
  const value = String(token ?? '')
  if (!value) return removeStoredToken()
  let envelope
  try {
    envelope = tokenOrigin.encodeEnvelope({ token: value, origin })
  } catch {
    // 没有可信 origin：宁可不记住登录，也不存一份说不清归属的云端凭据（H79）
    return false
  }
  try {
    if (!(await safeStorage.isAsyncEncryptionAvailable())) return false
    const file = tokenPath()
    await fs.promises.mkdir(path.dirname(file), { recursive: true })
    const encrypted = await safeStorage.encryptStringAsync(envelope)
    await fs.promises.writeFile(file, encrypted, { mode: 0o600 })
    return true
  } catch {
    return false
  }
}

async function clearToken() {
  return removeStoredToken()
}

module.exports = { tokenPath, readToken, readTokenResult, writeToken, clearToken }
