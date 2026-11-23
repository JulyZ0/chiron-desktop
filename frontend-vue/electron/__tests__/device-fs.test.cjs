'use strict'
/**
 * S1 设备文件桥（本机侧）：路径包含校验、只读 op、截断语义。
 * 不启动 WebSocket —— 连接层由主进程联调覆盖，这里钉纯逻辑。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  handleDeviceOp,
  canonicalRootKey,
  ensureInside,
  toWsUrl,
} = require('../device-fs.cjs')

function tempProject(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-device-fs-'))
  fs.writeFileSync(path.join(dir, 'hello.txt'), 'hello world', 'utf8')
  fs.mkdirSync(path.join(dir, 'sub'))
  fs.writeFileSync(path.join(dir, 'sub', 'a.txt'), 'A', 'utf8')
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

test('canonicalize：存在目录返回 root_key / exists / is_dir', async t => {
  const root = tempProject(t)
  const res = await handleDeviceOp({ op: 'canonicalize', path: root })
  assert.equal(res.exists, true)
  assert.equal(res.is_dir, true)
  assert.ok(res.root_key)
  if (process.platform === 'win32' || process.platform === 'darwin') {
    assert.equal(res.root_key, canonicalRootKey(root.toUpperCase()))
  }
})

test('canonicalize：不存在的路径也返回词法 key（不报错）', async () => {
  const missing = path.join(os.tmpdir(), 'chiron-missing-' + Date.now())
  const res = await handleDeviceOp({ op: 'canonicalize', path: missing })
  assert.equal(res.exists, false)
  assert.ok(res.root_key)
})

test('ensureInside：拒绝项目根之外与 ..', () => {
  const root = path.join(os.tmpdir(), 'chiron-root')
  assert.equal(ensureInside(root, path.join(root, 'a')), path.join(root, 'a'))
  assert.equal(ensureInside(root, path.join(root, '..', 'evil')), null)
})

test('read/list/stat：只读原语与截断', async t => {
  const root = tempProject(t)
  const stat = await handleDeviceOp({ op: 'stat', root, path: path.join(root, 'hello.txt') })
  assert.equal(stat.stat.is_dir, false)
  assert.equal(stat.stat.mode, 'regular')
  assert.equal(stat.stat.size, 'hello world'.length)

  const read = await handleDeviceOp({
    op: 'read', root, path: path.join(root, 'hello.txt'), max_bytes: 5,
  })
  assert.equal(Buffer.from(read.data_b64, 'base64').toString('utf8'), 'hello')
  assert.equal(read.truncated, true)

  const list = await handleDeviceOp({ op: 'list', root, path: root })
  const names = list.entries.map(e => e.name).sort()
  assert.deepEqual(names, ['hello.txt', 'sub'])
})

test('越界读取被拒绝', async t => {
  const root = tempProject(t)
  await assert.rejects(
    () => handleDeviceOp({ op: 'read', root, path: path.join(root, '..', 'outside.txt') }),
    /outside project root/,
  )
})

test('toWsUrl：http→ws、https→wss，并保留 host', () => {
  assert.equal(toWsUrl('http://127.0.0.1:8080').toString(), 'ws://127.0.0.1:8080/ws/device-fs')
  assert.equal(toWsUrl('https://chiron.example.com').toString(), 'wss://chiron.example.com/ws/device-fs')
})

test('walk：递归列出相对路径并跳过噪音目录', async t => {
  const root = tempProject(t)
  fs.mkdirSync(path.join(root, 'node_modules'))
  fs.writeFileSync(path.join(root, 'node_modules', 'x.js'), 'x')
  fs.writeFileSync(path.join(root, '.hidden'), 'h')
  const res = await handleDeviceOp({ op: 'walk', root, path: root })
  const names = res.entries.map(e => e.name).sort()
  assert.deepEqual(names, ['hello.txt', 'sub/a.txt'])
  assert.equal(res.truncated, false)
})

test('write：expected_hash 冲突时拒绝，匹配时写入并返回新 hash', async t => {
  const root = tempProject(t)
  const target = path.join(root, 'hello.txt')
  const before = await handleDeviceOp({ op: 'hash', root, path: target })
  await assert.rejects(
    () => handleDeviceOp({
      op: 'write', root, path: target,
      data_b64: Buffer.from('x').toString('base64'),
      expected_hash: 'deadbeef',
    }),
    /conflict/,
  )
  const ok = await handleDeviceOp({
    op: 'write', root, path: target,
    data_b64: Buffer.from('new').toString('base64'),
    expected_hash: before.hash,
  })
  assert.ok(ok.hash)
  assert.equal(fs.readFileSync(target, 'utf8'), 'new')
})

test('write：新建文件（无期望 hash）可创建父目录', async t => {
  const root = tempProject(t)
  const target = path.join(root, 'deep', 'dir', 'new.txt')
  const res = await handleDeviceOp({
    op: 'write', root, path: target,
    data_b64: Buffer.from('hi').toString('base64'),
  })
  assert.equal(fs.readFileSync(target, 'utf8'), 'hi')
  assert.ok(res.hash)
})

test('read：支持 offset（WebDAV 分段读）', async t => {
  const root = tempProject(t)
  const res = await handleDeviceOp({
    op: 'read', root, path: path.join(root, 'hello.txt'), offset: 6, max_bytes: 5,
  })
  assert.equal(Buffer.from(res.data_b64, 'base64').toString('utf8'), 'world')
})

test('mkdir / rename / remove（WebDAV 目录操作）', async t => {
  const root = tempProject(t)
  await handleDeviceOp({ op: 'mkdir', root, path: path.join(root, 'newdir') })
  assert.equal(fs.statSync(path.join(root, 'newdir')).isDirectory(), true)

  fs.writeFileSync(path.join(root, 'newdir', 'x.txt'), 'x')
  await handleDeviceOp({
    op: 'rename', root,
    path: path.join(root, 'newdir', 'x.txt'),
    to: path.join(root, 'newdir', 'y.txt'),
  })
  assert.equal(fs.existsSync(path.join(root, 'newdir', 'x.txt')), false)
  assert.equal(fs.readFileSync(path.join(root, 'newdir', 'y.txt'), 'utf8'), 'x')

  await handleDeviceOp({ op: 'remove', root, path: path.join(root, 'newdir') })
  assert.equal(fs.existsSync(path.join(root, 'newdir')), false)
})
