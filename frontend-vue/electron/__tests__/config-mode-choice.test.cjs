'use strict'
/**
 * 首启「选择运行模式」的判定契约（契约 §1）。
 *
 * 这里钉住的是一个**曾经真实存在**的缺陷：`needsMigration = configExists && !modeChosen`，
 * 而全新安装时磁盘上连 config.json 都没有（`configExists=false`），于是首启
 * **一声不响**地按默认值（local）起步 —— 用户从未做过选择，与
 * 「首次启动让用户选本地/云服务，确认后再继续」的要求不符。
 *
 * 因此把"要不要问"单独派生为 `needsModeChoice`（只看 `modeChosen`），
 * 而"是不是老配置迁移"仍用 `needsMigration`（含 `configExists`）。
 * 两者混用就会退回到上面那个缺陷。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const config = require('../config.cjs')

/** 每个用例一个独立 userData 目录，绝不碰真实的 %APPDATA%/Chiron */
function useTempUserData(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-mode-choice-'))
  const previous = process.env.CHIRON_DESKTOP_USER_DATA
  process.env.CHIRON_DESKTOP_USER_DATA = dir
  t.after(() => {
    if (previous === undefined) delete process.env.CHIRON_DESKTOP_USER_DATA
    else process.env.CHIRON_DESKTOP_USER_DATA = previous
    fs.rmSync(dir, { recursive: true, force: true })
  })
  return dir
}

function writeConfig(dir, data) {
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(data, null, 2), 'utf8')
}

test('全新安装（无 config.json）也要求询问模式，且不需要迁移提示', t => {
  useTempUserData(t)
  const settings = config.readDeviceSettings()
  assert.equal(settings.needsModeChoice, true, '首启必须询问模式')
  assert.equal(settings.modeChosen, false)
  // 全新安装不是"老配置迁移"：不该给用户看"检测到上一版本"这类文案
  assert.equal(settings.needsMigration, false)
  assert.equal(settings.mode, 'local', '默认选项仍是本地')
})

test('老配置（有 apiUrl 无 mode）既要询问也要迁移提示', t => {
  const dir = useTempUserData(t)
  writeConfig(dir, { apiUrl: 'http://example.com:8080' })
  const settings = config.readDeviceSettings()
  assert.equal(settings.needsModeChoice, true)
  assert.equal(settings.needsMigration, true)
  assert.equal(settings.migration.legacyCloudDetected, true)
  // 迁移提示必须能引用原地址，否则用户不知道"继续云端"会连哪里
  assert.equal(settings.apiUrl, 'http://example.com:8080')
})

test('用户选过模式后不再询问，且选择被持久化', t => {
  useTempUserData(t)
  assert.equal(config.readDeviceSettings().needsModeChoice, true)

  config.saveMode('cloud')

  const settings = config.readDeviceSettings()
  assert.equal(settings.mode, 'cloud')
  assert.equal(settings.modeChosen, true)
  assert.equal(settings.needsModeChoice, false, '已选过就不该再弹窗')
  assert.equal(settings.needsMigration, false)
  assert.ok(settings.migration.modeChosenAt, '要记下选择时间，便于排障')
})

test('选过模式后 apiUrl 原样保留（切模式不丢云端地址）', t => {
  const dir = useTempUserData(t)
  writeConfig(dir, { apiUrl: 'http://example.com:8080' })
  config.saveMode('local')
  assert.equal(config.readDeviceSettings().apiUrl, 'http://example.com:8080')
})

test('clearMode 回到"待选择"状态，下次启动会重新询问', t => {
  useTempUserData(t)
  config.saveMode('local')
  assert.equal(config.readDeviceSettings().needsModeChoice, false)

  config.clearMode()

  const settings = config.readDeviceSettings()
  assert.equal(settings.needsModeChoice, true)
  assert.equal(settings.modeChosen, false)
  // 默认值仍是本地，但"默认"不等于"已选择"
  assert.equal(settings.mode, 'local')
})

test('运行模式遵循配置与环境变量（远程模式已恢复）', t => {
  const dir = useTempUserData(t)
  writeConfig(dir, { mode: 'cloud', modeChosen: true })
  const previous = process.env.CHIRON_DESKTOP_MODE
  t.after(() => {
    if (previous === undefined) delete process.env.CHIRON_DESKTOP_MODE
    else process.env.CHIRON_DESKTOP_MODE = previous
  })
  delete process.env.CHIRON_DESKTOP_MODE
  assert.equal(
    config.resolveMode(config.readDeviceSettings()),
    'cloud',
    '配置里的 cloud 必须生效：桌面端可以连接远程服务',
  )
  process.env.CHIRON_DESKTOP_MODE = 'local'
  assert.equal(
    config.resolveMode(config.readDeviceSettings()),
    'local',
    '环境变量优先于配置（开发/排障口径）',
  )
})

test('交给渲染进程的设置快照里 mode 必须与运行模式一致', t => {
  // 这条钉的是一个**用户实测的完整故障链**（云端模式被误收回后）：
  //   · `resolveMode()` 恒返回 local，进程确实按本地跑；
  //   · 但旧配置里残留 `mode:"cloud"`，`readDeviceSettings()` 会**原样**返回它；
  //   · 渲染层 `loadSettings()` 拿着这个值覆盖运行模式，于是前端以为自己在云端：
  //     「退出登录」走云端分支真登出 + 清会话，再按云端的 401 处理跳回首页；
  //   · 云端登录页/入口缺失 —— 用户被永久留在首页，只剩一句
  //     「登录已过期，请重新登录」，页面上没有任何可点的登录入口。
  //
  // 所以过 IPC 的那一份必须走 `readRuntimeDeviceSettings()`：磁盘上的 mode 可以留着
  // （迁移/回滚要用），但**界面看到的运行模式**只能有一个出口（`resolveMode`，
  // 含环境变量覆盖）。现在恢复远程模式后，两者应当一致为 cloud。
  const dir = useTempUserData(t)
  writeConfig(dir, { mode: 'cloud', modeChosen: true })

  const raw = config.readDeviceSettings()
  assert.equal(raw.mode, 'cloud', '磁盘原值照旧保留（迁移与回滚要靠它走完）')

  const runtime = config.readRuntimeDeviceSettings()
  assert.equal(runtime.mode, 'cloud', '界面读到的运行模式必须与进程实际运行模式一致')
  // 收敛只针对 mode：其它字段不能因为这次归一而丢失
  assert.equal(runtime.modeChosen, true)
  assert.deepEqual(runtime.local, raw.local)
  assert.deepEqual(runtime.ui, raw.ui)
})
