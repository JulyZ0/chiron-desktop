'use strict'
/**
 * H51：依赖供应链策略（哈希锁 / 批准源 / fail-closed）。
 *
 * 修前的实际后果：`uv pip install -r 清单` 既不要求 hash 锁，也会在失败后自动换第三方
 * 镜像 —— 装进 venv 的包既没被固定到确切字节，也可能来自没审计过的源，而那个 venv
 * 随后会以用户身份执行模型生成的代码。
 */
const test = require('node:test')
const assert = require('node:assert')

const depLock = require('../dep-lock.cjs')

const HASH_LOCK = [
  'httpx==0.27.2 \\',
  '    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'pydantic==2.9.2 \\',
  '    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  '',
].join('\n')

test('inspectLock：全部精确版本且带 sha256 才算锁住', () => {
  const verdict = depLock.inspectLock(HASH_LOCK)
  assert.equal(verdict.ok, true)
  assert.equal(verdict.count, 2)
})

test('inspectLock：范围版本不算锁（>= / ~=）', () => {
  const lock = 'httpx>=0.27 --hash=sha256:' + 'a'.repeat(64) + '\n'
  const verdict = depLock.inspectLock(lock)
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'lock-not-pinned')
  assert.deepEqual(verdict.packages, ['httpx>=0.27'])
})

test('inspectLock：缺 hash 不算锁（半个锁比没锁更糟）', () => {
  const lock = 'httpx==0.27.2\n'
  const verdict = depLock.inspectLock(lock)
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'lock-not-hashed')
})

test('inspectLock：弱哈希算法不算（md5 不是完整性证明）', () => {
  const lock = 'httpx==0.27.2 --hash=md5:' + 'a'.repeat(32) + '\n'
  assert.equal(depLock.inspectLock(lock).ok, false)
})

test('inspectLock：空清单判为不可用', () => {
  assert.equal(depLock.inspectLock('').ok, false)
  assert.equal(depLock.inspectLock('# 只有注释\n').ok, false)
})

test('installPolicy：有锁 ⇒ 放行且要求 --require-hashes', () => {
  const policy = depLock.installPolicy({ lockText: HASH_LOCK })
  assert.equal(policy.ok, true)
  assert.equal(policy.requireHashes, true)
  assert.equal(policy.unpinned, false)
})

test('installPolicy：无锁且未显式放行 ⇒ 拒绝联网安装', () => {
  const policy = depLock.installPolicy({})
  assert.equal(policy.ok, false)
  assert.equal(policy.reason, 'no-lock')
  assert.match(policy.message, /CHIRON_ALLOW_UNPINNED_DEPS/)
})

test('installPolicy：无锁但显式放行 ⇒ 通过且标注 unpinned', () => {
  const policy = depLock.installPolicy({ allowUnpinned: true })
  assert.equal(policy.ok, true)
  assert.equal(policy.requireHashes, false)
  assert.equal(policy.unpinned, true)
  assert.ok(policy.message, '放行必须留下可读说明')
})

test('installPolicy：锁存在但不可信时，即使允许无锁也拒绝', () => {
  // 有锁就等于声明"我要可验证"，此时不能悄悄退化。
  const policy = depLock.installPolicy({ lockText: 'httpx==0.27.2\n', allowUnpinned: true })
  assert.equal(policy.ok, false)
  assert.equal(policy.reason, 'lock-not-hashed')
})

test('approvedIndexUrl：只接受 HTTPS 且主机在名单里', () => {
  assert.equal(depLock.approvedIndexUrl('https://pypi.org/simple').ok, true)
  assert.equal(depLock.approvedIndexUrl('').ok, true, '空表示用 uv 默认源')
  assert.equal(depLock.approvedIndexUrl('http://pypi.org/simple').reason, 'index-not-https')
  assert.equal(depLock.approvedIndexUrl('https://evil.example/simple').reason, 'index-host-not-approved')
  assert.equal(depLock.approvedIndexUrl('not a url').reason, 'unparsable-index-url')
})

test('supplyChainArgs：有锁时 --require-hashes 在 -r 之前，index 在最后', () => {
  const args = depLock.supplyChainArgs(
    { ok: true, requireHashes: true },
    '/tmp/requirements.lock',
    'https://pypi.org/simple',
  )
  assert.deepEqual(args, [
    '--require-hashes',
    '-r',
    '/tmp/requirements.lock',
    '--index-url',
    'https://pypi.org/simple',
  ])
})

test('supplyChainArgs：无锁时不加 --require-hashes', () => {
  const args = depLock.supplyChainArgs({ ok: true, requireHashes: false }, '/tmp/requirements.txt', '')
  assert.deepEqual(args, ['-r', '/tmp/requirements.txt'])
})
