'use strict'
/**
 * H53：清空本机数据的**真实范围**必须与界面承诺一致（PRD 9.8）。
 *
 * 缺陷形态：`wipeLocalData` 只删 `userData/local` 与设备 token。
 * 于是"清空本机数据 → 如同首次启动"这句话是假的：
 *   - `config.json` 还在 ⇒ 服务地址/运行模式/镜像源原样恢复；
 *   - `token.bin` 里的 safeStorage 凭据还在（`tokenStore.clearToken` 失败时只 warning）⇒ 仍然"已登录"；
 *   - `secrets.bin`（应用密钥材料）还在；
 *   - `device-id` 还在 ⇒ 换不了身份，服务端仍认得这台机器；
 *   - `approved-roots.json` 还在 ⇒ 旧的项目根授权继续有效；
 *   - `updates/`（含已下载安装包与 state）还在 ⇒ 下次启动认为"已下载待安装"；
 *   - `Partitions/**` 还在 ⇒ localStorage/IndexedDB 里仍是旧的界面状态。
 *
 * 这些用例用内存 fs 打桩，逐项断言"删到了"，并断言"删不掉时点名上报而不是静默跳过"。
 */
const test = require('node:test')
const assert = require('node:assert')
const path = require('node:path')

const { wipeLocalData } = require('../wipe-data.cjs')

const USER_DATA = path.join('C:', 'fake', 'chiron')

/** 极简内存 fs：只实现 wipe-data 用到的 promises.rm。 */
function makeFs(initial = []) {
  const removed = []
  const failOn = new Set()
  return {
    removed,
    failOn,
    promises: {
      async rm(target, opts) {
        for (const bad of failOn) {
          if (String(target).includes(bad)) throw new Error(`EBUSY: ${target}`)
        }
        removed.push({ target: String(target), opts })
      },
    },
  }
}

function makeElectron(sessionOverrides = {}) {
  return {
    app: { getPath: name => (name === 'userData' ? USER_DATA : '') },
    session: {
      fromPartition: () => ({
        clearStorageData: async () => {},
        clearCache: async () => {},
        clearAuthCache: async () => {},
        ...sessionOverrides,
      }),
    },
  }
}

async function run(fsStub, electron, options = {}) {
  return wipeLocalData({ fs: fsStub, path, electron, log: () => {}, ...options })
}

test('清空范围覆盖设置/凭据/身份/更新/分区/日志（H53）', async () => {
  const fs = makeFs()
  const result = await run(fs, makeElectron())
  const targets = fs.removed.map(r => r.target)

  const mustContain = [
    path.join(USER_DATA, 'local'),
    path.join(USER_DATA, 'config.json'),
    path.join(USER_DATA, 'token.bin'),
    path.join(USER_DATA, 'secrets.bin'),
    path.join(USER_DATA, 'device-id'),
    path.join(USER_DATA, 'approved-roots.json'),
    path.join(USER_DATA, 'two-factor.json'),
    path.join(USER_DATA, 'runtime-state.json'),
    path.join(USER_DATA, 'updates'),
    path.join(USER_DATA, 'Partitions', 'chiron-local'),
    path.join(USER_DATA, 'Partitions', 'chiron-cloud'),
    path.join(USER_DATA, 'logs'),
  ]
  for (const want of mustContain) {
    assert.ok(targets.includes(want), `应删除 ${want}，实际：${targets.join(', ')}`)
  }
  assert.ok(result.cleaned.length >= mustContain.length)
  assert.deepEqual(result.warnings, [])
})

test('递归删除目录（local / updates / logs 都用 recursive）', async () => {
  const fs = makeFs()
  await run(fs, makeElectron())
  const dirs = ['local', 'updates', 'logs'].map(name => path.join(USER_DATA, name))
  for (const dir of dirs) {
    const entry = fs.removed.find(r => r.target === dir)
    assert.ok(entry, `应删除 ${dir}`)
    assert.equal(entry.opts.recursive, true)
    assert.equal(entry.opts.force, true)
  }
})

test('删不掉的项必须点名上报，其余项继续清', async () => {
  const fs = makeFs()
  fs.failOn.add('config.json')
  const result = await run(fs, makeElectron())

  assert.ok(
    result.warnings.some(w => w.includes('config.json')),
    'config.json 删除失败必须出现在 warnings 里',
  )
  // 其余项仍然完成
  assert.ok(fs.removed.some(r => r.target.endsWith('token.bin')))
  assert.ok(fs.removed.some(r => r.target.endsWith(path.join('local'))))
})

test('keepLogs=true 时保留日志目录', async () => {
  const fs = makeFs()
  await run(fs, makeElectron(), { keepLogs: true })
  assert.ok(!fs.removed.some(r => r.target === path.join(USER_DATA, 'logs')))
})

test('自定义 partitions 时只删指定分区', async () => {
  const fs = makeFs()
  await run(fs, makeElectron(), { partitions: ['persist:custom'] })
  const partitions = fs.removed.filter(r => r.target.includes('Partitions')).map(r => r.target)
  assert.deepEqual(partitions, [path.join(USER_DATA, 'Partitions', 'custom')])
})

test('停后端失败时整体中止（不删半份数据）', async () => {
  const fs = makeFs()
  await assert.rejects(
    () =>
      wipeLocalData({
        fs,
        path,
        electron: makeElectron(),
        stopBackend: async () => {
          throw new Error('backend still running')
        },
      }),
    /停止本地后端失败/,
  )
  assert.equal(fs.removed.length, 0, '后端没停就不该删任何东西')
})

test('拿不到 userData 时明确报错', async () => {
  const fs = makeFs()
  await assert.rejects(
    () => wipeLocalData({ fs, path, electron: { app: { getPath: () => '' } } }),
    /userData/,
  )
})
