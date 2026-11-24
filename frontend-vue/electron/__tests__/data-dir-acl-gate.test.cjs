'use strict'
/**
 * H52：敏感数据目录 ACL 失败门禁。
 *
 * 现状缺陷（修复前）：
 *   1. `icacls` 只做 `/grant:r` + `/inheritance:r` —— 别人**已经写下的显式 ACE**
 *      原样留着，目录看起来加固成功、实际仍对未授权主体开放；
 *   2. 加固失败只 `logMain('warn', ...)`，token / provider key 照写不误。
 *
 * 本 spec 钉两件事：
 *   · 加固会显式审计 DACL、逐个移除未授权主体、再验证；有残留就必须 `ok:false`；
 *   · 加固未通过时，落盘门禁**拒绝**写 token/secret（而不是记一行日志继续）。
 *
 * 全部用例注入 exec，不真的调 icacls，也不碰真实 %APPDATA%。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const acl = require('../data-dir-acl.cjs')
const { createSensitiveStoreGate, GATE_REASON } = require('../sensitive-store-gate.cjs')

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-acl-gate-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

const ACL_HEADER_FREE = [
  'C:\\data DESKTOP-1\\alice:(OI)(CI)(F)',
  '         NT AUTHORITY\\SYSTEM:(OI)(CI)(F)',
  '         BUILTIN\\Users:(OI)(CI)(RX)',
  'Successfully processed 1 files; Failed processing 0 files',
].join('\r\n')

/** 一个可编程的假 icacls：按参数分派，并记录每次调用。 */
function fakeIcacls({ acls }) {
  const calls = []
  const queue = [...acls]
  const exec = (file, args) => {
    calls.push({ file, args: [...args] })
    assert.equal(file, 'icacls')
    if (args.length === 1) {
      // 纯 `icacls <dir>`：读取 DACL
      return queue.length ? queue.shift() : ''
    }
    return ''
  }
  return { exec, calls, remaining: () => queue.length }
}

test('parseIcaclsAcl：剥离首行路径，保留带空格的主体名，忽略本地化收尾行', () => {
  const aces = acl.parseIcaclsAcl(ACL_HEADER_FREE, 'C:\\data')
  assert.deepEqual(aces.map(a => a.principal), [
    'DESKTOP-1\\alice',
    'NT AUTHORITY\\SYSTEM',
    'BUILTIN\\Users',
  ])
  assert.equal(aces[2].inherited, false, '显式 ACE 不该被当成继承项')
})

test('parseIcaclsAcl：继承标记 (I) 被识别', () => {
  const aces = acl.parseIcaclsAcl('C:\\data BUILTIN\\Users:(I)(OI)(CI)(RX)', 'C:\\data')
  assert.equal(aces.length, 1)
  assert.equal(aces[0].inherited, true)
})

test('unauthorizedAces：只放行当前账户与 SYSTEM', () => {
  const aces = acl.parseIcaclsAcl(ACL_HEADER_FREE, 'C:\\data')
  const offenders = acl.unauthorizedAces(aces, { account: 'DESKTOP-1\\alice' })
  assert.deepEqual(offenders.map(a => a.principal), ['BUILTIN\\Users'])
})

test('unauthorizedAces：allowedExtra 可以显式保留指定主体', () => {
  const aces = acl.parseIcaclsAcl(ACL_HEADER_FREE, 'C:\\data')
  const offenders = acl.unauthorizedAces(aces, {
    account: 'DESKTOP-1\\alice',
    allowedExtra: ['BUILTIN\\Users'],
  })
  assert.deepEqual(offenders, [])
})

test('maskPrincipal：输出脱敏，不回显完整账户名或 SID', () => {
  const masked = acl.maskPrincipal('CORP\\alice')
  assert.equal(masked.includes('alice'), false, `脱敏后仍含账户名：${masked}`)
  assert.match(masked, /^C\*\*\*\\a\*\*\*e$/)
  assert.equal(acl.maskPrincipal('S-1-5-21-1111-2222-3333-1001'), 'S-1-5-***')
  assert.equal(acl.maskPrincipal(''), '***')
})

test('hardenDataDir(audit)：审计并移除未授权 ACE，再验证通过', t => {
  const dir = tempDir(t)
  const before = [
    'C:\\data DESKTOP-1\\alice:(OI)(CI)(F)',
    '         NT AUTHORITY\\SYSTEM:(OI)(CI)(F)',
    '         BUILTIN\\Users:(OI)(CI)(RX)',
  ].join('\r\n')
  const after = [
    'C:\\data DESKTOP-1\\alice:(OI)(CI)(F)',
    '         NT AUTHORITY\\SYSTEM:(OI)(CI)(F)',
  ].join('\r\n')
  const fake = fakeIcacls({ acls: [before, after] })

  const result = acl.hardenDataDir({
    dir, platform: 'win32', account: 'DESKTOP-1\\alice', audit: true, exec: fake.exec,
  })

  assert.equal(result.ok, true, JSON.stringify(result))
  // 授权 → 去继承 → 审计 → 移除未授权 → 再审计
  const removals = fake.calls.filter(c => c.args.includes('/remove:g'))
  assert.equal(removals.length, 1, '未授权 ACE 必须被显式移除')
  assert.deepEqual(removals[0].args, [dir, '/remove:g', 'BUILTIN\\Users'])
  assert.equal(fake.calls.filter(c => c.args.length === 1).length, 2, '必须审计两次（前/后）')
  // 返回值里的 DACL 是脱敏后的
  const principals = result.dacl.entries.map(e => e.principal)
  assert.ok(principals.every(p => !p.includes('alice')), principals.join(','))
  assert.deepEqual(result.dacl.residual, [])
})

test('hardenDataDir(audit)：未授权 ACE 移除不掉时 ok:false 并给出脱敏残余', t => {
  const dir = tempDir(t)
  const stuck = [
    'C:\\data DESKTOP-1\\alice:(OI)(CI)(F)',
    '         NT AUTHORITY\\SYSTEM:(OI)(CI)(F)',
    '         BUILTIN\\Users:(OI)(CI)(RX)',
  ].join('\r\n')
  // 两次审计都还看得见 BUILTIN\Users：模拟"移不掉"（例如策略/继承造成的顽固 ACE）
  const fake = fakeIcacls({ acls: [stuck, stuck] })

  const result = acl.hardenDataDir({
    dir, platform: 'win32', account: 'DESKTOP-1\\alice', audit: true, exec: fake.exec,
  })

  assert.equal(result.ok, false, '有残余 ACE 时绝不能报 ok')
  assert.equal(result.reason, 'acl-residual')
  assert.equal(result.dacl.residual.length, 1)
  assert.equal(result.dacl.residual[0].principal.includes('Users'), false, '残余主体名必须脱敏')
})

test('hardenDataDir(audit)：审计读不出 ACE 视为失败（不能把"没看见"当干净）', t => {
  const dir = tempDir(t)
  const fake = fakeIcacls({ acls: [''] })
  const result = acl.hardenDataDir({
    dir, platform: 'win32', account: 'DESKTOP-1\\alice', audit: true, exec: fake.exec,
  })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'acl-audit-empty')
})

test('hardenDataDir(audit)：审计调用抛错也按失败处理', t => {
  const dir = tempDir(t)
  let call = 0
  const result = acl.hardenDataDir({
    dir,
    platform: 'win32',
    account: 'DESKTOP-1\\alice',
    audit: true,
    exec: () => {
      call += 1
      if (call <= 2) return ''
      throw new Error('Access is denied.')
    },
  })
  assert.equal(result.ok, false)
  assert.match(result.reason, /acl-audit-failed/)
})

test('summarizeDacl：只输出数量与脱敏主体，不含原文账户名', () => {
  const summary = acl.summarizeDacl({
    dacl: {
      entries: [{ principal: 'D***\\a***e' }],
      residual: [{ principal: 'B***\\U***s' }],
    },
  })
  assert.match(summary, /entries=1/)
  assert.match(summary, /residual=1/)
  assert.equal(summary.includes('alice'), false)
})

// ── 落盘门禁 ───────────────────────────────────────────────────────────────

test('门禁：未评估即拒绝（fail-closed）', () => {
  const gate = createSensitiveStoreGate()
  assert.equal(gate.isAllowed(), false)
  const refusal = gate.refusal('token')
  assert.equal(refusal.ok, false)
  assert.equal(refusal.reason, GATE_REASON)
})

test('门禁：ACL 加固失败时拒绝写 token —— 修复前这里是"记日志继续"', () => {
  const logs = []
  const gate = createSensitiveStoreGate({ log: (level, msg) => logs.push([level, msg]) })

  // 假的 token 写入器：只有门禁放行才会真正落盘
  const disk = []
  const saveToken = value => {
    if (!gate.isAllowed()) return gate.refusal('token')
    disk.push(value)
    return { ok: true }
  }

  gate.applyHardenResult({ ok: false, reason: 'acl-residual' })
  assert.deepEqual(saveToken('bearer-abc'), {
    ok: false,
    reason: GATE_REASON,
    message: '敏感数据目录加固未通过（acl-residual），已拒绝写入 token',
  })
  assert.deepEqual(disk, [], '加固失败时不得落盘')
  assert.equal(logs.length, 1, '拒绝必须可观测（不静默）')
  assert.equal(logs[0][0], 'error')
})

test('门禁：skipped（取不到账户名）同样拒绝 —— 那是"没加固"，不是"不需要加固"', () => {
  const gate = createSensitiveStoreGate()
  gate.applyHardenResult({ ok: false, skipped: true, reason: 'no-account' })
  assert.equal(gate.isAllowed(), false)
  assert.equal(gate.state().skipped, true)
})

test('门禁：加固通过后允许写入', () => {
  const gate = createSensitiveStoreGate()
  gate.applyHardenResult({ ok: true, dacl: {} })
  assert.equal(gate.isAllowed(), true)
  assert.equal(gate.state().reason, 'ok')
})

test('main.cjs：加固带 audit、结论喂给门禁，且 token/密钥两条写入路径都过门禁', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  assert.match(source, /hardenDataDir\(\{\s*dir: app\.getPath\('userData'\),\s*audit: true\s*\}\)/,
    '启动时必须显式审计 DACL')
  assert.match(source, /sensitiveStoreGate\.applyHardenResult\(aclResult\)/,
    '加固结论必须进入门禁')
  assert.match(source, /summarizeDacl\(aclResult\)/, '必须输出（脱敏）DACL 验证结果')
  const saveToken = source.indexOf("ipcMain.handle('desktop:save-token'")
  assert.ok(saveToken > 0)
  assert.ok(
    source.slice(saveToken, saveToken + 1200).includes('sensitiveStoreGate.refusal'),
    'save-token 必须过门禁',
  )
  const saveModel = source.indexOf("ipcMain.handle('desktop:save-model-config'")
  assert.ok(saveModel > 0)
  assert.ok(
    source.slice(saveModel, saveModel + 800).includes('sensitiveStoreGate.isAllowed()'),
    'save-model-config 必须过门禁',
  )
})
