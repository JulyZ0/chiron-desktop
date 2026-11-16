'use strict'
/**
 * `two-factor.cjs` 的契约测试 —— 纯 Node，mock safeStorage + 临时目录。
 *
 * 钉住四条**安全相关**的不变量（都是"便宜实现会做错"的地方）：
 *
 *   1. **密钥不落明文**：磁盘上不能出现 base32 密钥或恢复码原文；
 *   2. **同一个 TOTP 不能用两次**（30 秒窗口内重放必须被拒）；
 *   3. **恢复码一次性**，且只存哈希；
 *   4. **未启用时验证放行**（skipped），不能因为没开 2FA 就拒绝解锁。
 *
 * 另有一条**失效安全性**：密文损坏时按"未启用"处理，而不是"永久锁死"。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const totp = require('../totp.cjs')
const twoFactor = require('../two-factor.cjs')

/** XOR 假加密：往返可逆，且密文里不含明文子串 */
const XOR_MASK = 0x5a
function fakeEncrypt(text) {
  const body = Buffer.from(text, 'utf8')
  for (let i = 0; i < body.length; i += 1) body[i] ^= XOR_MASK
  return Buffer.concat([Buffer.from('enc::', 'utf8'), body])
}
function fakeDecrypt(buffer) {
  const body = Buffer.from(buffer.subarray(5))
  for (let i = 0; i < body.length; i += 1) body[i] ^= XOR_MASK
  return body.toString('utf8')
}

function fakeSafeStorage(available = true) {
  return {
    isEncryptionAvailable: () => available,
    encryptString: fakeEncrypt,
    decryptString: fakeDecrypt,
  }
}

function bind(t, { available = true } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-2fa-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const safeStorage = fakeSafeStorage(available)
  twoFactor._setDeps({ userDataPath: dir, safeStorage: () => safeStorage })
  t.after(() => twoFactor._resetDeps())
  return { dir, file: path.join(dir, twoFactor.FILE) }
}

/** 走完"开始 → 确认"，返回密钥与恢复码 */
async function enroll(account = 'test@chiron.local') {
  const began = twoFactor.beginEnrollment({ account })
  const nowSeconds = Math.floor(Date.now() / 1000)
  const token = totp.totpAt(began.secret, nowSeconds)
  const confirmed = await twoFactor.confirmEnrollment(token, { account })
  assert.equal(confirmed.ok, true, `确认启用失败: ${JSON.stringify(confirmed)}`)
  return { secret: began.secret, recoveryCodes: confirmed.recoveryCodes, nowSeconds }
}

test('未启用时 verify 直接放行（skipped），不会因为没开 2FA 就拒绝解锁', async t => {
  bind(t)
  const result = await twoFactor.verify('000000')
  assert.equal(result.ok, true)
  assert.equal(result.skipped, true)
})

test('★★ 密钥与恢复码**不落明文**（磁盘上只有密文）', async t => {
  const { file } = bind(t)
  const { secret, recoveryCodes } = await enroll()

  const raw = fs.readFileSync(file)
  const asText = raw.toString('utf8')
  assert.ok(raw.length > 0, '配置应已落盘')
  assert.equal(asText.includes(secret), false, '磁盘上出现了 TOTP 密钥明文')
  for (const code of recoveryCodes) {
    const compact = code.replace(/-/g, '')
    assert.equal(asText.includes(code), false, `磁盘上出现恢复码原文: ${code}`)
    assert.equal(asText.includes(compact), false, `磁盘上出现恢复码紧凑形态: ${compact}`)
  }
})

test('★ 启用后 status 不含密钥，只报 enabled / 恢复码剩余', async t => {
  bind(t)
  const { recoveryCodes } = await enroll()

  const status = await twoFactor.getStatus()
  assert.equal(status.enabled, true)
  assert.equal(status.recoveryCodesLeft, recoveryCodes.length)
  // 类型层面就不该有 secret 这个键
  assert.equal('secret' in status, false, 'getStatus 绝不能回传密钥')
  assert.ok(status.enabledAt)
})

test('★★ 同一个 TOTP 不能用两次（窗口内重放必须被拒）', async t => {
  bind(t)
  // 直接构造一个"刚启用"的状态：启用时那个码已被记为已用
  const began = twoFactor.beginEnrollment({ account: 'a' })
  const now = Math.floor(Date.now() / 1000)
  const token = totp.totpAt(began.secret, now)
  const confirmed = await twoFactor.confirmEnrollment(token, { account: 'a' })
  assert.equal(confirmed.ok, true)

  // 用同一个码再验一次：启用时已记账，必须判为 replayed
  const replay = await twoFactor.verify(token)
  assert.equal(replay.ok, false, '同一验证码被接受了 —— 旁人瞥一眼就能重放')
  assert.equal(replay.reason, 'replayed')
})

test('★ 下一个时间步的验证码可以正常通过', async t => {
  bind(t)
  const began = twoFactor.beginEnrollment({ account: 'a' })
  const now = Math.floor(Date.now() / 1000)
  await twoFactor.confirmEnrollment(totp.totpAt(began.secret, now), { account: 'a' })

  // 下一个窗口的码：+30s 后仍落在 ±1 容差内，且时间步严格更大
  const nextToken = totp.totpAt(began.secret, now + totp.STEP_SECONDS)
  const result = await twoFactor.verify(nextToken)
  assert.equal(result.ok, true, `下一个窗口的码应通过: ${JSON.stringify(result)}`)
  assert.equal(result.via, 'totp')
})

test('★★ 恢复码一次性：用一次后同一个码失效', async t => {
  bind(t)
  const { recoveryCodes } = await enroll()
  const code = recoveryCodes[0]

  const first = await twoFactor.verify(code)
  assert.equal(first.ok, true)
  assert.equal(first.via, 'recovery')
  assert.equal(first.recoveryCodesLeft, recoveryCodes.length - 1)

  const second = await twoFactor.verify(code)
  assert.equal(second.ok, false, '恢复码被重复使用 —— 它必须是一次性的')
})

test('恢复码大小写 / 连字符不敏感（用户抄写的形态各异）', async t => {
  bind(t)
  const { recoveryCodes } = await enroll()
  const code = recoveryCodes[0]
  const messy = code.toLowerCase().replace('-', ' ')

  const result = await twoFactor.verify(messy)
  assert.equal(result.ok, true, `宽容形态应通过: ${JSON.stringify(result)}`)
})

test('错误验证码被拒，且不影响已经启用的事实', async t => {
  bind(t)
  await enroll()
  const result = await twoFactor.verify('000000')
  assert.equal(result.ok, false)
  assert.equal((await twoFactor.getStatus()).enabled, true)
})

test('★ 启用必须验证一次：确认时码错则不启用', async t => {
  bind(t)
  twoFactor.beginEnrollment({ account: 'a' })
  const result = await twoFactor.confirmEnrollment('000000', { account: 'a' })
  assert.equal(result.ok, false)
  assert.equal((await twoFactor.getStatus()).enabled, false, '校验失败却启用了 2FA')
})

test('没有候选密钥时确认返回 no-enrollment（不是静默启用）', async t => {
  bind(t)
  const result = await twoFactor.confirmEnrollment('123456', { account: 'nobody' })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'no-enrollment')
})

test('重新生成恢复码：旧的立即失效，新的可用', async t => {
  bind(t)
  const { recoveryCodes: oldCodes } = await enroll()

  const regenerated = await twoFactor.regenerateRecoveryCodes()
  assert.equal(regenerated.ok, true)
  assert.equal(regenerated.recoveryCodes.length, oldCodes.length)

  const oldTry = await twoFactor.verify(oldCodes[0])
  assert.equal(oldTry.ok, false, '旧恢复码仍可用 —— 重新生成没有作废旧码')

  const newTry = await twoFactor.verify(regenerated.recoveryCodes[0])
  assert.equal(newTry.ok, true)
})

test('未启用时不能重新生成恢复码', async t => {
  bind(t)
  const result = await twoFactor.regenerateRecoveryCodes()
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'not-enabled')
})

test('disable 之后回到未启用，且文件被删除', async t => {
  const { file } = bind(t)
  await enroll()
  assert.ok(fs.existsSync(file), '启用后应有配置文件')

  const result = await twoFactor.disable()
  assert.equal(result.ok, true)
  assert.equal(fs.existsSync(file), false, '禁用后配置文件应被删除')
  assert.equal((await twoFactor.getStatus()).enabled, false)
  // 未启用 ⇒ verify 恢复放行
  assert.equal((await twoFactor.verify('000000')).skipped, true)
})

test('safeStorage 不可用时不落任何文件（绝不退化成明文）', async t => {
  const { file } = bind(t, { available: false })
  const began = twoFactor.beginEnrollment({ account: 'a' })
  const token = totp.totpAt(began.secret, Math.floor(Date.now() / 1000))
  const result = await twoFactor.confirmEnrollment(token, { account: 'a' })

  assert.equal(result.ok, false)
  assert.equal(result.reason, 'write-failed')
  assert.equal(fs.existsSync(file), false, '加密不可用时不该留下任何文件')
})

test('★ 密文损坏时按"未启用"处理，并标记 unreadable（不是永久锁死）', async t => {
  const { file } = bind(t)
  await enroll()
  // 破坏密文：解密会抛，readState 走 catch 分支
  fs.writeFileSync(file, Buffer.from('not-a-valid-blob'))

  const status = await twoFactor.getStatus()
  assert.equal(status.enabled, false, '密文损坏时不应声称已启用（否则用户永远进不来）')
  assert.equal(status.unreadable, true, '必须让界面知道配置读不出来，而不是假装从未启用')
  assert.equal((await twoFactor.verify('000000')).skipped, true)
})

test('normalizeState 丢弃脏数据：非法恢复码哈希被剔除', () => {
  const state = twoFactor.normalizeState({
    enabled: true,
    secret: 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
    lastUsedStep: 123,
    recoveryCodes: [
      { hash: 'not-hex', usedAt: '' },
      { hash: 'a'.repeat(64), usedAt: '' },
      { hash: 'b'.repeat(64), usedAt: '2026-01-01T00:00:00Z' },
      'garbage',
    ],
  })
  assert.equal(state.recoveryCodes.length, 2, '非法哈希与非对象条目应被剔除')
  assert.equal(state.lastUsedStep, 123)
})

test('★ 声称 enabled 但没有密钥 ⇒ 按未启用处理（否则解锁永远算不出码）', () => {
  const state = twoFactor.normalizeState({ enabled: true, secret: '' })
  assert.equal(state.enabled, false)
})

test('恢复码形态：8 位、带一个连字符、无易混字符', () => {
  for (let i = 0; i < 50; i += 1) {
    const code = twoFactor.makeRecoveryCode()
    assert.match(code, /^[A-Z2-9]{4}-[A-Z2-9]{4}$/, `恢复码形态不对: ${code}`)
    const compact = code.replace('-', '')
    assert.equal(/[01IOL]/.test(compact), false, `含易混字符: ${code}`)
  }
})
