'use strict'
/**
 * H45：更新清单与安装器真实性门禁（PRD 9.2）。
 *
 * 缺陷形态：
 *   1. 清单允许**空 `sha512`**，主进程只在摘要非空时才校验，随后直接启动安装器 ——
 *      "没有摘要"等于"免检"；
 *   2. 缓存包没有摘要也会被再次启动（下载后校验一次，之后不再看）；
 *   3. 没有发布者签名：清单来源被换掉（镜像/劫持/被改的 release）时，
 *      安装器就是"看起来像官方"的任意二进制；
 *   4. 文件名/大小/架构不做 schema 校验，重定向也不限制域名。
 *
 * 口径是**默认拒绝**：缺字段、超范围、域名不对、摘要不匹配、无签名 —— 一律不执行。
 */
const test = require('node:test')
const assert = require('node:assert')
const crypto = require('node:crypto')

const gate = require('../update-manifest.cjs')

const SHA = crypto.createHash('sha512').update('payload').digest('base64')

function manifest(over = {}) {
  return {
    version: '1.2.3',
    file: {
      url: 'https://github.com/athenavi/chiron/releases/download/v1.2.3/Chiron-1.2.3-x64.exe',
      size: 1024,
      sha512: SHA,
      ...(over.file || {}),
    },
    ...over.rest,
  }
}

test('合格清单通过校验', () => {
  const r = gate.validateLatestManifest(manifest(), { platform: 'win32', arch: 'x64' })
  assert.equal(r.ok, true, JSON.stringify(r.errors))
  assert.equal(r.manifest.version, '1.2.3')
})

test('electron-builder 的相对文件名清单也通过（URL 由可信 repo 拼接）', () => {
  const r = gate.validateLatestManifest(
    manifest({ file: { url: 'Chiron-1.2.3-win-x64.exe' } }),
    { platform: 'win32', arch: 'x64' },
  )
  assert.equal(r.ok, true, JSON.stringify(r.errors))
})

test('真实 latest.yml 形状（files[].url 为文件名）能通过完整解析+校验', () => {
  const { parseLatestYml } = require('../update-check.cjs')
  const text = [
    'version: 0.261127.01',
    'files:',
    '  - url: Chiron-Setup-0.261127.01.exe',
    `    sha512: ${SHA}`,
    '    size: 123456',
    'path: Chiron-Setup-0.261127.01.exe',
    `sha512: ${SHA}`,
    'releaseDate: 2026-11-27T00:00:00.000Z',
  ].join('\n')
  const r = gate.validateLatestManifest(parseLatestYml(text), {
    platform: 'win32',
    arch: 'x64',
  })
  assert.equal(r.ok, true, JSON.stringify(r.errors))
  assert.equal(r.manifest.version, '0.261127.01')
})

test('相对文件名带路径穿越 ⇒ 拒绝', () => {
  for (const url of ['../evil.exe', 'sub/../../evil.exe', '..%2Fevil.exe']) {
    const r = gate.validateLatestManifest(manifest({ file: { url } }), { platform: 'win32' })
    assert.equal(r.ok, false, url)
    assert.ok(r.errors.includes('installer-name-unsafe'), `${url}: ${r.errors}`)
  }
})

test('缺 sha512 ⇒ 拒绝（这是本条的核心）', () => {
  const r = gate.validateLatestManifest(manifest({ file: { sha512: '' } }), { platform: 'win32' })
  assert.equal(r.ok, false)
  assert.ok(r.errors.includes('sha512-missing'))
})

test('sha512 形状不合法（长度/编码不对）⇒ 拒绝', () => {
  for (const bad of ['abc', 'not-base64!!', Buffer.from('short').toString('base64')]) {
    const r = gate.validateLatestManifest(manifest({ file: { sha512: bad } }), { platform: 'win32' })
    assert.equal(r.ok, false, `应拒绝 ${bad}`)
    assert.ok(r.errors.includes('sha512-invalid'))
  }
})

test('安装包域名不在白名单 ⇒ 拒绝', () => {
  for (const url of [
    'https://evil.example/Chiron-1.2.3-x64.exe',
    'http://github.com/a.exe',
    'https://github.com.evil.example/a.exe',
  ]) {
    const r = gate.validateLatestManifest(manifest({ file: { url } }), { platform: 'win32' })
    assert.equal(r.ok, false, `应拒绝 ${url}`)
    assert.ok(r.errors.includes('installer-host-not-allowed'), url)
  }
})

test('非 exe/msi 扩展名 ⇒ 拒绝（Windows）', () => {
  const r = gate.validateLatestManifest(
    manifest({ file: { url: 'https://github.com/a/Chiron-1.2.3-x64.zip' } }),
    { platform: 'win32' },
  )
  assert.equal(r.ok, false)
  assert.ok(r.errors.includes('installer-extension'))
})

test('文件名带路径片段 ⇒ 拒绝（防目录穿越落盘）', () => {
  const r = gate.validateLatestManifest(
    manifest({ file: { url: 'https://github.com/a/..%2F..%2Fevil.exe' } }),
    { platform: 'win32' },
  )
  assert.equal(r.ok, false)
})

test('大小非正整数 ⇒ 拒绝（显式写了才校验）', () => {
  for (const size of [0, -1, 1.5, 'x']) {
    const r = gate.validateLatestManifest(manifest({ file: { size } }), { platform: 'win32' })
    assert.equal(r.ok, false, `应拒绝 size=${size}`)
    assert.ok(r.errors.includes('size-invalid'))
  }
})

test('缺 size 的清单通过（electron-builder 26 的真实形状）', () => {
  // electron-builder 26 起 latest.yml 的 files[] 只有 url + sha512，没有 size
  // （app-builder-lib out/publish/updateInfoBuilder.js::createUpdateInfo）。
  // 若把缺省 size 判成非法，每一次更新检查都会失败（线上表现为 size-invalid）。
  const { parseLatestYml } = require('../update-check.cjs')
  const text = [
    'version: 0.261127.2',
    'files:',
    '  - url: Chiron-0.261127.2-win-x64.exe',
    `    sha512: ${SHA}`,
    'path: Chiron-0.261127.2-win-x64.exe',
    `sha512: ${SHA}`,
    "releaseDate: '2026-11-27T08:34:24.711Z'",
  ].join('\n')
  const r = gate.validateLatestManifest(parseLatestYml(text), {
    platform: 'win32',
    arch: 'x64',
  })
  assert.equal(r.ok, true, JSON.stringify(r.errors))
  assert.equal(r.manifest.file.url, 'Chiron-0.261127.2-win-x64.exe')
})

test('架构不匹配 ⇒ 拒绝（避免装错包）', () => {
  const arm = gate.validateLatestManifest(
    manifest({ file: { url: 'https://github.com/a/Chiron-1.2.3-arm64.exe' } }),
    { platform: 'win32', arch: 'x64' },
  )
  assert.equal(asBool(arm.ok), false)
  assert.ok(arm.errors.includes('arch-mismatch'))

  const x64 = gate.validateLatestManifest(
    manifest({ file: { url: 'https://github.com/a/Chiron-1.2.3-x64.exe' } }),
    { platform: 'win32', arch: 'arm64' },
  )
  assert.equal(x64.ok, false)
  assert.ok(x64.errors.includes('arch-mismatch'))
})

test('版本号必须是 semver（拒绝 latest / 带路径的伪版本）', () => {
  for (const version of ['', 'latest', '../1.2.3', '1.2.3/../x']) {
    const r = gate.validateLatestManifest(manifest({ rest: { version } }), { platform: 'win32' })
    assert.equal(r.ok, false, `应拒绝版本 ${JSON.stringify(version)}`)
    assert.ok(r.errors.includes('version-invalid'))
  }
})

function asBool(v) {
  return Boolean(v)
}

// ── 摘要比对 ───────────────────────────────────────────────────────────────
test('digestMatches：缺摘要一律 false（不得"没有摘要就算通过"）', () => {
  const buf = Buffer.from('payload')
  assert.equal(gate.digestMatches(buf, ''), false)
  assert.equal(gate.digestMatches(buf, undefined), false)
  assert.equal(gate.digestMatches(buf, SHA), true)
  assert.equal(gate.digestMatches(Buffer.from('tampered'), SHA), false)
})

// ── 签名 ───────────────────────────────────────────────────────────────────
function keypair() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519')
  return {
    publicKey: publicKey.export({ format: 'der', type: 'spki' }).toString('base64'),
    sign: (buf) => crypto.sign(null, buf, privateKey).toString('base64'),
  }
}

test('未配置公钥 ⇒ no-public-key（fail-closed，不是"跳过签名"）', () => {
  const r = gate.verifySignature(Buffer.from('x'), 'AAAA', '')
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'no-public-key')
})

test('正确签名通过；签名被改/公钥不匹配则拒绝', () => {
  const kp = keypair()
  const payload = Buffer.from('installer-bytes')
  const sig = kp.sign(payload)

  assert.equal(gate.verifySignature(payload, sig, kp.publicKey).ok, true)
  assert.equal(gate.verifySignature(Buffer.from('tampered'), sig, kp.publicKey).ok, false)

  const other = keypair()
  assert.equal(gate.verifySignature(payload, sig, other.publicKey).ok, false)
})

test('签名字段缺失 ⇒ missing-signature', () => {
  const kp = keypair()
  const r = gate.verifySignature(Buffer.from('x'), '', kp.publicKey)
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'missing-signature')
})

// ── 启动门禁 ───────────────────────────────────────────────────────────────
test('gateInstallerLaunch：摘要不匹配即拒绝（先于签名）', () => {
  const kp = keypair()
  const buf = Buffer.from('payload')
  const r = gate.gateInstallerLaunch({
    buffer: buf,
    manifest: { file: { sha512: crypto.createHash('sha512').update('other').digest('base64') } },
    installerSignature: kp.sign(buf),
    publicKey: kp.publicKey,
  })
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'sha512-mismatch')
})

test('gateInstallerLaunch：摘要通过但无公钥 ⇒ 拒绝启动（no-public-key）', () => {
  const buf = Buffer.from('payload')
  const r = gate.gateInstallerLaunch({
    buffer: buf,
    manifest: { file: { sha512: SHA } },
    installerSignature: 'AAAA',
    publicKey: '',
  })
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'no-public-key')
})

test('gateInstallerLaunch：摘要 + 签名都通过才放行', () => {
  const kp = keypair()
  const buf = Buffer.from('payload')
  const r = gate.gateInstallerLaunch({
    buffer: buf,
    manifest: { file: { sha512: SHA } },
    installerSignature: kp.sign(buf),
    publicKey: kp.publicKey,
  })
  assert.equal(r.ok, true)
})

test('gateInstallerLaunch：空 buffer / 缺摘要 各自拒绝', () => {
  assert.equal(gate.gateInstallerLaunch({}).reason, 'installer-missing')
  assert.equal(
    gate.gateInstallerLaunch({ buffer: Buffer.from('x'), manifest: { file: {} } }).reason,
    'sha512-missing',
  )
})

// ── 源码守卫：主进程不得再"没有摘要就放行" ─────────────────────────────────
test('main.cjs 的缓存包复用路径必须要求摘要存在', () => {
  const fs = require('node:fs')
  const path = require('node:path')
  const src = fs.readFileSync(path.resolve(__dirname, '..', 'main.cjs'), 'utf8')
  // 只查字符串会被"从错误模块解构"骗过：函数名出现在 require 列表里，运行时却是
  // undefined（线上表现为 `validateLatestManifest is not a function`）。这里必须
  // 确认两个函数是从**定义它们的模块**取出来的。
  const block = src.match(
    /const\s*\{([\s\S]*?)\}\s*=\s*require\('\.\/update-manifest\.cjs'\)/,
  )
  assert.ok(block, 'main.cjs 必须从 update-manifest.cjs 取真实性门禁')
  assert.ok(block[1].includes('validateLatestManifest'), '清单校验必须来自 update-manifest.cjs')
  assert.ok(block[1].includes('gateInstallerLaunch'), '安装门禁必须来自 update-manifest.cjs')
  assert.ok(
    !/const\s*\{[\s\S]*?validateLatestManifest[\s\S]*?\}\s*=\s*require\('\.\/update-check\.cjs'\)/.test(src),
    'update-check.cjs 不导出 validateLatestManifest，不得从那里解构',
  )
  assert.ok(
    !/if \(!job\.sha512 \|\| \(await sha512File\(final\)\) === job\.sha512\)/.test(src),
    '不得保留「没有摘要也放行」的旧判据',
  )
})
