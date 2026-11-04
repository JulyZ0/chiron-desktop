const test = require('node:test')
const assert = require('node:assert/strict')
const os = require('node:os')
const fs = require('node:fs')
const nodePath = require('node:path')
const { wipeLocalData, relaunchAfterWipe, CLEAR_STORAGES } = require('../wipe-data.cjs')

/** 造一个假的 userData，内含 local/ 子目录 */
function makeUserData() {
  const root = fs.mkdtempSync(nodePath.join(os.tmpdir(), 'wipe-'))
  const local = nodePath.join(root, 'local')
  fs.mkdirSync(nodePath.join(local, 'pg'), { recursive: true })
  fs.writeFileSync(nodePath.join(local, 'pg', 'PG_VERSION'), '18')
  return { root, local }
}

function fakeElectron({ root, clearStorageFails = false, withSession = true } = {}) {
  const calls = []
  return {
    calls,
    app: { getPath: () => root },
    session: withSession
      ? {
          fromPartition: partition => ({
            clearStorageData: async arg => {
              calls.push(['clearStorageData', partition, arg])
              if (clearStorageFails) throw new Error('boom')
            },
            clearCache: async () => calls.push(['clearCache', partition]),
            clearAuthCache: async () => calls.push(['clearAuthCache', partition]),
          }),
        }
      : undefined,
  }
}

test('成功路径：停后端 → 清存储 → 清 token → 删 local 目录', async () => {
  const { root, local } = makeUserData()
  const electron = fakeElectron({ root })
  const order = []
  const result = await wipeLocalData({
    electron,
    stopBackend: async () => order.push('stop'),
    tokenStore: { clearToken: async () => { order.push('token'); return true } },
    log: () => {},
  })

  assert.equal(fs.existsSync(local), false, 'local 目录必须被删除')
  assert.deepEqual(order, ['stop', 'token'])
  assert.ok(result.cleaned.includes('backend'))
  assert.ok(result.cleaned.includes('device-token'))
  assert.ok(result.cleaned.some(x => x.startsWith('data:')), result.cleaned.join(','))
  assert.equal(result.warnings.length, 0)
})

test('清的是 persist:chiron-local 分区的存储（不是别的分区）', async () => {
  const { root } = makeUserData()
  const electron = fakeElectron({ root })
  await wipeLocalData({ electron, log: () => {} })

  const storageCall = electron.calls.find(c => c[0] === 'clearStorageData')
  assert.ok(storageCall, '必须清渲染进程存储')
  assert.equal(storageCall[1], 'persist:chiron-local')
  assert.deepEqual(storageCall[2], { storages: CLEAR_STORAGES })
})

test('后端停不下来就中止，绝不继续删目录', async () => {
  const { root, local } = makeUserData()
  await assert.rejects(
    wipeLocalData({
      electron: fakeElectron({ root }),
      stopBackend: async () => { throw new Error('pg 还活着') },
      log: () => {},
    }),
    /停止本地后端失败/,
  )
  assert.equal(fs.existsSync(local), true, '中止时目录必须原样保留')
})

test('拿不到 userData 时报错，而不是静默当成清空成功', async () => {
  await assert.rejects(
    wipeLocalData({ electron: { app: { getPath: () => '' } }, log: () => {} }),
    /拿不到 userData 目录/,
  )
})

test('local 目录不存在也不报错（重复清空幂等）', async () => {
  const root = fs.mkdtempSync(nodePath.join(os.tmpdir(), 'wipe-empty-'))
  const result = await wipeLocalData({ electron: fakeElectron({ root }), log: () => {} })
  assert.ok(result.cleaned.some(x => x.startsWith('data:')))
})

test('没有 session 时降级为警告，不阻断删除', async () => {
  const { root, local } = makeUserData()
  const result = await wipeLocalData({
    electron: fakeElectron({ root, withSession: false }),
    log: () => {},
  })
  assert.equal(fs.existsSync(local), false)
  assert.ok(result.warnings.some(w => w.includes('界面缓存未清理')), result.warnings.join(','))
})

test('token 清除失败只记警告：凭据库不可用时它本来也没存进去', async () => {
  const { root, local } = makeUserData()
  const result = await wipeLocalData({
    electron: fakeElectron({ root }),
    tokenStore: { clearToken: async () => false },
    log: () => {},
  })
  assert.equal(fs.existsSync(local), false)
  assert.ok(result.warnings.some(w => w.includes('设备 token 清除失败')))
})

test('relaunchAfterWipe：调用 relaunch 并调度 exit', () => {
  const order = []
  let scheduled = null
  const electron = {
    app: {
      relaunch: () => order.push('relaunch'),
      exit: code => order.push(`exit:${code}`),
    },
  }
  const ok = relaunchAfterWipe(electron, () => {}, fn => { scheduled = fn })
  assert.equal(ok, true)
  assert.deepEqual(order, ['relaunch'])
  scheduled()
  assert.deepEqual(order, ['relaunch', 'exit:0'])
})

test('relaunchAfterWipe：拿不到 app 时返回 false，不假装重启了', () => {
  assert.equal(relaunchAfterWipe({}, () => {}), false)
})
