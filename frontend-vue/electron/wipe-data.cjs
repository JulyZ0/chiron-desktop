/**
 * Chiron Desktop — 清空本机数据
 *
 * 这是「运行模式」标签页里唯一的破坏性动作：它替换了原来的「切换运行模式」。
 * 云端模式已废除，应用只有本地一种形态，所以那个按钮不再有意义；
 * 用户真正可能需要的是**把本机这份数据清掉重来**。
 *
 * ## 顺序是硬要求
 *
 *   停后端 → 清渲染进程存储/缓存 → 清设备 token → 删 userData/local → 重启
 *
 * 必须先停后端再删目录：PG 的数据文件在 `userData/local/pg` 下，
 * 进程还活着时删目录在 Windows 上会失败（文件被占用），或者删出半个库。
 * 也必须在删除成功后才重启 —— 反过来会重启出一个"看起来没清掉"的应用。
 *
 * ## 为什么还要清分区存储
 *
 * `userData/local/**` 只是文件。渲染进程的 localStorage / IndexedDB 在
 * `Partitions/` 下，不在 `local/` 里；只删目录会留下"数据没了但界面还记着
 * 上次登录态/列表缓存"的错位状态。
 *
 * ## 失败必须可归因
 *
 * 每一步都独立记录：哪一步失败、失败原因。整体抛错时把这些拼进 message ——
 * 用户看到"清空失败"却不知道卡在哪，就只能来反馈。
 */
const fsDefault = require('node:fs')
const pathDefault = require('node:path')

/** 要清的渲染进程存储（与 mode-switch.cjs 的枚举同一套 Electron storages 键名） */
const CLEAR_STORAGES = ['cookies', 'localstorage', 'indexdb', 'cachestorage', 'serviceworkers']

/**
 * @param {object} [options]
 * @param {object} [options.electron]  注入的 electron 模块（测试用 mock）
 * @param {Function} [options.stopBackend] 停止本地后端的回调（可注入）
 * @param {object} [options.fs]        注入的 fs（测试用）
 * @param {object} [options.path]      注入的 path（测试用）
 * @param {Function} [options.log]     (level, message) => void
 * @returns {Promise<{cleaned:string[],warnings:string[]}>}
 */
async function wipeLocalData(options = {}) {
  const electron = options.electron || {}
  const fsMod = options.fs || fsDefault
  const pathMod = options.path || pathDefault
  const log = typeof options.log === 'function' ? options.log : () => {}
  const result = { cleaned: [], warnings: [] }

  // 1) 先停后端：PG/Redis 的进程持有 local/pg 与 local/redis 下的文件句柄
  if (typeof options.stopBackend === 'function') {
    try {
      await options.stopBackend()
      result.cleaned.push('backend')
    } catch (err) {
      // 停不下来就不能删目录：硬删要么失败，要么留下损坏的库
      throw new Error(`停止本地后端失败，未执行清空：${(err && err.message) || err}`)
    }
  }

  // 2) 清渲染进程的存储与缓存（localStorage 镜像、IndexedDB、Service Worker）
  const partition = options.partition || 'persist:chiron-local'
  const ses = electron.session && typeof electron.session.fromPartition === 'function'
    ? electron.session.fromPartition(partition)
    : null
  if (ses && typeof ses.clearStorageData === 'function') {
    await ses.clearStorageData({ storages: CLEAR_STORAGES })
    result.cleaned.push(`storage:${partition}`)
  } else {
    result.warnings.push('拿不到渲染进程会话，界面缓存未清理')
  }
  for (const method of ['clearCache', 'clearAuthCache']) {
    if (ses && typeof ses[method] === 'function') {
      await ses[method]()
      result.cleaned.push(`${method}:${partition}`)
    }
  }

  // 3) 清设备 token（系统凭据库）。失败降级为警告：凭据库不可用时它本来也没存进去
  const tokenStore = options.tokenStore
  if (tokenStore && typeof tokenStore.clearToken === 'function') {
    try {
      const ok = await tokenStore.clearToken()
      if (ok === false) result.warnings.push('设备 token 清除失败（系统凭据库不可用）')
      else result.cleaned.push('device-token')
    } catch (err) {
      result.warnings.push(`设备 token 清除异常：${(err && err.message) || err}`)
    }
  }

  // 4) 删 userData/local：会话、知识库、记忆、上传文件、PG 数据目录全在这里
  const app = electron.app
  const userData = app && typeof app.getPath === 'function' ? app.getPath('userData') : ''
  if (!userData) {
    throw new Error('拿不到 userData 目录，无法清空本地数据')
  }
  const target = pathMod.join(userData, 'local')
  await fsMod.promises.rm(target, { recursive: true, force: true })
  result.cleaned.push(`data:${target}`)
  log('warn', `wiped local data: ${target}`)

  return result
}

/**
 * 重启应用：relaunch 之后把 exit 排到下一个 tick，
 * 让 IPC 回复先发出去，否则渲染进程只会看到 "frame was disposed"。
 */
function relaunchAfterWipe(electron, log, schedule = fn => setImmediate(fn)) {
  const app = electron && electron.app
  if (!app || typeof app.relaunch !== 'function') {
    log('warn', '拿不到 Electron app，数据已清空但需要手动重启')
    return false
  }
  app.relaunch()
  schedule(() => {
    try {
      if (typeof app.exit === 'function') app.exit(0)
      else if (typeof app.quit === 'function') app.quit()
    } catch (err) {
      log('error', `exit after wipe failed: ${(err && err.message) || err}`)
    }
  })
  return true
}

module.exports = { wipeLocalData, relaunchAfterWipe, CLEAR_STORAGES }
