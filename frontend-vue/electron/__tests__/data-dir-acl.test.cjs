'use strict'
/**
 * 数据目录权限收口的契约。
 *
 * 背景：本地模式的隔离边界是「一个 OS 用户一套数据」。`%APPDATA%` 本身是 per-user，
 * NTFS 让该目录继承父目录 ACL —— 于是其他 OS 用户本来就读不到。但那是**继承来的
 * 运气**，不是声明。本模块把它显式化。
 *
 * 另一个动因：`main.cjs` / `secrets.cjs` / `token-store.cjs` 里写的 `{ mode: 0o600 }`
 * 在 Windows 上是**空操作**（Node 忽略 NTFS 上的 mode），所以必须有 icacls 这一层。
 *
 * 全部用例注入 exec，不真的调 icacls，也不碰真实 %APPDATA%。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const acl = require('../data-dir-acl.cjs')

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-acl-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

test('currentAccount 优先用 USERDOMAIN\USERNAME 拼出域账户', () => {
  const account = acl.currentAccount({ USERDOMAIN: 'CORP', USERNAME: 'alice' })
  assert.equal(account, 'CORP\\alice')
})

test('currentAccount 没有域时只用用户名', () => {
  assert.equal(acl.currentAccount({ USERNAME: 'alice' }), 'alice')
})

test('currentAccount 环境变量缺失时退回 os.userInfo', () => {
  const account = acl.currentAccount({}, () => ({ username: 'bob' }))
  assert.equal(account, 'bob')
})

test('currentAccount 在 userInfo 抛错时返回空串而非崩溃', () => {
  const account = acl.currentAccount({}, () => {
    throw new Error('no passwd entry')
  })
  assert.equal(account, '')
})

test('buildIcaclsArgs 先补授权再移除继承', () => {
  const steps = acl.buildIcaclsArgs('C:\\data', 'CORP\\alice')
  assert.equal(steps.length, 2)
  // 第一步：补当前账户与 SYSTEM 的完全控制
  assert.deepEqual(steps[0], [
    'C:\\data',
    '/grant:r', 'CORP\\alice:(OI)(CI)F',
    '/grant:r', 'SYSTEM:(OI)(CI)F',
  ])
  // 第二步：去掉继承 —— 顺序反过来会有「谁都不能访问」的瞬间
  assert.deepEqual(steps[1], ['C:\\data', '/inheritance:r'])
})

test('buildIcaclsArgs 遇到空账户不会生成空授权项', () => {
  const steps = acl.buildIcaclsArgs('C:\\data', '')
  assert.equal(steps[0].includes(':(OI)(CI)F'), false)
  assert.deepEqual(steps[0], ['C:\\data', '/grant:r', 'SYSTEM:(OI)(CI)F'])
})

test('Windows 下依次执行两条 icacls', t => {
  const dir = tempDir(t)
  const calls = []
  const result = acl.hardenDataDir({
    dir,
    platform: 'win32',
    account: 'CORP\\alice',
    exec: (file, args) => calls.push([file, args]),
  })
  assert.equal(result.ok, true)
  assert.equal(calls.length, 2)
  assert.equal(calls[0][0], 'icacls')
  assert.deepEqual(calls[1][1], [dir, '/inheritance:r'])
})

test('icacls 失败时返回 ok:false 并带原因，不抛', t => {
  const dir = tempDir(t)
  const result = acl.hardenDataDir({
    dir,
    platform: 'win32',
    account: 'CORP\\alice',
    exec: () => {
      throw new Error('Access is denied.')
    },
  })
  assert.equal(result.ok, false)
  assert.match(result.reason, /icacls-failed/)
})

test('非 Windows 平台走 chmod 0700，不碰 icacls', t => {
  const dir = tempDir(t)
  const result = acl.hardenDataDir({
    dir,
    platform: 'linux',
    exec: () => {
      throw new Error('不该调用 icacls')
    },
  })
  assert.equal(result.ok, true)
})

test('拿不到账户名时跳过，绝不传空账户给 icacls', t => {
  const dir = tempDir(t)
  const calls = []
  const result = acl.hardenDataDir({
    dir,
    platform: 'win32',
    account: '',
    exec: (file, args) => calls.push(args),
  })
  assert.equal(result.ok, false)
  assert.equal(result.skipped, true)
  assert.equal(result.reason, 'no-account')
  assert.equal(calls.length, 0, '空账户会清空现有 ACL，必须一次都不调')
})

test('账户名含引号或换行时拒绝拼命令行', t => {
  const dir = tempDir(t)
  for (const bad of ['a"b', 'a\nb', 'a\rb']) {
    const result = acl.hardenDataDir({
      dir,
      platform: 'win32',
      account: bad,
      exec: () => assert.fail('不该执行'),
    })
    assert.equal(result.skipped, true)
    assert.equal(result.reason, 'unsafe-account')
  }
})

test('相对路径被拒绝：收错地方的锁比不锁更危险', () => {
  const result = acl.hardenDataDir({ dir: 'relative/dir', exec: () => {} })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'not-absolute')
})

test('空目录名被拒绝', () => {
  assert.equal(acl.hardenDataDir({}).reason, 'empty-dir')
})

test('目录不存在时会先创建再收口', t => {
  const base = tempDir(t)
  const nested = path.join(base, 'deep', 'nested')
  const calls = []
  const result = acl.hardenDataDir({
    dir: nested,
    platform: 'win32',
    account: 'CORP\\alice',
    exec: (file, args) => calls.push(args),
  })
  assert.equal(result.ok, true)
  assert.equal(fs.existsSync(nested), true)
  assert.equal(calls.length, 2)
})

test('main.cjs 在单实例锁之前收口，且只在隔离模式下做', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  const harden = source.indexOf('dataDirAcl.hardenDataDir(')
  const lock = source.indexOf('app.requestSingleInstanceLock()')
  assert.ok(harden > 0, 'main.cjs 应当调用 hardenDataDir')
  assert.ok(lock > 0)
  assert.ok(harden < lock, '收口必须在单实例锁之前 —— 锁文件就在该目录里')
  assert.match(source, /if \(enforceUserDataIsolation\)/, '仅本地模式收口，云端不碰')
})
