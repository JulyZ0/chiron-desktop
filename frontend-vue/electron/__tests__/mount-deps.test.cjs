'use strict'
/**
 * 团队挂载依赖管理器：清单必须带真实 SHA256；非 Windows 平台明确不支持，
 * 不静默降级；未知依赖拒绝。下载/安装路径需真实网络与 UAC，不在单测触发。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createMountDeps, MANIFEST } = require('../mount-deps.cjs')

function tempApp(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-mount-deps-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return { getPath: () => dir }
}

test('清单：版本、资产与 SHA256 齐全且格式正确', () => {
  for (const [name, dep] of Object.entries(MANIFEST)) {
    assert.ok(dep.version, `${name} 缺版本`)
    assert.ok(dep.asset, `${name} 缺资产名`)
    assert.match(dep.sha256, /^[a-f0-9]{64}$/, `${name} SHA256 格式不对`)
    assert.ok(dep.url.startsWith('https://github.com/'), `${name} 必须来自官方 releases`)
    assert.ok(dep.size > 0, `${name} 缺大小`)
  }
})

test('status：非 Windows 平台 supported=false，不尝试安装', async t => {
  const deps = createMountDeps({
    app: tempApp(t),
    net: { fetch: async () => { throw new Error('must not download') } },
    shell: { openPath: async () => '' },
    platform: 'linux',
    arch: 'x64',
  })
  const st = await deps.status()
  assert.equal(st.supported, false)
  const res = await deps.install('rclone')
  assert.equal(res.ok, false)
  assert.equal(res.reason, 'unsupported-platform')
})

test('install：未知依赖直接拒绝', async t => {
  const deps = createMountDeps({
    app: tempApp(t),
    net: { fetch: async () => { throw new Error('must not download') } },
    shell: { openPath: async () => '' },
    platform: 'win32',
    arch: 'x64',
  })
  const res = await deps.install('nope')
  assert.equal(res.ok, false)
  assert.equal(res.reason, 'unknown-dependency')
})
