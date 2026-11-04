/**
 * Windows Hello 二次确认。
 *
 * 本地模式的身份来自 OS 账户（一个 Windows 用户 = 一个应用用户），
 * 免口令会话由网关按 loopback 来源直接签发。也就是说：**能坐到这台机器前的人
 * 就自动是"这个账户的主人"**。这在共用工作站的场景下不够 —— 登录 Windows 后
 * 人走开、屏幕没锁，回来的是另一个人。
 *
 * 所以启动后再加一道 `UserConsentVerifier`（Windows Hello：PIN / 指纹 / 人脸）。
 * 它验证不了"是谁"，但能证明"是同一个已通过 OS 登录的人在场"。
 *
 * ## 为什么走 PowerShell 而不是原生模块
 *
 * Electron 没有暴露 WinRT。装 `@nodert-win10-*` 之类的原生绑定要为每个 Electron
 * 版本重编译，且带上整条 C++/WinRT 依赖。而 `UserConsentVerifier` 是少数能用
 * 几行 PowerShell 调通的 WinRT API —— 起一个隐藏进程的代价（数百毫秒）在
 * 「启动时确认一次」这个频率上可以接受。
 *
 * ## 失败必须放行，不能锁死
 *
 * 本机没有 Hello 设备（无 TPM、无生物识别、精简版 Windows）时 `CheckAvailability`
 * 返回 `DeviceNotPresent`。此时**跳过**确认：这道确认是加固，不是访问控制前提；
 * 让它拦住启动等于让一部分机器永远进不去应用。同理，脚本执行失败（被组策略
 * 禁止、powershell 缺失）也只记日志放行。
 *
 * ## 边界
 *
 * 这道确认挡不住"用户自己点了通过"。它防的是**旁人**：没有该账户的
 * Windows Hello 凭据就无法确认。
 */
const { execFile } = require('node:child_process')

/** PowerShell 里复用的一段：把 WinRT 的 IAsyncOperation 转成 Task 并等结果 */
const WINRT_AWAIT_PRELUDE = [
  'Add-Type -AssemblyName System.Runtime.WindowsRuntime',
  '$null = [Windows.Security.Credentials.UI.UserConsentVerifier,Windows.Security.Credentials.UI,ContentType=WindowsRuntime]',
  '$gen = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {',
  "  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and",
  "  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'",
  '} | Select-Object -First 1',
  "if (-not $gen) { Write-Output 'Availability=NotSupported'; exit 0 }",
  'function Await-Op($op, [Type]$t) {',
  '  $m = $gen.MakeGenericMethod($t)',
  '  $task = $m.Invoke($null, @($op))',
  '  $task.Wait(60000) | Out-Null',
  '  return $task.Result',
  '}',
].join('\n')

/** 探测可用性（只读，不弹任何 UI） */
const AVAILABILITY_SCRIPT = `${WINRT_AWAIT_PRELUDE}
$r = Await-Op ([Windows.Security.Credentials.UI.UserConsentVerifier]::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
Write-Output ('Availability=' + $r)
`

/**
 * 构造发起确认的脚本。
 *
 * `message` 会进入单引号字符串：把 `'` 翻倍（PowerShell 的转义方式），
 * 其余字符原样 —— 这里不做白名单，因为消息来自我们自己的常量，
 * 转义只是防止将来有人把它接到用户输入上。
 */
function verificationScript(message) {
  const escaped = String(message == null ? '' : message).replace(/'/g, "''")
  return `${WINRT_AWAIT_PRELUDE}
$r = Await-Op ([Windows.Security.Credentials.UI.UserConsentVerifier]::RequestVerificationAsync('${escaped}')) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
Write-Output ('Verification=' + $r)
`
}

const defaultExecFile = (file, args, opts, cb) => execFile(file, args, opts, cb)

/** 可注入依赖（沿用 secrets.cjs 的 `_setDeps` 风格，否则单测跑不起来） */
let deps = { platform: process.platform, execFile: defaultExecFile }

function _setDeps(next = {}) {
  deps = { ...deps, ...next }
}

function _resetDeps() {
  deps = { platform: process.platform, execFile: defaultExecFile }
}

/**
 * 跑一段 PowerShell 并取出 `<Key>=<Value>` 结果行。
 *
 * 不抛错：任何异常都折算成 `null`，由调用方按「无法确认」处理。
 */
function runPowerShell(script, key) {
  return new Promise(resolve => {
    if (deps.platform !== 'win32') return resolve(null)
    try {
      deps.execFile(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
        { windowsHide: true, timeout: 70000 },
        (err, stdout) => {
          if (err && !stdout) return resolve(null)
          // 逐行找 `<Key>=<Value>`：不用 RegExp —— 反斜杠要穿过两层引号太容易写错，
          // 这里只需要前缀匹配，字符串方法更直白也更难写崩。
          const line = String(stdout || '')
            .split('\n')
            .map(s => s.trim())
            .find(s => s.startsWith(key + '='))
          resolve(line ? line.slice(key.length + 1) : null)
        },
      )
    } catch {
      resolve(null)
    }
  })
}

/** Hello 是否可用（只认 `Available`） */
async function checkAvailability() {
  return runPowerShell(AVAILABILITY_SCRIPT, 'Availability')
}

/**
 * 请求一次确认，返回**原始结果**（`Verified` / `Canceled` / `RetriesExhausted` /
 * `DeviceNotPresent` / …），脚本失败或非 Windows 上为 `null`。
 *
 * 调用方需要区分"用户没通过"（应拒绝）与"这台机器没法确认"（应放行），
 * 所以不能只回布尔。
 */
async function requestVerificationResult(message) {
  return runPowerShell(verificationScript(message), 'Verification')
}

/**
 * 请求一次确认的布尔视图（便利函数）。
 *
 * @returns {Promise<boolean>} 仅 `Verified` 为 true；取消 / 重试耗尽 / 出错都是 false
 */
async function requestVerification(message) {
  return (await requestVerificationResult(message)) === 'Verified'
}

module.exports = {
  AVAILABILITY_SCRIPT,
  verificationScript,
  checkAvailability,
  requestVerification,
  requestVerificationResult,
  _setDeps,
  _resetDeps,
}
