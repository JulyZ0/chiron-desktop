'use strict'
/**
 * TOTP（RFC 6238）与 base32 的契约测试 —— **纯 Node**，不起 Electron。
 *
 * 为什么这组测试必须用**官方向量**而不是"自己算一遍再比对"：
 * 自己算的对拍只能证明"代码前后一致"，证明不了"与 authenticator app 一致"。
 * 一旦截断方式或字节序写错，实现与主流 app 就会一直对不上 —— 而用户在界面上
 * 只会看到"验证码错误"，无从判断是 app 的问题还是我们的问题。
 *
 * RFC 6238 附录 B 的向量是 SHA-1 + 20 字节种子 `12345678901234567890`
 * （base32：`GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ`）。官方向量给 8 位，
 * 我们的实现是 6 位（取后 6 位），所以断言里只比后 6 位。
 */
const test = require('node:test')
const assert = require('node:assert/strict')

const totp = require('../totp.cjs')

const RFC_SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'

test('base32 编解码往返：任意字节串都能还原', () => {
  const cases = [
    Buffer.from('12345678901234567890', 'ascii'),
    Buffer.from([0x00, 0xff, 0x10, 0x7f]),
    Buffer.from('chiron', 'utf8'),
  ]
  for (const raw of cases) {
    const encoded = totp.base32Encode(raw)
    assert.match(encoded, /^[A-Z2-7]+$/, `base32 输出含非法字符: ${encoded}`)
    assert.deepEqual(totp.base32Decode(encoded), raw)
  }
})

test('base32 解码宽容处理用户粘贴形态（小写 / 空格 / 连字符 / padding）', () => {
  const expected = Buffer.from('12345678901234567890', 'ascii')
  for (const variant of [
    'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
    'gezdgnbvgy3tqojqgezdgnbvgy3tqojq',
    'GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ',
    'GEZD-GNBV-GY3T-QOJQ-GEZD-GNBV-GY3T-QOJQ',
  ]) {
    assert.deepEqual(totp.base32Decode(variant), expected, `形态未接受: ${variant}`)
  }
})

test('base32 解码遇到非法字符返回 null（不猜）', () => {
  assert.equal(totp.base32Decode('GEZD1NBV'), null, '数字 1 不在 base32 字母表里')
  assert.equal(totp.base32Decode(''), null)
})

test('★★ RFC 6238 官方测试向量（SHA-1，20 字节种子）', () => {
  // 时刻 → 官方 8 位码；我们的 6 位实现应等于其末 6 位
  const vectors = [
    [59, '94287082'],
    [1111111109, '07081804'],
    [1111111111, '14050471'],
    [1234567890, '89005924'],
    [2000000000, '69279037'],
    [20000000000, '65353130'],
  ]
  for (const [seconds, expected8] of vectors) {
    const got = totp.totpAt(RFC_SECRET, seconds)
    assert.equal(got, expected8.slice(-6), `t=${seconds} 与 RFC 向量不一致（got=${got}）`)
    assert.equal(got.length, 6)
  }
})

test('★★ 验证接受 ±1 个时间步的时钟偏差（RFC 6238 §5.2）', () => {
  const now = 1_700_000_000
  const current = totp.totpAt(RFC_SECRET, now)
  const previous = totp.totpAt(RFC_SECRET, now - totp.STEP_SECONDS)
  const next = totp.totpAt(RFC_SECRET, now + totp.STEP_SECONDS)

  for (const [label, token] of [
    ['当前窗口', current],
    ['前一窗口', previous],
    ['后一窗口', next],
  ]) {
    const result = totp.verifyTotp(RFC_SECRET, token, { nowSeconds: now })
    assert.equal(result.ok, true, `${label} 应当通过（时钟偏差）`)
  }
})

test('★ 偏差超过一个时间步必须拒绝（放宽窗口 = 放宽第二因素）', () => {
  const now = 1_700_000_000
  for (const offset of [2, 3, -2, -3]) {
    const token = totp.totpAt(RFC_SECRET, now + offset * totp.STEP_SECONDS)
    const result = totp.verifyTotp(RFC_SECRET, token, { nowSeconds: now })
    assert.equal(result.ok, false, `偏差 ${offset} 步不该通过`)
  }
})

test('verifyTotp 返回命中的时间步（供"一次性使用"约束）', () => {
  const now = 1_700_000_000
  const currentStep = Math.floor(now / totp.STEP_SECONDS)
  const token = totp.totpAt(RFC_SECRET, now)
  const result = totp.verifyTotp(RFC_SECRET, token, { nowSeconds: now })
  assert.equal(result.ok, true)
  assert.equal(result.matchedStep, currentStep)
})

test('非 6 位数字一律拒绝（含空串、字母、超长）', () => {
  const now = 1_700_000_000
  for (const bad of ['', '12345', '1234567', 'abcdef', '12 34 56 7']) {
    const result = totp.verifyTotp(RFC_SECRET, bad, { nowSeconds: now })
    assert.equal(result.ok, false, `不该接受: ${JSON.stringify(bad)}`)
  }
})

test('密钥非法时明确返回 invalid-secret（而不是当成"验证码错"）', () => {
  const result = totp.verifyTotp('not-base32!', '123456', { nowSeconds: 1_700_000_000 })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'invalid-secret')
})

test('生成的新密钥是 20 字节 base32、可直接用于验证', () => {
  const secret = totp.generateSecret()
  assert.match(secret, /^[A-Z2-7]{32}$/, `20 字节应是 32 个 base32 字符，实际 ${secret}`)
  const decoded = totp.base32Decode(secret)
  assert.equal(decoded.length, 20)

  const now = 1_700_000_000
  const token = totp.totpAt(secret, now)
  assert.equal(totp.verifyTotp(secret, token, { nowSeconds: now }).ok, true)
})

test('otpauth URI 结构正确：标签与参数都被正确编码', () => {
  const uri = totp.otpauthUri({
    issuer: 'Chiron',
    account: 'owner@chiron.local',
    secret: RFC_SECRET,
  })
  assert.ok(uri.startsWith('otpauth://totp/'), uri)
  // `@` 与 `:` 必须编码，否则 URI 结构被破坏
  assert.ok(uri.includes('Chiron%3Aowner%40chiron.local'), `标签编码不对: ${uri}`)

  const params = new URL(uri).searchParams
  assert.equal(params.get('secret'), RFC_SECRET)
  assert.equal(params.get('issuer'), 'Chiron')
  assert.equal(params.get('algorithm'), 'SHA1')
  assert.equal(params.get('digits'), '6')
  assert.equal(params.get('period'), '30')
})
