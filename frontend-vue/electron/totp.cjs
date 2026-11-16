'use strict'
/**
 * TOTP（RFC 6238）与 base32（RFC 4648）—— 纯函数，不依赖 Electron、不碰磁盘。
 *
 * ## 为什么自己实现，而不是加一个依赖
 *
 * `otplib` / `speakeasy` 都是几十 KB 的依赖，而 TOTP 本身是
 *
 *     HOTP = HMAC-SHA1(key, counter) 的动态截断
 *     counter = floor(unixSeconds / 30)
 *
 * 十几行就能写完，且 `node:crypto` 已提供 HMAC。多一个依赖意味着多一条供应链，
 * 而这个模块处理的是**第二因素密钥**——恰恰是最不该引入外部代码的地方。
 *
 * ## 为什么验证要允许 ±1 个窗口
 *
 * 客户端与服务器的时钟不可能严格对齐（手机快/慢十几秒是常态）。RFC 6238 §5.2
 * 明确建议容许一个时间步的偏差。只认当前窗口会让一部分用户在正确输入时被告知
 * "验证码错误"，而他们唯一的出路是去校时——比放行一格风险大得多（一格 = 30 秒，
 * 且验证码一次性使用、试错次数有上限）。
 *
 * 反向约束：**不能放宽到 ±2 以上**。窗口越宽，被穷举的机会越大（6 位码 100 万种，
 * 每多一个窗口就多一次合法尝试）。这里固定 ±1，并靠调用方的失败计数兜底。
 */
const crypto = require('node:crypto')

/** 时间步长（秒）。30 是各家 authenticator app 的共识默认值。 */
const STEP_SECONDS = 30
/** 输出位数。6 位是标准；8 位虽然更强，但多数 app 默认只显示/接受 6 位。 */
const DIGITS = 6
/**
 * 允许的时钟偏差（单位：时间步）。`1` = 前一个 / 当前 / 后一个窗口。
 *
 * 写死而不是做成配置项：放宽它就是在放宽第二因素，不该是一个"顺手调大"的旋钮。
 */
const ALLOWED_DRIFT_STEPS = 1

const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

/**
 * 生成一个新的 TOTP 密钥（base32，无 padding）。
 *
 * 20 字节 = HMAC-SHA1 的块长，也是 RFC 4226 §4 建议的最小长度（160 bit）。
 * 更短会削弱安全性；更长对 SHA-1 没有额外收益。
 */
function generateSecret() {
  return base32Encode(crypto.randomBytes(20))
}

/** 字节 → base32（RFC 4648，大写、无 padding —— authenticator app 都认这个形态） */
function base32Encode(buffer) {
  const bytes = Buffer.from(buffer)
  let bits = 0
  let value = 0
  let out = ''
  for (const byte of bytes) {
    value = (value << 8) | byte
    bits += 8
    while (bits >= 5) {
      out += BASE32_ALPHABET[(value >>> (bits - 5)) & 31]
      bits -= 5
    }
  }
  if (bits > 0) out += BASE32_ALPHABET[(value << (5 - bits)) & 31]
  return out
}

/**
 * base32 → 字节。宽容处理用户从别处粘来的形态：
 * 小写、空格、`-`、`=` padding 都接受；非法字符返回 null（调用方按"密钥无效"处理）。
 *
 * 为什么要宽容：密钥经常是用户手动输入或从二维码/短信里复制来的，里面带空格或
 * 换行是常态。严格拒绝只会让人对着一个"看起来正确"的密钥反复失败。
 */
function base32Decode(input) {
  const text = String(input || '')
    .toUpperCase()
    .replace(/[\s\-_=]/g, '')
  if (!text) return null
  let bits = 0
  let value = 0
  const out = []
  for (const ch of text) {
    const idx = BASE32_ALPHABET.indexOf(ch)
    if (idx === -1) return null
    value = (value << 5) | idx
    bits += 5
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff)
      bits -= 8
    }
  }
  return out.length ? Buffer.from(out) : null
}

/** HOTP：HMAC-SHA1 + RFC 4226 §5.3 动态截断 */
function hotp(keyBuffer, counter) {
  const buf = Buffer.alloc(8)
  // 计数器是 64 位大端。用 writeBigUInt64BE 直接写，避免手工拆高低位。
  buf.writeBigUInt64BE(BigInt(counter))
  const digest = crypto.createHmac('sha1', keyBuffer).update(buf).digest()
  const offset = digest[digest.length - 1] & 0x0f
  const binary =
    ((digest[offset] & 0x7f) << 24) |
    ((digest[offset + 1] & 0xff) << 16) |
    ((digest[offset + 2] & 0xff) << 8) |
    (digest[offset + 3] & 0xff)
  return String(binary % 10 ** DIGITS).padStart(DIGITS, '0')
}

/** 给定时钟（秒）算 TOTP。`nowSeconds` 显式传入便于单测钉住向量。 */
function totpAt(secret, nowSeconds) {
  const key = base32Decode(secret)
  if (!key) return ''
  const counter = Math.floor(Number(nowSeconds) / STEP_SECONDS)
  return hotp(key, counter)
}

/**
 * 校验用户输入的验证码。
 *
 * @returns {{ok: boolean, reason?: string, matchedStep?: number}}
 *   `matchedStep` 为命中的时间步：调用方据此做**一次性**约束（同一个码不能用两次），
 *   否则用户可以在 30 秒内反复提交同一个正确码。
 */
function verifyTotp(secret, token, options = {}) {
  const key = base32Decode(secret)
  if (!key) return { ok: false, reason: 'invalid-secret' }
  const digits = String(token || '').replace(/\s/g, '')
  if (!new RegExp(`^\\d{${DIGITS}}$`).test(digits)) return { ok: false, reason: 'invalid-token' }

  const nowSeconds = Number(options.nowSeconds ?? Math.floor(Date.now() / 1000))
  const currentStep = Math.floor(nowSeconds / STEP_SECONDS)
  for (let drift = -ALLOWED_DRIFT_STEPS; drift <= ALLOWED_DRIFT_STEPS; drift += 1) {
    const step = currentStep + drift
    if (step < 0) continue
    const expected = hotp(key, step)
    // 定长比较：避免按字节提前返回带来的时序差（虽然本地场景收益有限，
    // 但这段代码的成本为零，没有理由留一个可被观察的分支）。
    if (crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(digits))) {
      return { ok: true, matchedStep: step }
    }
  }
  return { ok: false, reason: 'mismatch' }
}

/**
 * `otpauth://` URI —— authenticator app 扫码用。
 *
 * 标签按 RFC 惯例写成 `Issuer:Account`：多数 app 会把它拆成"发行方 + 账户"两栏，
 * 只写账户时用户面对一堆条目不知道哪个是哪个。两个字段都要 encodeURIComponent，
 * 邮箱里的 `@` 与冒号会破坏 URI 结构。
 */
function otpauthUri(options = {}) {
  const issuer = String(options.issuer || 'Chiron')
  const account = String(options.account || 'local')
  const secret = String(options.secret || '')
  const params = new URLSearchParams({
    secret,
    issuer,
    algorithm: 'SHA1',
    digits: String(DIGITS),
    period: String(STEP_SECONDS),
  })
  return `otpauth://totp/${encodeURIComponent(`${issuer}:${account}`)}?${params.toString()}`
}

module.exports = {
  STEP_SECONDS,
  DIGITS,
  ALLOWED_DRIFT_STEPS,
  generateSecret,
  base32Encode,
  base32Decode,
  hotp,
  totpAt,
  verifyTotp,
  otpauthUri,
}
