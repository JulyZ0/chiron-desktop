'use strict'
/**
 * H46：云端设备桥的能力、根目录与设备身份授权。
 *
 * 修复前的四处缺口（本 spec 逐条钉住）：
 *   1. 服务端在 op 里传任意 `root`，本端只做包含校验 → 任意绝对路径都能读/写；
 *   2. `canonicalize` 之后包含校验有"词法回退"分支 → symlink / junction 逃逸；
 *   3. token 放在 WS query 里（会被日志/代理抄走），`device_id` 只要求非空；
 *   4. 服务端断连一律按"重连"处理，撤销与 HTTP 401 语义不一致。
 *
 * 全部用例用假 WebSocket + 注入的票据换取函数，不建真实连接、不碰真实网关。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  createDeviceFSBridge,
  handleDeviceOp,
  requestDeviceTicket,
  runDeviceGit,
  DEVICE_OPS,
  TICKET_PATH,
} = require('../device-fs.cjs')
const rootsStore = require('../approved-roots.cjs')
const identityModule = require('../device-identity.cjs')
const { EventEmitter } = require('node:events')

function tempDir(t, prefix = 'chiron-h46-') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

function tempProject(t) {
  const root = tempDir(t, 'chiron-h46-proj-')
  fs.writeFileSync(path.join(root, 'hello.txt'), 'hello', 'utf8')
  fs.mkdirSync(path.join(root, 'sub'))
  return root
}

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
  const dir = tempDir(t, 'chiron-h46-userdata-')
  return { getPath: () => dir }
}

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

    close(code = 1000) {
      if (this.readyState === 3) return
      this.readyState = 3
      this.emit('close', { code, reason: '' })
    }
  }
  const prev = globalThis.WebSocket
  globalThis.WebSocket = FakeWebSocket
  t.after(() => {
    globalThis.WebSocket = prev
  })
  return instances
}

/** 建立一条桥，返回 instances + 已连上的 socket。 */
async function connectedBridge(t, options = {}) {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token-abc',
    log: () => {},
    reconnectMs: 10,
    exchangeTicket: async () => ({ ok: true, ticket: 'ticket-1' }),
    ...options,
  })
  await bridge.start('http://127.0.0.1:9')
  const socket = instances[0]
  assert.ok(socket, '应当建立一条连接')
  socket.open()
  return { bridge, socket, instances }
}

// ── 1) 根授权 ──────────────────────────────────────────────────────────────

test('op：未授权 root 被拒（修复前服务端选哪个根就用哪个）', async t => {
  const root = tempProject(t)
  const approve = rootsStore.canonicalizeDir(root)
  const deps = { isRootGranted: candidate => rootsStore.isWithin(approve, candidate) }

  const ok = await handleDeviceOp(
    { op: 'read', root, path: path.join(root, 'hello.txt') },
    deps,
  )
  assert.equal(Buffer.from(ok.data_b64, 'base64').toString('utf8'), 'hello')

  const other = tempProject(t)
  await assert.rejects(
    () => handleDeviceOp({ op: 'read', root: other, path: path.join(other, 'hello.txt') }, deps),
    /root not granted/,
  )
})

test('op：canonicalize 也不接受未授权路径（不再当"无害探测"放行）', async t => {
  const root = tempProject(t)
  const deps = { isRootGranted: () => false }
  await assert.rejects(
    () => handleDeviceOp({ op: 'canonicalize', path: root }, deps),
    /root not granted/,
  )
})

test('桥：服务端请求未授权的根时由主进程问用户，用户拒绝则 op 失败', async t => {
  const root = tempProject(t)
  const asked = []
  const { socket } = await connectedBridge(t, {
    isRootGranted: () => false,
    requestRootApproval: async info => {
      asked.push(info)
      return false
    },
  })

  socket.message({ id: 'op-1', op: 'read', root, path: path.join(root, 'hello.txt') })
  await waitFor(() => socket.sent.some(m => m.id === 'op-1'), '回包')
  const reply = socket.sent.find(m => m.id === 'op-1')
  assert.equal(reply.ok, false)
  assert.match(reply.error, /root not granted/)
  assert.equal(asked.length, 1)
  assert.equal(asked[0].root, root)
})

test('桥：用户当场批准后 op 立即放行', async t => {
  const root = tempProject(t)
  const approved = new Set()
  const { socket } = await connectedBridge(t, {
    isRootGranted: candidate => approved.has(rootsStore.canonicalizeDir(candidate)),
    requestRootApproval: async info => {
      approved.add(rootsStore.canonicalizeDir(info.root))
      return true
    },
  })
  socket.message({ id: 'op-2', op: 'read', root, path: path.join(root, 'hello.txt') })
  await waitFor(() => socket.sent.some(m => m.id === 'op-2'), '回包')
  const reply = socket.sent.find(m => m.id === 'op-2')
  assert.equal(reply.ok, true)
  assert.equal(Buffer.from(reply.data_b64, 'base64').toString('utf8'), 'hello')
})

// ── 2) realpath-only，拒绝 symlink / junction 逃逸 ─────────────────────────

test('op：经 junction 逃出根的读写都被拒（词法回退已删除）', async t => {
  const root = tempProject(t)
  const outside = tempDir(t, 'chiron-h46-outside-')
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'top secret', 'utf8')
  const link = path.join(root, 'escape')
  try {
    fs.symlinkSync(outside, link, 'junction')
  } catch (err) {
    t.skip(`该环境不支持创建 junction：${err.code || err.message}`)
    return
  }
  for (const op of ['read', 'stat', 'list']) {
    await assert.rejects(
      () => handleDeviceOp({ op, root, path: path.join(link, op === 'list' ? '' : 'secret.txt') }),
      /outside project root/,
      op,
    )
  }
  // 写入同样不能借 junction 落到根外
  await assert.rejects(
    () => handleDeviceOp({
      op: 'write',
      root,
      path: path.join(link, 'planted.txt'),
      data_b64: Buffer.from('x').toString('base64'),
    }),
    /outside project root/,
  )
  assert.equal(fs.existsSync(path.join(outside, 'planted.txt')), false)
})

test('op：指向根外文件的 symlink 被拒（被操作对象本身是链接）', async t => {
  const root = tempProject(t)
  const outside = tempDir(t, 'chiron-h46-outside2-')
  const secret = path.join(outside, 'secret.txt')
  fs.writeFileSync(secret, 'top secret', 'utf8')
  const linkFile = path.join(root, 'looks-inside.txt')
  try {
    fs.symlinkSync(secret, linkFile, 'file')
  } catch (err) {
    t.skip(`该环境不支持创建符号链接：${err.code || err.message}`)
    return
  }
  await assert.rejects(
    () => handleDeviceOp({ op: 'read', root, path: linkFile }),
    /outside project root/,
  )
})

test('op：未知 op 在执行任何 IO 之前就被拒（白名单）', async t => {
  const root = tempProject(t)
  assert.ok(DEVICE_OPS.has('read'))
  assert.equal(DEVICE_OPS.has('exec'), false)
  await assert.rejects(
    () => handleDeviceOp({ op: 'exec', root, path: root }),
    /unsupported op/,
  )
})

test('op：UNC 路径即使落在词法根内也不接受', async t => {
  const root = tempProject(t)
  await assert.rejects(
    () => handleDeviceOp({ op: 'read', root, path: '\\\\server\\share\\x.txt' }),
    /outside project root/,
  )
})

// ── 3) 短时一次性票据 + 设备身份 ───────────────────────────────────────────

test('票据：token 只出现在 Authorization 头，不出现在 URL 或请求体', async t => {
  const dir = tempDir(t, 'chiron-h46-identity-')
  const identity = identityModule.ensureDeviceIdentity({ dir })
  assert.equal(identity.ok, true)
  const calls = []
  const result = await requestDeviceTicket({
    apiUrl: 'https://chiron.example.com',
    token: 'secret-token',
    deviceId: 'device-1',
    identity,
    fetchFn: async (url, init) => {
      calls.push({ url, init })
      return {
        ok: true,
        status: 200,
        json: async () => ({ ticket: 'ticket-xyz', expires_in: 45 }),
      }
    },
  })
  assert.equal(result.ok, true)
  assert.equal(result.ticket, 'ticket-xyz')
  const call = calls[0]
  assert.equal(new URL(call.url).pathname, TICKET_PATH)
  assert.equal(call.url.includes('secret-token'), false, 'token 不得出现在 URL 里')
  assert.equal(call.init.headers.authorization, 'Bearer secret-token')
  assert.equal(String(call.init.body).includes('secret-token'), false, 'token 不得出现在 body 里')
  const body = JSON.parse(call.init.body)
  assert.equal(body.device_id, 'device-1')
  assert.ok(body.signature, '必须带设备签名')
})

test('票据：服务端拒绝（401/403）时标记 revoked —— 与 HTTP 同语义', async t => {
  const dir = tempDir(t, 'chiron-h46-identity-')
  const identity = identityModule.ensureDeviceIdentity({ dir })
  for (const status of [401, 403]) {
    const result = await requestDeviceTicket({
      apiUrl: 'https://chiron.example.com',
      token: 't',
      deviceId: 'd',
      identity,
      fetchFn: async () => ({ ok: false, status, json: async () => ({}) }),
    })
    assert.equal(result.ok, false)
    assert.equal(result.revoked, true, String(status))
  }
})

test('票据：端点未实现（404）时失败，不降级到 query token', async t => {
  const dir = tempDir(t, 'chiron-h46-identity-')
  const identity = identityModule.ensureDeviceIdentity({ dir })
  const result = await requestDeviceTicket({
    apiUrl: 'https://chiron.example.com',
    token: 't',
    deviceId: 'd',
    identity,
    fetchFn: async () => ({ ok: false, status: 404, json: async () => ({}) }),
  })
  assert.equal(result.ok, false)
  assert.equal(result.revoked, undefined)
  assert.match(result.message, /server support required/)
})

test('设备身份：Ed25519 证明可被校验，篡改任一字段即失效', t => {
  const dir = tempDir(t, 'chiron-h46-identity-')
  const identity = identityModule.ensureDeviceIdentity({ dir })
  assert.equal(identity.ok, true)
  // 私钥落盘。POSIX 上 mode 真生效；Windows 上 Node 忽略 mode（NTFS 不走 POSIX
  // 权限位），那条路径的隔离靠 userData 的 ACL 收口（H52）。
  const keyFile = path.join(dir, identityModule.KEY_FILE)
  assert.equal(fs.existsSync(keyFile), true)
  if (process.platform !== 'win32') {
    const mode = fs.statSync(keyFile).mode & 0o777
    assert.equal(mode & 0o077, 0, `私钥权限过宽：${mode.toString(8)}`)
  }

  const proof = identityModule.buildDeviceProof({ identity, deviceId: 'device-9', issuedAt: 1700000000000 })
  assert.equal(identityModule.verifyDeviceProof({ proof, deviceId: 'device-9' }), true)
  assert.equal(identityModule.verifyDeviceProof({ proof, deviceId: 'device-other' }), false)
  assert.equal(
    identityModule.verifyDeviceProof({ proof: { ...proof, nonce: 'tampered' }, deviceId: 'device-9' }),
    false,
  )
  assert.equal(
    identityModule.verifyDeviceProof({ proof: { ...proof, issued_at: 1 }, deviceId: 'device-9' }),
    false,
  )
})

test('设备身份：同一目录下重复取用得到同一把钥匙（不会悄悄换身份）', t => {
  const dir = tempDir(t, 'chiron-h46-identity-')
  const first = identityModule.ensureDeviceIdentity({ dir })
  const second = identityModule.ensureDeviceIdentity({ dir })
  assert.equal(first.keyId, second.keyId)
  assert.equal(first.publicKeySpki, second.publicKeySpki)
})

test('桥：WS URL 只带 ticket + device_id，token 一次都不出现', async t => {
  const { socket } = await connectedBridge(t)
  const url = new URL(socket.url)
  assert.equal(url.searchParams.get('ticket'), 'ticket-1')
  assert.ok(url.searchParams.get('device_id'))
  assert.equal(url.searchParams.has('token'), false, 'token 必须从 URL 里消失')
  assert.equal(socket.url.includes('token-abc'), false)
})

test('桥：拿不到票据就不连接（fail-closed，不退回 query token）', async t => {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token-abc',
    log: () => {},
    reconnectMs: 10,
    exchangeTicket: async () => ({ ok: false, message: 'ticket endpoint returned 500' }),
  })
  await bridge.start('http://127.0.0.1:9')
  await sleep(50)
  assert.equal(instances.length, 0, '没有票据就不该建立任何连接')
  assert.match(bridge.status().lastError, /ticket endpoint returned 500/)
  bridge.stop()
})

test('桥：票据被撤销（401）后进入 revoked 且不再重连', async t => {
  const instances = installFakeWebSocket(t)
  const bridge = createDeviceFSBridge({
    app: fakeApp(t),
    readToken: async () => 'token-abc',
    log: () => {},
    reconnectMs: 5,
    maxReconnectMs: 10,
    exchangeTicket: async () => ({ ok: false, revoked: true, message: 'device bridge unauthorized' }),
  })
  await bridge.start('http://127.0.0.1:9')
  await sleep(80)
  assert.equal(instances.length, 0)
  assert.equal(bridge.status().revoked, true)
  assert.match(bridge.status().lastError, /unauthorized/)
  bridge.stop()
})

test('桥：服务端以 4401/4403 关闭连接时停止重连（撤销与 HTTP 一致）', async t => {
  const { bridge, socket, instances } = await connectedBridge(t, { maxReconnectMs: 10 })
  socket.close(4401)
  await sleep(80)
  assert.equal(bridge.status().connected, false)
  assert.equal(bridge.status().revoked, true)
  assert.equal(instances.length, 1, '撤销后不得重连')
  bridge.stop()
})

test('桥：普通断连仍然重连（撤销语义只对 401/403/4401/4403 生效）', async t => {
  const { bridge, socket, instances } = await connectedBridge(t, { maxReconnectMs: 10 })
  socket.close(1006)
  await waitFor(() => instances.length === 2, '重连出第二条连接')
  assert.equal(bridge.status().revoked, false)
  bridge.stop()
})

test('桥：token 变化后重新换取票据并清除 revoked', async t => {
  const { bridge, socket, instances } = await connectedBridge(t, { maxReconnectMs: 10 })
  socket.close(4403)
  assert.equal(bridge.status().revoked, true)
  bridge.notifyTokenChanged()
  await waitFor(() => instances.length === 2, 'token 变化后重新连接')
  assert.equal(bridge.status().revoked, false)
  bridge.stop()
})

test('main.cjs：桥注入批准名单、票据由主进程换取，根批准只能由主进程弹窗产生', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  assert.match(source, /createDeviceFSBridge\(\{[\s\S]{0,900}isRootGranted:/,
    '设备桥必须注入用户批准的根名单')
  assert.match(source, /requestRootApproval:[\s\S]{0,400}confirmProjectRootApproval/,
    '未授权根必须由主进程弹对话框')
  assert.match(source, /projectRoots\.covers/, '根授权判定要走批准名单')
})

// ── Git：别名/分页器也从仓库配置里清掉 ─────────────────────────────────────

function fakeSpawnGit(responses) {
  const calls = []
  const spawnGit = (command, args, options) => {
    calls.push({ command, args: [...args], options })
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => {}
    const response = responses.shift() || { code: 0, stdout: '' }
    queueMicrotask(() => {
      if (response.stdout) child.stdout.emit('data', Buffer.from(response.stdout))
      if (response.stderr) child.stderr.emit('data', Buffer.from(response.stderr))
      child.emit('close', response.code == null ? 0 : response.code)
    })
    return child
  }
  return { calls, spawnGit }
}

test('git：仓库里的 alias 与 pager 会被显式清空（白名单之外不留后门）', async t => {
  const root = tempProject(t)
  const fake = fakeSpawnGit([
    { code: 0, stdout: 'alias.status\n!evil-command\0pager.status\nmalicious-pager\0' },
    { code: 0, stdout: '' },
  ])
  const result = await runDeviceGit(root, ['status', '--porcelain=v1', '-z'], 1000, fake)
  assert.equal(result.code, 0, result.stderr)
  const execution = fake.calls[1].args
  assert.ok(execution.includes('alias.status='), execution.join(' '))
  assert.ok(execution.includes('pager.status=cat'), execution.join(' '))
})

test('git：最小环境 + 天花板目录，主进程环境与上层仓库都进不来', async t => {
  const root = tempProject(t)
  const fake = fakeSpawnGit([{ code: 0, stdout: '' }, { code: 0, stdout: '' }])
  process.env.CHIRON_H46_SECRET = 'must-not-leak'
  t.after(() => { delete process.env.CHIRON_H46_SECRET })
  const result = await runDeviceGit(root, ['status', '--porcelain=v1', '-z'], 1000, fake)
  assert.equal(result.code, 0, result.stderr)
  for (const call of fake.calls) {
    assert.equal(call.options.env.CHIRON_H46_SECRET, undefined)
    assert.equal(call.options.env.GIT_CONFIG_GLOBAL, process.platform === 'win32' ? 'NUL' : os.devNull)
    assert.equal(call.options.env.GIT_CEILING_DIRECTORIES, path.dirname(path.resolve(root)))
  }
})
