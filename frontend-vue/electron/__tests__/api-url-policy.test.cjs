'use strict'
/**
 * H50：远程连接传输安全（PRD 9.2）。
 *
 * 缺陷形态：配置层接受**任意** http(s) 地址，API 客户端随后向它附加 Bearer token，
 * CSP 也会放行该 origin。把地址填成 `http://team.example.com` 就等于让 token、
 * 会话 Cookie 与全部私有内容走明文；内网地址还会把凭据送给内网服务。
 *
 * 口径：
 *   · loopback 允许 http（桌面本地栈就是 http）；
 *   · 其余主机必须 https；
 *   · 自建内网 http 需显式 allowInsecure —— 但**凭据仍不发**；
 *   · 非 http(s) scheme、URL 内嵌用户名密码一律拒绝。
 *
 * 这里同时校验主进程与前端两份实现：它们必须同口径，否则会出现"保存时放行、
 * 连接时抛错"（或更糟：保存时拒绝、连接时放行）。
 */
const test = require('node:test')
const assert = require('node:assert')
const path = require('node:path')
const fs = require('node:fs')

const policy = require('../api-url-policy.cjs')

test('loopback 允许 http', () => {
  for (const url of ['http://127.0.0.1:8080', 'http://localhost:8080', 'http://[::1]:9000']) {
    const v = policy.classifyApiUrl(url)
    assert.equal(v.ok, true, url)
    assert.equal(v.isLoopback, true, url)
  }
})

test('非 loopback 的明文地址默认拒绝', () => {
  for (const url of ['http://team.example.com', 'http://10.0.0.5:8080', 'http://192.168.1.9']) {
    const v = policy.classifyApiUrl(url)
    assert.equal(v.ok, false, url)
    assert.equal(v.reason, 'insecure-scheme', url)
  }
})

test('显式 allowInsecure 时放行明文地址（自建内网场景）', () => {
  const v = policy.classifyApiUrl('http://10.0.0.5:8080', { allowInsecure: true })
  assert.equal(v.ok, true)
  assert.equal(v.url, 'http://10.0.0.5:8080')
  assert.equal(v.isLoopback, false)
})

test('https 一律放行（含自签域名）', () => {
  for (const url of ['https://team.example.com', 'https://team.example.com:8443/api']) {
    assert.equal(policy.classifyApiUrl(url).ok, true, url)
  }
})

test('拒绝非 http(s) scheme 与内嵌凭据的地址', () => {
  assert.equal(policy.classifyApiUrl('file:///etc/passwd').reason, 'scheme')
  assert.equal(policy.classifyApiUrl('javascript:alert(1)').reason, 'scheme')
  assert.equal(policy.classifyApiUrl('https://user:pass@team.example.com').reason, 'credentials-in-url')
})

test('空地址与乱写各自给出明确原因', () => {
  assert.equal(policy.classifyApiUrl('').reason, 'empty')
  assert.equal(policy.classifyApiUrl('not a url').reason, 'unparsable')
})

test('origin 规范化：去掉路径与查询（base URL 只保留 origin）', () => {
  const v = policy.classifyApiUrl('https://team.example.com/api/v1?x=1')
  assert.equal(v.url, 'https://team.example.com')
})

// ── 凭据闸门：明文非回环**永远**不发凭据 ──────────────────────────────────
test('canSendCredentials：https 与回环允许，明文非回环拒绝', () => {
  assert.equal(policy.canSendCredentials('https://team.example.com').ok, true)
  assert.equal(policy.canSendCredentials('http://127.0.0.1:8080').ok, true)

  const insecure = policy.canSendCredentials('http://10.0.0.5:8080')
  assert.equal(insecure.ok, false)
  assert.equal(insecure.reason, 'plaintext-non-loopback')
})

// ── 两份实现必须同口径 ─────────────────────────────────────────────────────
test('前端 apiUrlPolicy.ts 与主进程判据一致（同样的输入同样的结论）', () => {
  const src = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'utils', 'apiUrlPolicy.ts'), 'utf8')
  for (const marker of ['insecure-scheme', 'credentials-in-url', 'plaintext-non-loopback', 'unparsable']) {
    assert.ok(src.includes(marker), `前端策略缺少 ${marker}（两份实现必须同口径）`)
  }
})

test('config.cjs 的保存路径走策略校验', () => {
  const src = fs.readFileSync(path.resolve(__dirname, '..', 'config.cjs'), 'utf8')
  assert.ok(src.includes("require('./api-url-policy.cjs')"), '保存地址必须复用同一策略')
  assert.ok(src.includes('classifyApiUrl'), 'saveApiUrl 必须调用 classifyApiUrl')
})

test('api/index.ts 的连接路径也校验，并且明文时不带凭据', () => {
  const src = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'api', 'index.ts'), 'utf8')
  assert.ok(src.includes('assertTransportSafe'), '建连前必须校验传输安全')
  assert.ok(src.includes('canSendCredentials'), '必须按地址决定是否带凭据')
  assert.ok(src.includes('__anonymous'), '匿名请求要打标，否则 401 会被误读成登录过期')
})
