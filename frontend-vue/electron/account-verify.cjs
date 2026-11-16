/**
 * Windows 账户密码验证（Windows Hello 的兜底）。
 *
 * windows-hello.cjs 在设备不可用时会跳过确认。那对「启动提醒」可以接受，
 * 但对**清空本机数据**这种不可逆操作不行 —— 跳过等于零验证。
 *
 * Windows 的 PIN 属于 Hello 体系（配了 PIN 时 UserConsentVerifier 报 Available
 * 并弹 PIN 输入框），所以「Hello 不可用」通常意味着两者都没配；此时唯一还能
 * 证明「你是这个账户的主人」的办法，就是验当前 Windows 账户密码。
 *
 * 密码绝不走命令行参数：Windows 上同权限进程能读到别的进程的 CommandLine。
 * 脚本走 -EncodedCommand（base64/UTF-16LE），密码走 stdin。
 */
const { spawn } = require('node:child_process')

/**
 * LOGON32_LOGON_INTERACTIVE（=2）。
 *
 * 为什么不是 LOGON32_LOGON_NETWORK（=3）：网络登录**必须联系 DC 验证**，
 * 域账户在断网、隔离主机、VPN 未连时必然失败（1311 无登录服务器 / 1326）。
 * 而"清空本机数据"是**纯本地操作**，用依赖网络的验证去挡它，等于让这些环境里的
 * 用户永远清不了数据。
 *
 * INTERACTIVE 走**本地缓存凭据**（cached credentials，域用户上次成功登录后
 * 系统保留的那份哈希），断网也能验证本机账户与域账户。代价是它理论上需要
 * SeInteractiveLogonRight —— 但我们要验证的正是**当前这个已登录的账户**，
 * 它本来就持有该权限，所以不受影响。
 */
const LOGON_TYPE = 2
const LOGON_PROVIDER = 0
const SEP = String.fromCharCode(92)
const NL = String.fromCharCode(10)
const CR = String.fromCharCode(13)

/** 密码错 / 用户名错 —— 只有这两种算「验证不通过」 */
const BAD_CREDENTIAL = 1326

/** 无法完成验证的环境原因（断网、DC 不可达、服务未启动）—— 与"密码错"必须分开 */
const UNAVAILABLE_CODES = {
  1311: '无法联系登录服务器（域控不可达，可能处于离线状态）',
  1355: '指定的域不存在或无法联系',
  1722: 'RPC 服务器不可用',
  5: '拒绝访问：当前账户没有验证所需的权限',
  1909: '账户已被锁定（本次验证不计入失败次数）',
}

/** 账号层面的问题：报成「密码错」会让用户反复试一个正确的密码 */
const ACCOUNT_ISSUES = {
  1330: '密码已过期，请先在 Windows 中更新密码',
  1907: '需要先修改密码才能登录',
  1331: '账户已被禁用',
  1327: '账户限制：可能是空密码被策略禁止',
}

/**
 * 从 stdin 读密码 → 调 LogonUser 验证。
 * 用户名/域走环境变量（同样不进命令行）。
 */
const VERIFY_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  '$src = @"',
  'using System;',
  'using System.Runtime.InteropServices;',
  'public class ChironLogonApi {',
  '  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]',
  '  public static extern bool LogonUser(string user, string domain, string password,',
  '    int logonType, int logonProvider, out IntPtr token);',
  '  [DllImport("kernel32.dll", SetLastError=true)]',
  '  public static extern bool CloseHandle(IntPtr h);',
  '}',
  '"@',
  'Add-Type -TypeDefinition $src',
  '',
  '$pw = [Console]::In.ReadToEnd()',
  '$pw = $pw.TrimEnd([char]13, [char]10)',
  "if ($pw.Length -eq 0) { Write-Output 'Verify=Empty'; exit 0 }",
  '$user = $env:CHIRON_VERIFY_USER',
  '$domain = $env:CHIRON_VERIFY_DOMAIN',
  "if (-not $user) { Write-Output 'Verify=NoUser'; exit 0 }",
  '',
  '$tok = [IntPtr]::Zero',
  '$ok = [ChironLogonApi]::LogonUser($user, $domain, $pw, ' + LOGON_TYPE + ', ' +
  LOGON_PROVIDER + ', [ref]$tok)',
  '$err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()',
  'if ($tok -ne [IntPtr]::Zero) { [ChironLogonApi]::CloseHandle($tok) | Out-Null }',
  '',
  "if ($ok) { Write-Output 'Verify=Verified' }",
  "else { Write-Output 'Verify=Failed'; Write-Output ('Code=' + $err) }",
].join(NL)

/** -EncodedCommand 要 base64 / UTF-16LE */
function encodeCommand(script) {
  return Buffer.from(String(script), 'utf16le').toString('base64')
}

const defaultSpawn = (cmd, args, opts) => spawn(cmd, args, opts)

let deps = { platform: process.platform, spawn: defaultSpawn }

function _setDeps(next = {}) {
  deps = { ...deps, ...next }
}

function _resetDeps() {
  deps = { platform: process.platform, spawn: defaultSpawn }
}

/**
 * 解析 `DOMAIN\username`。
 *
 * 本地账户的域用 `.` 表示本机最稳（机器名改过也对）；
 * 域账户必须用真实域名。
 */
function parseAccount(account) {
  const raw = String(account || '').trim()
  if (!raw) return null
  const idx = raw.indexOf(SEP)
  if (idx <= 0) return { user: raw, domain: '.' }
  const domain = raw.slice(0, idx)
  const user = raw.slice(idx + 1)
  if (!user) return null
  return { user, domain }
}

/**
 * 验证当前 Windows 账户密码。
 *
 * @param {object} options
 * @param {string} options.account    `DOMAIN\username`
 * @param {string} options.password   用户输入（不落盘、不记日志、不进响应）
 * @param {Function} [options.verify] 注入的验证实现（测试用）
 * @returns {Promise<{ok:boolean, reason:string, message:string}>}
 */
async function verifyAccountPassword(options = {}) {
  const account = parseAccount(options.account)
  if (!account) {
    return { ok: false, reason: 'unsupported', message: '拿不到当前 Windows 账户' }
  }
  if (deps.platform !== 'win32') {
    return { ok: false, reason: 'unsupported', message: '仅 Windows 支持账户密码验证' }
  }

  const password = typeof options.password === 'string' ? options.password : ''
  if (password.length === 0) {
    return { ok: false, reason: 'empty-password', message: '请输入当前 Windows 账户密码' }
  }

  const verify = typeof options.verify === 'function' ? options.verify : runVerify
  let raw = null
  try {
    raw = await verify(account, password)
  } catch {
    return { ok: false, reason: 'error', message: '验证执行异常' }
  }

  if (!raw) return { ok: false, reason: 'error', message: '验证进程未返回结果' }
  if (raw.verified) return { ok: true, reason: 'verified', message: '' }
  if (raw.code === BAD_CREDENTIAL) {
    return { ok: false, reason: 'bad-credential', message: '密码不正确' }
  }
  if (raw.code && UNAVAILABLE_CODES[raw.code]) {
    // 环境原因：用户没错，这台机器当前没法验证。调用方据此走离线兜底，
    // 而不是让人反复重试一个正确的密码。
    return { ok: false, reason: 'unavailable', message: UNAVAILABLE_CODES[raw.code] }
  }
  if (raw.code && ACCOUNT_ISSUES[raw.code]) {
    return { ok: false, reason: 'account-issue', message: ACCOUNT_ISSUES[raw.code] }
  }
  return {
    ok: false,
    reason: 'error',
    message: raw.code ? `验证失败（代码 ${raw.code}）` : '验证未通过',
  }
}

/** 起 PowerShell 跑验证，密码写进 stdin。返回 { verified, code } 或 null */
function runVerify(account, password) {
  return new Promise(resolve => {
    let child
    try {
      child = deps.spawn(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-EncodedCommand', encodeCommand(VERIFY_SCRIPT)],
        {
          windowsHide: true,
          env: {
            SystemRoot: process.env.SystemRoot || 'C:' + SEP + 'Windows',
            PATH: process.env.PATH || '',
            CHIRON_VERIFY_USER: account.user,
            CHIRON_VERIFY_DOMAIN: account.domain,
          },
          stdio: ['pipe', 'pipe', 'pipe'],
        },
      )
    } catch {
      return resolve(null)
    }

    let stdout = ''
    let settled = false
    const finish = value => {
      if (settled) return
      settled = true
      resolve(value)
    }

    const timer = setTimeout(() => {
      try { child.kill() } catch { /* 已退出 */ }
      finish(null)
    }, 30000)

    child.stdout.on('data', chunk => { stdout += chunk })
    // stderr 必须消费，否则管道写满会阻塞子进程
    child.stderr.on('data', () => {})
    child.on('error', () => { clearTimeout(timer); finish(null) })
    child.on('close', () => {
      clearTimeout(timer)
      const lines = String(stdout || '').split(NL).map(s => s.trim())
      const get = key => {
        const line = lines.find(s => s.startsWith(key + '='))
        return line === undefined ? '' : line.slice(key.length + 1)
      }
      const verdict = get('Verify')
      if (verdict === 'Verified') return finish({ verified: true, code: 0 })
      if (verdict === 'Failed') {
        const code = Number.parseInt(get('Code'), 10)
        return finish({ verified: false, code: Number.isFinite(code) ? code : 0 })
      }
      finish(null)
    })

    // 密码只在这里出现一次，写进管道后即丢弃引用
    try {
      child.stdin.end(password + NL)
    } catch {
      finish(null)
    }
  })
}

module.exports = {
  verifyAccountPassword,
  parseAccount,
  encodeCommand,
  VERIFY_SCRIPT,
  BAD_CREDENTIAL,
  ACCOUNT_ISSUES,
  UNAVAILABLE_CODES,
  _setDeps,
  _resetDeps,
}
