'use strict'
/**
 * H48：敏感操作与第二因素必须 fail-closed（PRD 9.2）。
 *
 * 缺陷形态：
 *   1. **非 Windows 上"清空本机数据"完全免验证**：`confirmSensitiveAction` 在
 *      `process.platform !== 'win32'` 时直接 `{ok:true, verified}` —— 同一份界面文案
 *      却写着"会再次验证身份"。不可逆动作没有第二道确认，还不能说没发生过。
 *   2. **2FA 密文不可读时按"未启用"放行**：只记一条 warning，然后解锁成功 ——
 *      第二因素被**静默关闭**，用户永远不会知道。
 *
 * 修法：两处都改成如实拒绝，并为 2FA 提供**显式的**重置路径
 * （`two-factor.reset()`），避免把"静默关闭"换成"永久锁死"。
 */
const test = require('node:test')
const assert = require('node:assert')
const path = require('node:path')
const fs = require('node:fs')

const ROOT = path.resolve(__dirname, '..')

function readMain() {
  return fs.readFileSync(path.join(ROOT, 'main.cjs'), 'utf8')
}

test('非 Windows 不再"免验证通过"', () => {
  const src = readMain()
  // 旧形态：非 win32 直接 return { ok: true, reason: 'verified' }
  const narrowed = src.slice(src.indexOf('async function confirmSensitiveAction'), src.indexOf('async function confirmSensitiveAction') + 3000)
  assert.ok(
    !/if \(process\.platform !== 'win32'\) \{\s*\n\s*\/\/[^\n]*\n\s*return \{ ok: true, reason: 'verified'/.test(narrowed),
    '非 Windows 分支不得直接返回 verified',
  )
  assert.ok(
    narrowed.includes('verification-unavailable') || narrowed.includes('process.platform'),
    '要保留平台判断',
  )
  const nonWin = narrowed.slice(narrowed.indexOf("process.platform !== 'win32'"))
  assert.ok(nonWin.includes('ok: false'), '非 Windows 应返回 ok:false（拒绝破坏性操作）')
})

test('2FA 不可读改为拒绝解锁，并给出可恢复原因', () => {
  const src = readMain()
  const i = src.indexOf('status.unreadable')
  assert.ok(i > 0, '应有 unreadable 分支')
  const block = src.slice(i, i + 900)
  assert.ok(block.includes("reason: 'second-factor-unreadable'"), '必须返回明确原因')
  assert.ok(block.includes("recoverable: 'reset-two-factor'"), '必须给出恢复路径')
  assert.ok(/ok: false/.test(block), '不可读时必须拒绝解锁')
  // 收窄到**旧的那行日志**：新注释里会解释"旧实现静默关闭"，不能误判。
  assert.ok(
    !block.includes("logMain('warn', '2FA 配置不可读，按未启用放行')"),
    '不得再"按未启用放行"',
  )
})

test('two-factor.reset：只有在不可读时才允许', () => {
  const src = fs.readFileSync(path.join(ROOT, 'two-factor.cjs'), 'utf8')
  assert.ok(src.includes('async function reset()'), '缺少 reset()')
  assert.ok(src.includes("reason: 'not-unreadable'"), '状态可读时必须拒绝（不能绕过第二因素）')
  assert.ok(src.includes('module.exports') && src.includes('reset,'), 'reset 必须导出')
})

test('主进程暴露 reset IPC，且经过 sender 校验', () => {
  const src = readMain()
  const i = src.indexOf("ipcMain.handle('desktop:reset-two-factor'")
  assert.ok(i > 0, '缺少 reset IPC')
  const block = src.slice(i, i + 400)
  assert.ok(block.includes('assertTrustedSender(event)'), 'IPC 必须校验来源')
  assert.ok(block.includes('twoFactor.reset()'), 'IPC 应调用模块 reset')
})

test('reset 语义：可读时拒绝、不可读时删掉坏密文（用桩验证行为）', async () => {
  const os = require('node:os')
  const twoFactor = require('../two-factor.cjs')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-h48-'))

  // 伪造一个"可读且已启用"的状态
  const safeStorage = {
    isEncryptionAvailable: async () => true,
    encryptStringAsync: async (text) => ({ encrypted: Buffer.from(text, 'utf8') }),
    decryptStringAsync: async (buf) => ({ result: buf.toString('utf8'), shouldReEncrypt: false }),
  }
  // deps() 直接返回注入对象本身：需要 userDataPath 与 safeStorage 两项
  // （fs 走模块级 require，无需注入）。
  twoFactor._setDeps({
    userDataPath: dir,
    safeStorage: () => safeStorage,
  })
  try {
    // 先启用（走真实流程会要 TOTP，这里直接写文件模拟已启用）
    const stateFile = path.join(dir, 'two-factor.bin')
    fs.writeFileSync(
      stateFile,
      JSON.stringify({ enabled: true, secret: 'JBSWY3DPEHPK3PXP', recovery: [], account: 'a' }),
      'utf8',
    )
    const blocked = await twoFactor.reset()
    assert.equal(blocked.ok, false)
    assert.equal(blocked.reason, 'not-unreadable')
    assert.ok(fs.existsSync(stateFile), '可读状态下不得删除配置')

    // 再造一个"不可读"的密文（不是合法 JSON）
    fs.writeFileSync(stateFile, 'not-json-at-all', 'utf8')
    const status = await twoFactor.getStatus()
    assert.equal(status.unreadable, true, '坏密文应报告 unreadable')

    const done = await twoFactor.reset()
    assert.equal(done.ok, true, JSON.stringify(done))
    assert.ok(!fs.existsSync(stateFile), '不可读时应删除坏密文')
  } finally {
    twoFactor._resetDeps()
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
