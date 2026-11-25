/**
 * 子进程树终止（PRD 9.8 / H56）。
 *
 * 缺陷形态：安装/设备 Git 操作超时或取消时只 `child.kill()` —— Windows 上那只杀
 * **直接子进程**，它派生出来的进程（python.exe 派生的 pip、git 派生的 credential
 * helper、shell 包装层…）会继续跑。后果有三层：
 *   1. 文件被占用 ⇒ 紧接着的"删临时目录/覆盖安装"失败，报错指向别处；
 *   2. 残留进程继续写盘，下次启动读到半份运行时；
 *   3. 用户在设置页点了取消，任务却仍在后台跑（且没有界面能再取消它）。
 *
 * 修法：Windows 用 `taskkill /T /F /PID <pid>` 杀整棵树；POSIX 先把子进程放进自己的
 * 进程组（`detached: true`），再 `kill(-pid)` 杀整组。两条路都保底再尝试一次
 * `child.kill('SIGKILL')`。
 *
 * 纯函数 + 可注入 spawn：单测不需要真实的进程树。
 */
'use strict'

/** 终止一个子进程及其全部派生进程。返回是否"发出了终止动作"。 */
async function killProcessTree(child, options = {}) {
  if (!child || typeof child !== 'object') return false
  const pid = typeof child.pid === 'number' ? child.pid : 0
  const platform = options.platform || process.platform
  const spawnSync = options.spawnSync || require('node:child_process').spawnSync
  const log = typeof options.log === 'function' ? options.log : () => {}

  let acted = false

  if (platform === 'win32') {
    if (pid > 0) {
      try {
        // /T = 连同子进程树，/F = 强制。不加 /T 就只是"杀那个 shim"，
        // 真正的 python/git 会活下来（这就是本条的起因）。
        const res = spawnSync('taskkill', ['/T', '/F', '/PID', String(pid)], {
          windowsHide: true,
          encoding: 'utf8',
        })
        acted = true
        if (res && typeof res.status === 'number' && res.status !== 0) {
          log('warn', `taskkill /T 返回 ${res.status}: ${String(res.stderr || '').trim()}`)
        }
      } catch (err) {
        log('warn', `taskkill /T 失败: ${(err && err.message) || err}`)
      }
    }
  } else if (pid > 0) {
    try {
      // 负数 PID = 整个进程组（要求 spawn 时 detached: true，见 killOptions()）。
      process.kill(-pid, 'SIGKILL')
      acted = true
    } catch {
      /* 进程组可能已经没了，继续走兜底 */
    }
  }

  try {
    child.kill('SIGKILL')
    acted = true
  } catch {
    /* 已经退出 */
  }

  return acted
}

/**
 * spawn 选项：POSIX 上让子进程自成一组，`kill(-pid)` 才有意义。
 * Windows 不需要（`taskkill /T` 自己会遍历进程树）。
 */
function killOptions(platform = process.platform) {
  if (platform === 'win32') return {}
  return { detached: true }
}

module.exports = { killProcessTree, killOptions }
