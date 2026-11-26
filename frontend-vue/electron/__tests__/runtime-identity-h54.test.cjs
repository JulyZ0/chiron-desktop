'use strict'
/**
 * H54：本机栈身份挑战（PRD 9.7）。
 *
 * 缺陷形态：外壳判断"本机栈还在跑"只看 loopback `/ready` 返回 200 且 postgres/redis
 * 都 up。于是**任何**在这台机器上起过、恰好也连了 PG/Redis 的服务都会被当成"我们的栈"：
 *   - 端口被另一个 data dir 的 Chiron 实例占着 → 接管错误的后端，本地数据写进别人的库；
 *   - 端口被第三方服务占着但它也应答 `/ready` → 更糟。
 *
 * 修法：本地启动时注入实例 ID / 协议版本 / data dir 指纹，`/ready` 原样回显，
 * 外壳逐项比对；任一不符即拒绝接管（未知服务不得覆盖）。状态文件里没有 instanceId
 * 的旧部署也一律不认 —— 无法证明的归属等于没有归属。
 */
const test = require('node:test')
const assert = require('node:assert')

const rs = require('../runtime-state.cjs')

/** 造一个只应答 /ready 的 fetch 桩。 */
function readyFetch(payload, opts = {}) {
  const calls = []
  return {
    calls,
    fetch: async (url) => {
      calls.push(String(url))
      return {
        ok: opts.ok !== false,
        json: async () => ({ success: true, data: payload }),
      }
    },
  }
}

const baseState = {
  schema: rs.STATE_SCHEMA,
  instanceId: 'inst-1',
  instanceProtocol: rs.INSTANCE_PROTOCOL,
  gatewayPort: 18900,
}

test('实例 ID 一致才接管', async () => {
  const stub = readyFetch({ postgres: 'up', redis: 'up', instance_id: 'inst-1', protocol: rs.INSTANCE_PROTOCOL })
  const result = await rs.probeExistingStack(baseState, { fetch: stub.fetch })
  assert.equal(result.alive, true)
  assert.equal(result.port, 18900)
})

test('实例 ID 不同 ⇒ 拒绝接管（这是本条的核心）', async () => {
  const stub = readyFetch({ postgres: 'up', redis: 'up', instance_id: 'someone-else', protocol: rs.INSTANCE_PROTOCOL })
  const result = await rs.probeExistingStack(baseState, { fetch: stub.fetch })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'instance-mismatch')
})

test('服务端不回显实例 ID ⇒ 视为无法证明（旧网关）', async () => {
  const stub = readyFetch({ postgres: 'up', redis: 'up' })
  const result = await rs.probeExistingStack(baseState, { fetch: stub.fetch })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'identity-not-echoed')
})

test('状态文件里没有 instanceId（旧部署）⇒ 不认', async () => {
  const legacy = { schema: rs.STATE_SCHEMA, gatewayPort: 18900 }
  const stub = readyFetch({ postgres: 'up', redis: 'up', instance_id: 'inst-1' })
  const result = await rs.probeExistingStack(legacy, { fetch: stub.fetch })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'state-without-instance-id')
})

test('协议版本不一致 ⇒ 拒绝（语义可能已变，不能猜）', async () => {
  const stub = readyFetch({ postgres: 'up', redis: 'up', instance_id: 'inst-1', protocol: rs.INSTANCE_PROTOCOL + 1 })
  const result = await rs.probeExistingStack(baseState, { fetch: stub.fetch })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'protocol-mismatch')
})

test('依赖未就绪仍然优先报 dependencies-down（与身份无关）', async () => {
  const stub = readyFetch({ postgres: 'down', redis: 'up', instance_id: 'inst-1' })
  const result = await rs.probeExistingStack(baseState, { fetch: stub.fetch })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'dependencies-down')
})

test('写状态时自动带上实例身份', () => {
  const os = require('node:os')
  const path = require('node:path')
  const fs = require('node:fs')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-h54-'))
  try {
    const written = rs.writeRuntimeState(dir, { gatewayPort: 19001 })
    assert.ok(written, '写状态应成功')
    assert.match(String(written.instanceId), /^[0-9a-f]{32}$/, '实例 ID 应是 32 位十六进制')
    assert.equal(written.instanceProtocol, rs.INSTANCE_PROTOCOL)

    const readBack = rs.readRuntimeState(dir)
    assert.equal(readBack.instanceId, written.instanceId, '读回的身份必须一致')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('显式传入的 instanceId 不被覆盖（重启复用同一身份时用）', () => {
  const os = require('node:os')
  const path = require('node:path')
  const fs = require('node:fs')
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-h54b-'))
  try {
    const written = rs.writeRuntimeState(dir, { gatewayPort: 19002, instanceId: 'fixed-id' })
    assert.equal(written.instanceId, 'fixed-id')
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
