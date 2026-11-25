'use strict'
/**
 * H57：更新渠道与版本语义（PRD 9.8）。
 *
 * 缺陷形态：
 *   1. `compareVersions` 只比数字段：`2.0.0-beta` 与 `2.0.0` 判等 ⇒
 *      **预发布版本永远拿不到更新**，而本仓的发版常态就是 prerelease；
 *   2. `pickLatestRelease` 无条件取"版本号最大者" ⇒
 *      清单里混进旧 tag 时会把用户**降级**；撤回（withdrawal）的包也照样会被选中；
 *   3. 没有 channel / pin 语义：stable 渠道会收到 prerelease，pin 住的版本会被"升级"。
 *
 * 这一组用纯函数把三条钉住，全部不联网。
 */
const test = require('node:test')
const assert = require('node:assert')

const update = require('../update-check.cjs')

const release = (tag, extra = {}) => ({
  tag_name: tag,
  draft: false,
  prerelease: /-/.test(tag),
  url_html: '',
  assets: [{ name: 'latest.yml' }],
  ...extra,
})

test('compareVersions 遵循 semver 优先级（含 prerelease）', () => {
  const cases = [
    ['1.0.0', '1.0.1', -1],
    ['1.0.1', '1.0.0', 1],
    ['1.0.0', '1.0.0', 0],
    ['2.0.0-beta', '2.0.0', -1], // 旧实现：0（判等 ⇒ 永不升级）
    ['2.0.0', '2.0.0-beta', 1],
    ['2.0.0-beta.2', '2.0.0-beta.10', -1], // 数字段按数值：beta.2 < beta.10
    ['2.0.0-beta.1', '2.0.0-beta', 1], // 前缀相同 ⇒ 更长者更高
    ['2.0.0-alpha', '2.0.0-beta', -1], // 非数字标识符按字典序
    ['2.0.0-1', '2.0.0-alpha', -1], // 数字标识符 < 字母标识符
    ['1.2.3+build.9', '1.2.3+build.1', 0], // build 元数据不参与优先级
    ['v1.2.3', '1.2.3', 0], // 前缀 v 容错
    ['1.2', '1.2.0', 0], // 两位写法容错
  ]
  for (const [a, b, want] of cases) {
    assert.equal(update.compareVersions(a, b), want, `${a} vs ${b}`)
  }
})

test('compareVersions 对不可解析输入不声称谁更高', () => {
  // 旧实现会把垃圾 tag 当 0.0.0：`nightly` 与 `1.0.0` 比出 -1，直接导致降级判断失误。
  assert.equal(update.compareVersions('nightly', '1.0.0'), 0)
  assert.equal(update.compareVersions('', '1.0.0'), 0)
})

test('parseSemver 拆出三段与预发布/build', () => {
  assert.deepEqual(update.parseSemver('v3.4.5-rc.1+sha.abc'), {
    ok: true,
    raw: '3.4.5-rc.1+sha.abc',
    major: 3,
    minor: 4,
    patch: 5,
    prerelease: ['rc', '1'],
    build: 'sha.abc',
  })
  assert.equal(update.isPrereleaseVersion('1.0.0-rc.1'), true)
  assert.equal(update.isPrereleaseVersion('1.0.0'), false)
  assert.equal(update.isPrereleaseVersion('garbage'), false)
})

test('pickLatestRelease 不会把用户降级到旧 tag', () => {
  const releases = [release('1.0.0'), release('0.9.0'), release('1.1.0-beta.1')]
  const picked = update.pickLatestRelease(releases, { current: '1.2.0' })
  assert.equal(picked, null, '没有比当前更新的版本时必须返回 null（不能降级）')
})

test('pickLatestRelease 在旧实现里会选错：prerelease 比正式版新', () => {
  const releases = [release('2.0.0'), release('2.1.0-beta.1')]
  const picked = update.pickLatestRelease(releases, { current: '2.0.0' })
  assert.equal(picked.tag, '2.1.0-beta.1')
})

test('channel=stable 过滤 prerelease；默认允许（本仓发版常态）', () => {
  const releases = [release('2.0.0'), release('2.1.0-beta.1'), release('2.2.0')]
  // 默认（不传 channel）= 接受 prerelease，取版本号最大者。
  assert.equal(update.pickLatestRelease(releases, { current: '2.0.0' }).tag, '2.2.0')
  // 只有 prerelease 比当前新时，默认渠道会拿到它。
  assert.equal(
    update.pickLatestRelease([release('2.0.0'), release('2.1.0-beta.1')], { current: '2.0.0' }).tag,
    '2.1.0-beta.1',
  )
  // 显式 stable：prerelease 被过滤；且"过滤后没有更新的版本" ⇒ null（不是降级到 2.0.0）。
  assert.equal(
    update.pickLatestRelease([release('2.0.0'), release('2.1.0-beta.1')], { current: '2.0.0', channel: 'stable' }),
    null,
  )
  // stable 下仍有更新的正式版时正常返回。
  assert.equal(
    update.pickLatestRelease([release('2.2.0'), release('2.3.0-beta.1')], { current: '2.0.0', channel: 'stable' }).tag,
    '2.2.0',
  )
})

test('被撤回的版本不进候选', () => {
  const releases = [release('2.0.0'), release('2.2.0')]
  // 撤掉最新的那个 ⇒ 只剩"当前版本"，按不降级规则返回 null（绝不回退到 2.0.0 重装）。
  assert.equal(
    update.pickLatestRelease(releases, { current: '2.0.0', withdrawn: ['v2.2.0'] }),
    null,
    '撤回名单必须按 semver 规范化比较（v2.2.0 与 2.2.0 是同一个版本）',
  )
  // 撤掉最新的、但还有次新的 ⇒ 返回次新。
  const three = [release('2.0.0'), release('2.1.0'), release('2.2.0')]
  assert.equal(
    update.pickLatestRelease(three, { current: '2.0.0', withdrawn: ['2.2.0'] }).tag,
    '2.1.0',
  )
})

test('pin：只返回被 pin 的那个版本，否则视为无需升级', () => {
  const releases = [release('2.0.0'), release('2.3.0')]
  assert.equal(update.pickLatestRelease(releases, { current: '2.0.0', pin: '2.0.0' }).tag, '2.0.0')
  assert.equal(update.pickLatestRelease(releases, { current: '2.0.0', pin: '1.9.0' }), null)
})

test('草稿与缺清单的 release 一律不参与', () => {
  const releases = [
    release('2.0.0', { draft: true }),
    { tag_name: '2.1.0', draft: false, assets: [{ name: 'notes.txt' }] },
    release('1.5.0'),
  ]
  assert.equal(update.pickLatestRelease(releases, { current: '1.0.0' }).tag, '1.5.0')
})

test('空列表返回 null（不能抛）', () => {
  assert.equal(update.pickLatestRelease([], { current: '1.0.0' }), null)
  assert.equal(update.pickLatestRelease(null), null)
})
