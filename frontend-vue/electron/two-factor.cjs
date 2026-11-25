'use strict'
/**
 * 可选的第二因素（TOTP）—— 存储、启用/停用、验证、恢复码。
 *
 * ## 为什么是"可选"
 *
 * 本地模式的第一因素已经是 OS 账户（坐到机器前 = 账户主人），第二因素的价值是
 * **防"人走开、屏幕没锁"**。这对共用工作站很有用，但对单人独占的家用机是纯摩擦
 * （每次解锁都要掏手机）。所以它默认关闭，由用户显式开启。
 *
 * ## 密钥怎么存
 *
 * 与 provider 密钥同一套：`safeStorage`（Windows DPAPI）加密后落
 * `userData/two-factor.bin`，绝不落明文、绝不进日志、绝不回传给渲染进程。
 * 加密原语**复用 `secrets.cjs`**，不另写一份 —— 加密实现只有一份才可能只有一套
 * 约束（不可用时不落明文、先写临时文件再 rename）。
 *
 * ## 三条不能用便宜实现换掉的约束
 *
 * 1. **同一个验证码不能用两次**。TOTP 在 30 秒内是常量；不做一次性约束的话，
 *    旁人瞥到一眼就能在窗口内重放。这里按"命中的时间步"记账（`lastUsedStep`），
 *    只接受**严格大于**它的时间步。
 * 2. **恢复码只存哈希**。它是绕过 TOTP 的等价凭据，明文落盘等于把第二因素
 *    降级成第一因素。用 sha256（不是密码，不需要慢哈希 —— 它有 10^10 量级熵）。
 * 3. **启用必须验证一次**。`beginEnrollment()` 只生成候选密钥，必须用户回填一个
 *    当前有效的验证码才 `confirmEnrollment()` —— 否则手机没配对成功就被锁在门外。
 *
 * ## 失败次数
 *
 * 不在这里记账。主进程的解锁路径每次都要**先验 OS 密码**（一次一密，不是暴力可
 * 猜的对象），而 TOTP 的 ±1 窗口已经把在线穷举压到 3 次/30 秒。再加一层本地计数
 * 只会带来"输错几次就永久锁死"的运维问题，却没有实质收益。
 */
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

const totp = require('./totp.cjs')
const secrets = require('./secrets.cjs')

const FILE = 'two-factor.bin'
const VERSION = 1

/** 恢复码数量。10 个够用且一次性 —— 多了会诱导用户随手存进不安全的笔记里。 */
const RECOVERY_CODE_COUNT = 10
/** 恢复码字符集：去掉易混的 0/O/1/I/L，避免用户抄错。 */
const RECOVERY_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
/** 每组字符数；`XXXX-XXXX` 是"可抄写"的常见形态。 */
const RECOVERY_GROUP = 4
const RECOVERY_GROUPS = 2

let injected = null

function deps() {
  if (injected) return injected
  return {
    get userDataPath() {
      const electron = require('electron')
      return electron.app.getPath('userData')
    },
    safeStorage: () => {
      const electron = require('electron')
      return electron.safeStorage
    },
  }
}

function filePath() {
  return path.join(deps().userDataPath, FILE)
}

/** 空状态：未启用。 */
function emptyState() {
  return {
    version: VERSION,
    enabled: false,
    secret: '',
    /** 已用掉的最大时间步：同一个码不能重放（见模块头第 1 条） */
    lastUsedStep: 0,
    /** `{ hash, usedAt }`；`usedAt` 非空表示已用掉 */
    recoveryCodes: [],
    enabledAt: '',
  }
}

/** 收敛形状：脏数据一律丢弃，绝不把非法字段透传出去 */
function normalizeState(raw) {
  const out = emptyState()
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out
  out.enabled = raw.enabled === true
  out.secret = typeof raw.secret === 'string' ? raw.secret.trim() : ''
  out.lastUsedStep = Number.isFinite(raw.lastUsedStep) ? Math.max(0, Math.trunc(raw.lastUsedStep)) : 0
  out.enabledAt = typeof raw.enabledAt === 'string' ? raw.enabledAt : ''
  const codes = Array.isArray(raw.recoveryCodes) ? raw.recoveryCodes : []
  out.recoveryCodes = codes
    .map(entry => {
      if (!entry || typeof entry !== 'object') return null
      const hash = typeof entry.hash === 'string' ? entry.hash.trim() : ''
      if (!/^[0-9a-f]{64}$/.test(hash)) return null
      const usedAt = typeof entry.usedAt === 'string' ? entry.usedAt : ''
      return { hash, usedAt }
    })
    .filter(Boolean)
  // 自洽性：声称 enabled 却没有密钥 = 坏状态，按未启用处理。
  // 否则解锁路径会要求一个永远算不出来的验证码，用户被彻底锁死。
  if (out.enabled && !out.secret) {
    out.enabled = false
    out.secret = ''
  }
  return out
}

async function readState() {
  try {
    const safeStorage = deps().safeStorage()
    if (!safeStorage || !(await secrets.encryptionAvailable(safeStorage))) return emptyState()
    let encrypted
    try {
      encrypted = await fs.promises.readFile(filePath())
    } catch {
      return emptyState()
    }
    if (!encrypted || encrypted.length === 0) return emptyState()
    const { text } = await secrets.decryptText(safeStorage, encrypted)
    return normalizeState(JSON.parse(text))
  } catch {
    // 解密失败 / JSON 坏掉：当作"没启用"。
    //
    // 这是一个**安全相关的取舍**：解密失败时若报"已启用"，用户永远进不来
    // （密钥没了，验证码算不出来）；若报"未启用"，等于第二因素被静默关掉。
    // 选后者，因为本机的第一因素（OS 账户）始终有效，而"永久锁死"是不可接受的
    // 失效模式。代价是密文被破坏时第二因素降级 —— 这一点写在界面的状态里：
    // `state` 返回 `unreadable: true` 让用户知道"2FA 配置读不出来"。
    return { ...emptyState(), unreadable: true }
  }
}

async function writeState(state) {
  try {
    const safeStorage = deps().safeStorage()
    if (!safeStorage || !(await secrets.encryptionAvailable(safeStorage))) return false
    const payload = JSON.stringify(normalizeState(state))
    const file = filePath()
    await fs.promises.mkdir(path.dirname(file), { recursive: true })
    const encrypted = await secrets.encryptText(safeStorage, payload)
    if (!encrypted || encrypted.length === 0) return false
    // 先写临时文件再 rename：中途崩掉不会留下半截密文（同 secrets.cjs）
    const tmp = `${file}.tmp`
    await fs.promises.writeFile(tmp, encrypted, { mode: 0o600 })
    await fs.promises.rename(tmp, file)
    return true
  } catch {
    return false
  }
}

/** 恢复码明文（只在启用那一刻返回一次）→ `XXXX-XXXX` */
function makeRecoveryCode() {
  let body = ''
  for (let i = 0; i < RECOVERY_GROUP * RECOVERY_GROUPS; i += 1) {
    body += RECOVERY_ALPHABET[crypto.randomInt(RECOVERY_ALPHABET.length)]
  }
  return `${body.slice(0, RECOVERY_GROUP)}-${body.slice(RECOVERY_GROUP)}`
}

function hashCode(code) {
  // 归一化后再哈希：用户抄回来时大小写、空格、连字符都可能变
  const normalized = String(code || '')
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
  return crypto.createHash('sha256').update(normalized, 'utf8').digest('hex')
}

/** 对外状态：**绝不包含 secret** */
function publicStatus(state) {
  return {
    enabled: state.enabled === true,
    enabledAt: state.enabledAt || '',
    /** 剩余可用恢复码数量 —— 归零时界面要提醒用户重新生成 */
    recoveryCodesLeft: state.recoveryCodes.filter(c => !c.usedAt).length,
    /** 配置读不出来（密文损坏 / 密钥环换了）：界面据此提示而不是假装"未启用" */
    unreadable: state.unreadable === true,
  }
}

async function getStatus() {
  return publicStatus(await readState())
}

/**
 * 开始启用：生成候选密钥与 otpauth URI，但**不落 enabled**。
 *
 * 候选密钥暂存在内存（`enrollments`），不写盘 —— 用户不确认就等于什么都没发生。
 * 用内存而不是写 `pendingSecret` 到磁盘，是因为"半启用的状态"是纯粹的麻烦：
 * 崩在中间时磁盘上会留下一份既没生效、又含真实密钥的配置。
 */
const enrollments = new Map()

function accountKey(account) {
  return String(account || 'local')
}

function beginEnrollment(options = {}) {
  const account = accountKey(options.account)
  const secret = totp.generateSecret()
  enrollments.set(account, secret)
  return {
    secret, // 给界面显示/手动输入用；它本来就是"待用户扫码"的东西
    otpauth: totp.otpauthUri({
      issuer: options.issuer || 'Chiron',
      account,
      secret,
    }),
  }
}

/**
 * 确认启用：验证用户回填的验证码，通过后才写 `enabled=true` 并生成恢复码。
 *
 * 返回**明文恢复码** —— 这是唯一一次，之后库里只有哈希。
 */
async function confirmEnrollment(token, options = {}) {
  const account = accountKey(options.account)
  const secret = enrollments.get(account)
  if (!secret) return { ok: false, reason: 'no-enrollment' }

  const check = totp.verifyTotp(secret, token)
  if (!check.ok) {
    // 码错**不**清掉候选密钥：用户多半是手慢错过窗口，重新输一次即可。
    return { ok: false, reason: check.reason || 'mismatch' }
  }

  const codes = Array.from({ length: RECOVERY_CODE_COUNT }, () => makeRecoveryCode())
  const state = emptyState()
  state.enabled = true
  state.secret = secret
  state.enabledAt = new Date().toISOString()
  // 刚验证过的这个时间步直接记为已用：否则同一码在窗口内还能再用一次
  state.lastUsedStep = check.matchedStep || 0
  state.recoveryCodes = codes.map(code => ({ hash: hashCode(code), usedAt: '' }))

  if (!(await writeState(state))) {
    return { ok: false, reason: 'write-failed' }
  }
  enrollments.delete(account)
  return { ok: true, recoveryCodes: codes }
}

async function disable() {
  enrollments.clear()
  try {
    await fs.promises.rm(filePath(), { force: true })
    return { ok: true }
  } catch {
    return { ok: false, reason: 'delete-failed' }
  }
}

/**
 * 验证第二因素（解锁时用）。
 *
 * 两条合法输入：
 *   · 6 位 TOTP —— 必须严格晚于 `lastUsedStep`（防同一窗口内重放）；
 *   · 恢复码 —— 命中即标记为已用（一次性）。
 *
 * 未启用时直接放行（`{ ok:true, skipped:true }`）：调用方按"没有第二因素"处理，
 * 不要因为用户没开 2FA 就拒绝解锁。
 */
/**
 * 显式重置第二因素（H48）。
 *
 * 使用场景只有一个：**密文不可读**。此时 `getStatus()` 返回 `unreadable: true`，
 * 解锁会被拒绝（fail-closed），用户必须自己确认"我知道 2FA 用不了了，重置它"。
 *
 * 与 `disable()` 的区别：`disable()` 需要用户输入密码且要求状态可读；
 * `reset()` 恰恰是在状态不可读时才用得上 —— 它删掉那份坏密文并清空状态，
 * 让用户重新注册。**它不返回任何秘密**，也不能用来绕过已启用且可读的 2FA。
 */
async function reset() {
  const status = await getStatus()
  if (!status.unreadable) {
    // 状态可读时走正常的 disable（需要密码确认），不允许用"重置"绕过第二因素。
    return {
      ok: false,
      reason: 'not-unreadable',
      message: '2FA 状态正常，重置请走「停用」并输入密码',
    }
  }
  try {
    await fs.promises.rm(filePath(), { force: true })
  } catch (err) {
    return { ok: false, reason: 'reset-failed', message: (err && err.message) || String(err) }
  }
  return { ok: true, reason: 'reset', message: '已清除无法读取的 2FA 配置，请重新启用' }
}

async function verify(token) {
  const state = await readState()
  if (!state.enabled) return { ok: true, skipped: true }

  const raw = String(token || '').trim()
  if (!raw) return { ok: false, reason: 'needs-token' }

  // 恢复码：先试它。它的形状（含连字符 / 8 位字母数字）与 6 位纯数字不重叠。
  const asHash = hashCode(raw)
  const idx = state.recoveryCodes.findIndex(c => c.hash === asHash && !c.usedAt)
  if (idx >= 0) {
    state.recoveryCodes[idx] = { ...state.recoveryCodes[idx], usedAt: new Date().toISOString() }
    if (!(await writeState(state))) return { ok: false, reason: 'write-failed' }
    return { ok: true, via: 'recovery', recoveryCodesLeft: publicStatus(state).recoveryCodesLeft }
  }

  const check = totp.verifyTotp(state.secret, raw)
  if (!check.ok) return { ok: false, reason: check.reason || 'mismatch' }
  // 一次性：只接受严格更晚的时间步。±1 窗口内重放同一个码在这里被挡住。
  if (check.matchedStep !== undefined && check.matchedStep <= state.lastUsedStep) {
    return { ok: false, reason: 'replayed' }
  }
  state.lastUsedStep = check.matchedStep || state.lastUsedStep
  if (!(await writeState(state))) return { ok: false, reason: 'write-failed' }
  return { ok: true, via: 'totp' }
}

/** 重新生成恢复码（旧的立即失效）。需要已启用。 */
async function regenerateRecoveryCodes() {
  const state = await readState()
  if (!state.enabled) return { ok: false, reason: 'not-enabled' }
  const codes = Array.from({ length: RECOVERY_CODE_COUNT }, () => makeRecoveryCode())
  state.recoveryCodes = codes.map(code => ({ hash: hashCode(code), usedAt: '' }))
  if (!(await writeState(state))) return { ok: false, reason: 'write-failed' }
  return { ok: true, recoveryCodes: codes }
}

function _setDeps(next) {
  injected = next || null
}

function _resetDeps() {
  injected = null
  enrollments.clear()
}

module.exports = {
  reset,
  FILE,
  VERSION,
  RECOVERY_CODE_COUNT,
  emptyState,
  normalizeState,
  publicStatus,
  getStatus,
  beginEnrollment,
  confirmEnrollment,
  disable,
  verify,
  regenerateRecoveryCodes,
  makeRecoveryCode,
  hashCode,
  _setDeps,
  _resetDeps,
}
