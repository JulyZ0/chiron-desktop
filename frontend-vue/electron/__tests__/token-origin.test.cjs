'use strict'
/**
 * H79：云端 Bearer 凭据必须绑定 API origin。
 *
 * 缺陷形态（PRD 9.10）：
 *   safeStorage 里只存裸 token，没有任何"这份凭据是哪个服务签发的"元数据。
 *   用户登录 A 服务后把服务地址改成 B：`desktop:set-api-url` 只写 `apiUrl` 并重启窗口，
 *   盘上 token 原样留着，下次启动直接把它当"云端登录态"恢复 ——
 *   于是 A 的 Bearer 被 Axios / SSE / 重试送到 B。B 拿到了别人的凭据，
 *   A 的会话也被泄露给一个不属于它的服务。
 *
 * 治法：凭据**只能**以 `{token, 规范化 origin}` 这一对存/取。规范化为
 * `scheme://host:port`（默认端口显式补全），路径/query 不参与 —— 同一服务换路径
 * 不该让用户重新登录，换域必须。
 *
 * 纯 Node 单测：不依赖 Electron（safeStorage 由 token-store 在下一层封装）。
 */
const test = require('node:test')
const assert = require('node:assert')

const origin = require('../token-origin.cjs')

test('normalizeOrigin 补全默认端口并忽略路径/query', () => {
  assert.equal(origin.normalizeOrigin('https://a.example.com'), 'https://a.example.com:443')
  assert.equal(origin.normalizeOrigin('https://a.example.com:443'), 'https://a.example.com:443')
  assert.equal(origin.normalizeOrigin('http://a.example.com'), 'http://a.example.com:80')
  assert.equal(origin.normalizeOrigin('https://A.Example.com/api/v1?x=1'), 'https://a.example.com:443')
  assert.equal(origin.normalizeOrigin('https://a.example.com:8443'), 'https://a.example.com:8443')
})

test('normalizeOrigin 拒绝无法确定认证域的输入', () => {
  for (const raw of ['', '   ', 'not-a-url', 'ftp://a.example.com', 'file:///etc/passwd', 'ws://a.example.com']) {
    assert.equal(origin.normalizeOrigin(raw), '', `应拒绝 ${JSON.stringify(raw)}`)
  }
})

test('sameOrigin：默认端口与显式端口视为同一认证域', () => {
  assert.equal(origin.sameOrigin('https://a.example.com', 'https://a.example.com:443'), true)
  assert.equal(origin.sameOrigin('https://a.example.com', 'https://a.example.com:8443'), false)
  assert.equal(origin.sameOrigin('http://a.example.com', 'https://a.example.com'), false)
  // 解析不了一律 false（fail-closed）：不确定就当成"不是同一个"。
  assert.equal(origin.sameOrigin('', 'https://a.example.com'), false)
})

test('落盘信封带 origin，且没有可信 origin 时拒绝生成', () => {
  const env = origin.encodeEnvelope({ token: 'tok-A', origin: 'https://a.example.com' })
  const parsed = JSON.parse(env)
  assert.equal(parsed.v, origin.ENVELOPE_VERSION)
  assert.equal(parsed.origin, 'https://a.example.com:443')
  assert.equal(parsed.token, 'tok-A')

  assert.throws(() => origin.encodeEnvelope({ token: 'tok-A' }), /origin/)
  assert.throws(() => origin.encodeEnvelope({ token: 'tok-A', origin: 'garbage' }), /origin/)
  assert.throws(() => origin.encodeEnvelope({ token: '', origin: 'https://a.example.com' }), /不能为空/)
})

test('decodeEnvelope 把旧版裸 token 判为 legacy（无法证明归属）', () => {
  const legacy = origin.decodeEnvelope('old-plain-token-without-json')
  assert.equal(legacy.ok, false)
  assert.equal(legacy.reason, 'legacy')

  const malformed = origin.decodeEnvelope('{"v":2,"token":"x"}') // 缺 origin
  assert.equal(malformed.ok, false)
  assert.equal(malformed.reason, 'malformed')

  const wrongVersion = origin.decodeEnvelope('{"v":1,"token":"x","origin":"https://a.example.com"}')
  assert.equal(wrongVersion.ok, false)
  assert.equal(wrongVersion.reason, 'version')
})

test('evaluateStoredToken：同域交还 token，跨域清盘，无期望域不交还也不清盘', () => {
  const stored = origin.encodeEnvelope({ token: 'tok-A', origin: 'https://a.example.com' })

  const same = origin.evaluateStoredToken(stored, 'https://a.example.com')
  assert.deepEqual(
    { token: same.token, clear: same.clear, reason: same.reason },
    { token: 'tok-A', clear: false, reason: 'ok' },
  )

  // 典型攻击场景：登录 A 之后把地址改成 B。
  const cross = origin.evaluateStoredToken(stored, 'https://b.example.com')
  assert.equal(cross.token, '')
  assert.equal(cross.clear, true, '跨域必须清盘，不能留给下一个服务')
  assert.equal(cross.reason, 'origin-mismatch')

  // 还没配置任何云端地址：不交还（fail-closed），但不销毁用户登录态。
  const unknown = origin.evaluateStoredToken(stored, '')
  assert.equal(unknown.token, '')
  assert.equal(unknown.clear, false)
  assert.equal(unknown.reason, 'no-expected-origin')

  // 旧版裸 token：无法证明归属 ⇒ 与"损坏"同等对待（清盘，重新登录一次）。
  const legacy = origin.evaluateStoredToken('plain-legacy-token', 'https://a.example.com')
  assert.equal(legacy.token, '')
  assert.equal(legacy.clear, true)

  // 空文件不是"旧凭据"，不该触发清盘动作（清也无可清）。
  const empty = origin.evaluateStoredToken('', 'https://a.example.com')
  assert.equal(empty.token, '')
  assert.equal(empty.clear, false)
})
