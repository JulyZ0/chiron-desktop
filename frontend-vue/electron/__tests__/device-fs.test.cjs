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
  createDeviceFSBridge,
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

test('git：参数与 cwd 边界', async t => {
  const root = tempProject(t)
  await assert.rejects(() => handleDeviceOp({ op: 'git', root, path: root, args: [] }), /invalid git args/)
  await assert.rejects(
    () => handleDeviceOp({ op: 'git', root, path: root, args: Array(65).fill('status') }),
    /invalid git args/,
  )
  await assert.rejects(
    () => handleDeviceOp({ op: 'git', root, path: path.join(root, '..'), args: ['status'] }),
    /outside project root/,
  )
})

test('git：非仓库返回非零 code 而不是抛错', async t => {
  const root = tempProject(t)
  const res = await handleDeviceOp({ op: 'git', root, path: root, args: ['rev-parse', '--is-inside-work-tree'] })
  assert.equal(typeof res.code, 'number')
  assert.equal(typeof res.stdout, 'string')
  // 没装 git 时是 127；装了但目录不是仓库时是非零 —— 两者都必须走结果而不是异常。
  assert.notEqual(res.code, 0)
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
  fs.writeFileSync(path.join(root, '.env'), 'SECRET=not-for-server')
  fs.writeFileSync(path.join(root, 'id_ed25519'), 'private key')
  fs.mkdirSync(path.join(root, '.ssh'))
  fs.writeFileSync(path.join(root, '.ssh', 'id_ed25519'), 'private key')
  fs.mkdirSync(path.join(root, '.git'))
  fs.writeFileSync(path.join(root, '.git', 'config'), 'secret local metadata')
  fs.mkdirSync(path.join(root, '.github', 'workflows'), { recursive: true })
  fs.writeFileSync(path.join(root, '.github', 'workflows', 'ci.yml'), 'name: ci')
  const res = await handleDeviceOp({ op: 'walk', root, path: root })
  const names = res.entries.map(e => e.name).sort()
  assert.deepEqual(names, ['.github/workflows/ci.yml', '.hidden', 'hello.txt', 'sub/a.txt'])
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

test('remove：expected_hash 与基线不符时按冲突拒绝', async t => {
  const root = tempProject(t)
  const target = path.join(root, 'hello.txt')
  const before = await handleDeviceOp({ op: 'hash', root, path: target })
  fs.writeFileSync(target, 'device edited', 'utf8')
  await assert.rejects(
    () => handleDeviceOp({ op: 'remove', root, path: target, expected_hash: before.hash }),
    /conflict/,
  )
  assert.equal(fs.readFileSync(target, 'utf8'), 'device edited')
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

// ── 连接层：并发/重连/替换/回包路由（假 WebSocket 驱动，不建真实连接）──

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

async function waitFor(cond, label, ms = 3000) {
  const deadline = Date.now() + ms
  while (Date.now() < deadline) {
    if (cond()) return
    await sleep(5)
  }
  throw new Error(`等待超时：${label}`)
}

function fakeApp(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-bridge-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return { getPath: () => dir }
}

/** 记录所有连接实例，并可手动驱动 open/close/message/send。 */
function installFakeWebSocket(t) {
  const instances = []
  class FakeWebSocket {
    constructor(url) {
      this.url = String(url)
      this.readyState = 0
      this.sent = []
      this.listeners = new Map()
      instances.push(this)
    }

    addEventListener(type, fn) {
      if (!this.listeners.has(type)) this.listeners.set(type, [])
      this.listeners.get(type).push(fn)
    }

    emit(type, extra = {}) {
      const ev = { type, target: this, ...extra }
      for (const fn of [...(this.listeners.get(type) || [])]) fn(ev)
    }

    open() {
      this.readyState = 1
      this.emit('open')
    }

    message(obj) {
      this.emit('message', { data: JSON.stringify(obj) })
    }

    send(data) {
      if (this.readyState !== 1) throw new Error('WebSocket is not open')
      this.sent.push(JSON.parse(String(data)))
    }

    close() {
      if (this.readyState === 3) return
      this.readyState = 3
      this.emit('close', { code: 1000, reason: '' })
    }
  }
  const prev = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket
  t.after(() => {
    globalThis.WebSocket = prev
  })
  return instances
}

test('桥：重复 start 只建立一条连接（不会泄漏旧 socket）', async t => {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token',
    // H46：桥不再把 token 放进 WS query，而是先换一次性票据。这些用例测的是
    // 连接/重连语义，票据本身由 device-bridge-auth.test.cjs 覆盖。
    exchangeTicket: async () => ({ ok: true, ticket: 'test-ticket' }),
    log: () => {},
    reconnectMs: 10,
  })
  await Promise.all([
    bridge.start('http://127.0.0.1:9'),
    bridge.start('http://127.0.0.1:9'),
  ])
  assert.equal(instances.length, 1)
  instances[0].open()
  assert.equal(bridge.status().connected, true)
  bridge.stop()
})

test('桥：断线后重连，旧 socket 的迟到 close 不会打断新连接', async t => {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token',
    // H46：桥不再把 token 放进 WS query，而是先换一次性票据。这些用例测的是
    // 连接/重连语义，票据本身由 device-bridge-auth.test.cjs 覆盖。
    exchangeTicket: async () => ({ ok: true, ticket: 'test-ticket' }),
    log: () => {},
    reconnectMs: 10,
    maxReconnectMs: 40,
  })
  try {
    await bridge.start('http://127.0.0.1:9')
    const first = instances[0]
    first.open()
    assert.equal(bridge.status().connected, true)

    first.close()
    assert.equal(bridge.status().connected, false)

    await waitFor(() => instances.length === 2, '重连出第二条连接')
    const second = instances[1]
    second.open()
    assert.equal(bridge.status().connected, true)

    // 旧连接再补一个 close（真实环境里 close/error 可能重复投递）
    first.close()
    first.emit('close', { code: 1006, reason: 'stale' })
    assert.equal(bridge.status().connected, true, '旧 socket 的 close 不能把新连接标记为断开')

    // 也不应因为旧事件多排一次重连
    await sleep(80)
    assert.equal(instances.length, 2)
  } finally {
    bridge.stop()
  }
})

test('桥：断开后不再重连（stop 生效），且不会留下定时器', async t => {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token',
    // H46：桥不再把 token 放进 WS query，而是先换一次性票据。这些用例测的是
    // 连接/重连语义，票据本身由 device-bridge-auth.test.cjs 覆盖。
    exchangeTicket: async () => ({ ok: true, ticket: 'test-ticket' }),
    log: () => {},
    reconnectMs: 10,
  })
  await bridge.start('http://127.0.0.1:9')
  instances[0].open()
  bridge.stop()
  await sleep(80)
  assert.equal(instances.length, 1)
  assert.equal(bridge.status().connected, false)
})

test('桥：op 的回包只发回收到请求的那条连接，不串到重连后的新连接', async t => {
  const instances = installFakeWebSocket(t)
  const root = tempProject(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token',
    // H46：桥不再把 token 放进 WS query，而是先换一次性票据。这些用例测的是
    // 连接/重连语义，票据本身由 device-bridge-auth.test.cjs 覆盖。
    exchangeTicket: async () => ({ ok: true, ticket: 'test-ticket' }),
    log: () => {},
    reconnectMs: 0,
  })
  const unhandled = []
  const onUnhandled = err => unhandled.push(err)
  process.on('unhandledRejection', onUnhandled)
  t.after(() => process.removeListener('unhandledRejection', onUnhandled))
  try {
    await bridge.start('http://127.0.0.1:9')
    const first = instances[0]
    first.open()

    // git op 会 spawn 子进程，回包一定晚于下面的重连。
    first.message({ id: 'op-old', op: 'git', root, path: root, args: ['rev-parse', '--is-inside-work-tree'] })
    first.close()

    await waitFor(() => instances.length === 2, '重连出第二条连接')
    const second = instances[1]
    second.open()

    await sleep(400)
    assert.equal(
      second.sent.some(m => m.id === 'op-old'), false,
      '旧连接的回包不能发到新连接上',
    )
    assert.deepEqual(unhandled, [], '回包失败不能产生未处理的 rejection')

    // 新连接自身的 op 正常回包（证明桥仍在工作）。
    second.message({ id: 'op-new', op: 'stat', root, path: path.join(root, 'hello.txt') })
    await waitFor(() => second.sent.some(m => m.id === 'op-new'), '新连接回包')
    const reply = second.sent.find(m => m.id === 'op-new')
    assert.equal(reply.ok, true)
    assert.equal(reply.stat.size, 'hello world'.length)
  } finally {
    bridge.stop()
  }
})

test('桥：断开连接上的 send 抛错被吞掉，不产生未处理 rejection', async t => {
  const instances = installFakeWebSocket(t)
  const root = tempProject(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token',
    // H46：桥不再把 token 放进 WS query，而是先换一次性票据。这些用例测的是
    // 连接/重连语义，票据本身由 device-bridge-auth.test.cjs 覆盖。
    exchangeTicket: async () => ({ ok: true, ticket: 'test-ticket' }),
    log: () => {},
    reconnectMs: 10000,
  })
  const unhandled = []
  const onUnhandled = err => unhandled.push(err)
  process.on('unhandledRejection', onUnhandled)
  t.after(() => process.removeListener('unhandledRejection', onUnhandled))
  try {
    await bridge.start('http://127.0.0.1:9')
    const socket = instances[0]
    socket.open()
    // 请求进来后马上断开：回包路径必须安全丢弃。
    socket.message({ id: 'op-dead', op: 'walk', root, path: root })
    socket.readyState = 2 // CLOSING：send 会抛错
    await sleep(200)
    assert.deepEqual(unhandled, [])
    assert.equal(socket.sent.some(m => m.id === 'op-dead'), false)
  } finally {
    bridge.stop()
  }
})
