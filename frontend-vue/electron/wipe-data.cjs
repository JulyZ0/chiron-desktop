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

  // 4) 删 userData 下的**出厂范围**（H53）。
  //
  // 旧实现只删 `local/` 与设备 token：设置（服务地址/运行模式/镜像源）、安全存储里的
  // 凭据、设备身份、已批准的项目根、2FA 材料、更新缓存与已下载安装包全都留着 ——
  // 而界面上写的是"清空本机数据后如同首次启动"。用户按那句话做判断，代码却没做到：
  // 重置之后仍然连向旧服务、仍然带旧设备身份，甚至会被上一份更新包顶回去。
  //
  // 每一项独立记录成功/失败：一处失败不该让其余项一起放弃（否则用户永远清不干净，
  // 也不知道卡在哪）。
  const app = electron.app
  const userData = app && typeof app.getPath === 'function' ? app.getPath('userData') : ''
  if (!userData) {
    throw new Error('拿不到 userData 目录，无法清空本地数据')
  }

  const removeOne = async (label, absPath) => {
    try {
      await fsMod.promises.rm(absPath, { recursive: true, force: true })
      result.cleaned.push(`${label}:${absPath}`)
    } catch (err) {
      // 删不掉的必须点名上报：静默跳过会把"清空成功"变成假承诺。
      result.warnings.push(`${label} 未能删除（${absPath}）：${(err && err.message) || err}`)
    }
  }

  // 4a) 本机数据目录（会话/知识库/记忆/上传/内嵌 PG·Redis）
  await removeOne('data', pathMod.join(userData, 'local'))

  // 4b) 设置 / 凭据 / 身份 / 2FA —— 逐个文件，而不是"删整个 userData"：
  //     userData 里还有 Electron 自己的缓存目录，整体删除在 Windows 上经常因为
  //     文件被占用而失败，结果是"删了一半"，比不删更难判断。
  for (const [label, name] of [
    ['settings', 'config.json'],
    ['token', 'token.bin'],
    ['secrets', 'secrets.bin'],
    ['device-id', 'device-id'],
    ['approved-roots', 'approved-roots.json'],
    ['two-factor', 'two-factor.json'],
    ['runtime-state', 'runtime-state.json'],
  ]) {
    await removeOne(label, pathMod.join(userData, name))
  }

  // 4c) 更新缓存与已下载安装包（含 state：否则下次启动会认为"已下载待安装"）
  await removeOne('updates', pathMod.join(userData, 'updates'))

  // 4d) 渲染进程分区：localStorage / IndexedDB 的落盘目录
  const partitions = Array.isArray(options.partitions) && options.partitions.length
    ? options.partitions
    : ['persist:chiron-local', 'persist:chiron-cloud']
  for (const name of partitions) {
    await removeOne(
      `partition:${name}`,
      pathMod.join(userData, 'Partitions', String(name).replace(/^persist:/, '')),
    )
  }

  // 4e) 日志（默认清：否则"清空"之后日志里还留着上一次的路径与会话线索）
  if (options.keepLogs !== true) {
    await removeOne('logs', pathMod.join(userData, 'logs'))
  }

  log('warn', `wiped user data under: ${userData}`)

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
