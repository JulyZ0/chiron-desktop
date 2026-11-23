'use strict'
/**
 * 回归测试：检查更新在"最新版是 pre-release"时必须还能用。
 *
 * 现场：仓库里的 Release 全部是 pre-release（未签名安装包刻意不标成 latest），
 * 于是客户端读的 `releases/latest/download/latest.yml` 对所有版本都 404，
 * 界面只能报"更新清单尚未发布"。真正的判据应是发布列表 + 版本号比较。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const {
  parseLatestYml,
  compareVersions,
  pickLatestRelease,
  manifestUrlForTag,
  installerUrlForTag,
} = require('../update-check.cjs')

const MANIFEST = `version: 0.261123.2
files:
  - url: Chiron-0.261123.2-win-x64.exe
    sha512: abcDEF==
    size: 260574391
path: Chiron-0.261123.2-win-x64.exe
sha512: abcDEF==
releaseDate: '2026-11-23T00:30:00.000Z'
`

function release(overrides) {
  return {
    tag_name: 'V0.261123.02',
    html_url: 'https://github.com/o/r/releases/tag/V0.261123.02',
    draft: false,
    prerelease: true,
    assets: [{ name: 'latest.yml' }, { name: 'Chiron-0.261123.2-win-x64.exe' }],
    ...overrides,
  }
}

test('清单解析：取 x64 安装包与 sha512', () => {
  const manifest = parseLatestYml(MANIFEST)
  assert.equal(manifest.version, '0.261123.2')
  assert.equal(manifest.file.url, 'Chiron-0.261123.2-win-x64.exe')
  assert.equal(manifest.file.sha512, 'abcDEF==')
})

test('版本比较：容忍 V 前缀与 tag 里的前导零', () => {
  assert.equal(compareVersions('V0.261123.02', '0.261123.2'), 0)
  assert.equal(compareVersions('V0.261124.01', '0.261123.02'), 1)
  assert.equal(compareVersions('0.261111.01', 'V0.261123.02'), -1)
})

test('★ 发布选择：一个稳定版都没有时，必须接受 pre-release', () => {
  const picked = pickLatestRelease([
    release({ tag_name: 'V0.261122.02' }),
    release({ tag_name: 'V0.261123.02' }),
  ])
  assert.equal(picked.tag, 'V0.261123.02')
})

test('发布选择：按版本号最大者胜出，预发布标记不参与排序', () => {
  const picked = pickLatestRelease([
    release({ tag_name: 'V0.261123.02', prerelease: false }),
    release({ tag_name: 'V0.261130.01', prerelease: true }),
  ])
  assert.equal(picked.tag, 'V0.261130.01', '预发布只影响 /releases/latest，不影响能不能装')
})

test('发布选择：排除草稿与缺少 latest.yml 的版本', () => {
  const picked = pickLatestRelease([
    release({ tag_name: 'V0.261130.01', draft: true }),
    release({ tag_name: 'V0.261129.01', assets: [{ name: 'Chiron.exe' }] }),
    release({ tag_name: 'V0.261123.02' }),
  ])
  assert.equal(picked.tag, 'V0.261123.02')
  assert.equal(pickLatestRelease([]), null)
  assert.equal(pickLatestRelease(null), null)
})

test('地址：清单与安装包都走 tag 固定路径；无 tag 时沿用 latest', () => {
  assert.equal(
    manifestUrlForTag('o/r', 'V0.261123.02'),
    'https://github.com/o/r/releases/download/V0.261123.02/latest.yml',
  )
  assert.equal(
    installerUrlForTag('o/r', 'V0.261123.02', 'Chiron-0.261123.2-win-x64.exe'),
    'https://github.com/o/r/releases/download/V0.261123.02/Chiron-0.261123.2-win-x64.exe',
  )
  assert.equal(
    installerUrlForTag('o/r', '', 'Chiron-0.261123.2-win-x64.exe'),
    'https://github.com/o/r/releases/latest/download/Chiron-0.261123.2-win-x64.exe',
  )
})

test('main.cjs：latest.yml 404 时必须回退到发布列表，而不是直接报错', () => {
  const source = fs
    .readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
    .replace(/\r\n/g, '\n')
  const start = source.indexOf('async function checkAppUpdate()')
  assert.ok(start > 0)
  const body = source.slice(start, source.indexOf('\n}\n', start))
  assert.match(body, /err\?\.status === 404/)
  assert.match(body, /checkUpdateViaReleaseList\(current\)/)
})
