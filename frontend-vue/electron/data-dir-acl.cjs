/**
 * 数据目录的权限收口。
 *
 * 本地模式的隔离边界是「一个 OS 用户一套数据」：`%APPDATA%` 本身就是 per-user 的，
 * NTFS 会让该目录继承父目录的 ACL —— 于是**其他 OS 用户本来就读不到**。
 * 但「本来读不到」依赖父目录的继承，属于运气而非声明。本模块把这件事显式化：
 *
 *   - Windows：`icacls` 去掉继承，只授当前账户与 SYSTEM；
 *   - 其他平台：`chmod 0700`（POSIX 上 mode 是真生效的，Windows 上 `fs.chmod`
 *     的 `0o600` 其实被忽略 —— 这正是需要 icacls 的原因）。
 *
 * ## 为什么不用 `fs.chmod`
 *
 * `main.cjs` / `secrets.cjs` / `token-store.cjs` 里都写了 `{ mode: 0o600 }`，
 * 但 Node 在 Windows 上**直接忽略 mode**（NTFS 不走 POSIX 权限位）。那份代码
 * 在 Linux/macOS 有效，在 Windows 上是空操作。所以要单独走 ACL。
 *
 * ## 能力的边界（写进注释，避免后来者以为能防住更多）
 *
 * 收口只能挡住**其他 OS 用户**。同一用户下的任意进程都能读写这棵树 ——
 * 这是操作系统语义：用户对自己的配置文件目录有完全控制权。同用户场景的防线是
 * `secrets.cjs` 的 DPAPI 加密（密文只在原 OS 用户下可解），不是 ACL。
 *
 * Administrators 组成员同样可以 take-ownership 后读取，这一点无法用 ACL 阻止，
 * 也不在本模块的目标内（那需要 BitLocker 级别的全盘加密）。
 *
 * ## 失败不阻断启动
 *
 * 收口失败只是「没加固」，不该让应用起不来：权限操作可能因策略、网络盘、
 * 或非标准文件系统而失败。调用方拿到 `{ ok: false, reason }` 记日志即可。
 */
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

/** 目录树应有的权限串：OI=对象继承、CI=容器继承、F=完全控制 */
const OWNER_GRANT = '(OI)(CI)F'

/**
 * 当前登录账户的 `DOMAIN\user` 形态。
 *
 * 优先环境变量：`icacls` 需要能被解析的名字，`USERDOMAIN\USERNAME` 在域机与
 * 本地账户上都成立（本地账户的 USERDOMAIN 就是机器名）。环境变量缺失时才退回
 * `os.userInfo()` —— 后者在部分受限上下文会抛，故包在 try 里。
 */
function currentAccount(env = process.env, userInfo = os.userInfo) {
  const domain = String(env?.USERDOMAIN || '').trim()
  const name = String(env?.USERNAME || '').trim()
  if (name) return domain ? `${domain}\\${name}` : name
  try {
    const info = userInfo()
    const fallback = String(info?.username || '').trim()
    return fallback
  } catch {
    return ''
  }
}

/**
 * 生成 `icacls` 的参数。
 *
 * 两步而不是一步：先 `/grant:r` 补上当前账户与 SYSTEM 的完全控制，再
 * `/inheritance:r` 去掉继承。顺序反过来会出现一个瞬间 —— 继承已移除、
 * 显式授权还没补上 —— 该目录对任何人都不可访问。虽然本进程通常已是所有者
 * 且能再改回来，但没有理由冒这个险。
 *
 * @param {string} dir 绝对路径
 * @param {string} account `DOMAIN\user`
 * @returns {string[][]} 依次执行的参数数组
 */
function buildIcaclsArgs(dir, account) {
  const grants = [account, 'SYSTEM'].filter(Boolean)
  const grantArgs = []
  for (const who of grants) grantArgs.push('/grant:r', `${who}:${OWNER_GRANT}`)
  return [
    [dir, ...grantArgs],
    [dir, '/inheritance:r'],
  ]
}

/**
 * 给数据目录收口。
 *
 * @param {object} options
 * @param {string} options.dir 目标目录（绝对路径）
 * @param {string} [options.platform] 默认 `process.platform`，测试可注入
 * @param {string} [options.account] 默认由 `currentAccount()` 推断
 * @param {Function} [options.exec] 执行器，签名 `(file, args) => void`，默认 `execFileSync`
 * @returns {{ ok: boolean, reason?: string, skipped?: boolean }}
 */
function hardenDataDir(options = {}) {
  const dir = typeof options.dir === 'string' ? options.dir.trim() : ''
  const platform = options.platform || process.platform
  if (!dir) return { ok: false, reason: 'empty-dir' }
  // 相对路径会跟着 cwd 漂，收错地方的锁比不锁更危险
  if (!path.isAbsolute(dir)) return { ok: false, reason: 'not-absolute' }

  try {
    fs.mkdirSync(dir, { recursive: true })
  } catch (err) {
    return { ok: false, reason: `mkdir-failed: ${err?.message || err}` }
  }

  if (platform !== 'win32') {
    try {
      fs.chmodSync(dir, 0o700)
      return { ok: true }
    } catch (err) {
      return { ok: false, reason: `chmod-failed: ${err?.message || err}` }
    }
  }

  const account = options.account !== undefined ? String(options.account).trim() : currentAccount()
  // 拿不到账户名就什么都不做：icacls /grant:r 一个空名字会把现有 ACL 清空
  if (!account) return { ok: false, skipped: true, reason: 'no-account' }
  // 账户名里出现引号/换行说明来源不干净，不拿它拼命令行
  if (/["\r\n]/.test(account)) return { ok: false, skipped: true, reason: 'unsafe-account' }

  const exec =
    typeof options.exec === 'function'
      ? options.exec
      : (file, args) => require('node:child_process').execFileSync(file, args, { stdio: 'ignore' })

  try {
    for (const args of buildIcaclsArgs(dir, account)) exec('icacls', args)
    return { ok: true }
  } catch (err) {
    return { ok: false, reason: `icacls-failed: ${err?.message || err}` }
  }
}

module.exports = {
  OWNER_GRANT,
  currentAccount,
  buildIcaclsArgs,
  hardenDataDir,
}
