'use strict'
/**
 * 引擎环境变量白名单的回归测试。
 *
 * 钉的是三条**安全与可用性**约束：
 *   1. `SUPERAGENT_TAKEOVER_ENABLED` 必须能真的下发给引擎（否则装了也永远关着）；
 *   2. 白名单之外的键必须被丢弃（否则等于开放任意 env 注入）；
 *   3. 显式 `false` 与"没配过"必须能区分（排查时这两件事完全不同）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const engineEnv = require('../engine-env.cjs')

const {
  ENGINE_ENV_FILE,
  ENGINE_TOGGLES,
  engineEnvPath,
  normalizeToggles,
  togglesToEnv,
  mergeIntoEnv,
  readEngineEnvSettings,
  writeEngineEnvSettings,
} = engineEnv

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-engine-env-'))
}

test('★ 回归：SUPERAGENT_TAKEOVER_ENABLED 在白名单里且能下发', () => {
  const env = togglesToEnv({ SUPERAGENT_TAKEOVER_ENABLED: true })

  assert.equal(env.SUPERAGENT_TAKEOVER_ENABLED, 'true')
})

test('★ 白名单之外的键一律丢弃：不能把安全边界交给用户', () => {
  const env = togglesToEnv({
    SUPERAGENT_TAKEOVER_ENABLED: true,
    APP_SECRET: 'hijacked',
    SANDBOX_BACKEND: 'none',
    SANDBOX_ISOLATION_ALLOW_DOWNGRADE: true,
    PATH: 'C:\\evil',
    PYTHONPATH: 'C:\\evil',
  })

  assert.deepEqual(Object.keys(env), ['SUPERAGENT_TAKEOVER_ENABLED'])
})

test('显式 false 也要下发：与"从没配过"必须可区分', () => {
  const env = togglesToEnv({ SUPERAGENT_TAKEOVER_ENABLED: false })

  assert.equal(env.SUPERAGENT_TAKEOVER_ENABLED, 'false')
})

test('没配过的开关不出现在 env 里（留给引擎默认值）', () => {
  assert.deepEqual(togglesToEnv({}), {})
  assert.deepEqual(togglesToEnv(undefined), {})
})

test('布尔接受字符串与数字（配置文件是用户可手改的）', () => {
  for (const truthy of [true, 1, '1', 'true', 'TRUE', ' on ', 'yes']) {
    assert.equal(
      normalizeToggles({ SUPERAGENT_TAKEOVER_ENABLED: truthy }).SUPERAGENT_TAKEOVER_ENABLED,
      true,
      `${String(truthy)} 应判真`,
    )
  }
  for (const falsy of [false, 0, '0', 'false', 'off', 'no']) {
    assert.equal(normalizeToggles({ SUPERAGENT_TAKEOVER_ENABLED: falsy }).SUPERAGENT_TAKEOVER_ENABLED, false)
  }
})

test('无法识别的值丢掉，不猜', () => {
  /* `"maybe"` 猜成 true 会让一个写错的配置"看起来生效了" —— 宁可当没配 */
  assert.deepEqual(normalizeToggles({ SUPERAGENT_TAKEOVER_ENABLED: 'maybe' }), {})
  assert.deepEqual(normalizeToggles({ SUPERAGENT_TAKEOVER_ENABLED: null }), {})
  assert.deepEqual(normalizeToggles({ SUPERAGENT_TAKEOVER_ENABLED: {} }), {})
})

test('mergeIntoEnv 不改入参（buildEngineEnv 换端口重试会复用同一个对象）', () => {
  const base = { APP_SECRET: 'keep' }
  const merged = mergeIntoEnv(base, { SUPERAGENT_TAKEOVER_ENABLED: true })

  assert.deepEqual(base, { APP_SECRET: 'keep' })
  assert.equal(merged.APP_SECRET, 'keep')
  assert.equal(merged.SUPERAGENT_TAKEOVER_ENABLED, 'true')
})

test('落盘后能原样读回', () => {
  const dir = tmpDir()
  writeEngineEnvSettings(dir, { SUPERAGENT_TAKEOVER_ENABLED: true, MEMORY_DISTILL_ENABLED: false })

  assert.deepEqual(readEngineEnvSettings(dir), {
    SUPERAGENT_TAKEOVER_ENABLED: true,
    MEMORY_DISTILL_ENABLED: false,
  })
  assert.ok(fs.existsSync(engineEnvPath(dir)))
  assert.equal(path.basename(engineEnvPath(dir)), ENGINE_ENV_FILE)
})

test('写入是整体替换：取消勾选必须真的能撤销', () => {
  const dir = tmpDir()
  writeEngineEnvSettings(dir, { SUPERAGENT_TAKEOVER_ENABLED: true })
  writeEngineEnvSettings(dir, {})

  assert.deepEqual(readEngineEnvSettings(dir), {})
})

test('文件缺失 / 损坏一律当没配过，且不抛', () => {
  const dir = tmpDir()
  assert.deepEqual(readEngineEnvSettings(dir), {})

  fs.writeFileSync(engineEnvPath(dir), '{ not json', 'utf8')
  assert.deepEqual(readEngineEnvSettings(dir), {})

  /* 手改时塞进白名单外的键，读取侧也必须过滤 */
  fs.writeFileSync(
    engineEnvPath(dir),
    JSON.stringify({ toggles: { SUPERAGENT_TAKEOVER_ENABLED: true, APP_SECRET: 'x' } }),
    'utf8',
  )
  assert.deepEqual(readEngineEnvSettings(dir), { SUPERAGENT_TAKEOVER_ENABLED: true })
})

test('每个白名单项都带类型与默认值（UI 要显示"默认关"）', () => {
  for (const spec of ENGINE_TOGGLES) {
    assert.equal(typeof spec.env, 'string')
    assert.match(spec.env, /^[A-Z][A-Z0-9_]*$/, 'env 名必须是 Settings 字段的大写形式')
    assert.ok(['boolean'].includes(spec.type))
    assert.equal(typeof spec.default, 'boolean')
  }
})
