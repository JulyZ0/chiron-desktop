'use strict'
/**
 * 设备文件桥—— 桌面端主动外连远端网关。
 *
 * 远端引擎/网关需要读写本机项目文件时，不能 inbound 连用户机器（NAT/防火墙），
 * 所以由这里建立 WebSocket：
 *
 *   远端 /ws/device-fs  ◄── 本进程 ──►  fs（仅用户批准的项目根内）
 *
 * 安全边界（H46 之后的口径）：
 *   - **根授权**：每个 op 带的 `root` 必须是主进程里"用户批准过"的 canonical 根
 *     （`approved-roots.cjs`）。修复前服务端可以任选一个绝对路径当 root，随后
 *     read/write/remove/rename 全部放行。现在不在名单里就拒绝；可选的
 *     `requestRootApproval` 会让主进程弹一次原生对话框问用户。
 *   - **realpath-only**：包含校验只认 realpath，**不做词法回退**。修复前的
 *     「realpath 失败就按词法比」正是 symlink / NTFS junction 逃逸能成立的根因：
 *     词法上 `root\link\secret` 在根内，实际指向的是根外。
 *   - **能力白名单**：op 名列白名单；Git 只接受固定子命令与固定选项形状，动态
 *     禁用 aliases/hooks/filters/helpers/pager，最小环境（不继承主进程环境）。
 *   - **连接票据**：`POST /v1/device-fs/ticket`（Authorization 头带 token）换取
 *     **短时一次性票据**，WS 只带 `?ticket=…`。修复前 token 直接放在 WS query 里，
 *     会被网关访问日志、代理、崩溃转储成规模地抄走。拿不到票据就**不连**，
 *     绝不退回 query token（fail-closed）。
 *   - **设备归属**：票据申请带 Ed25519 签名证明（见 `device-identity.cjs`），
 *     `device_id` 不再是"随便填个非空串"。
 *   - **撤销与 HTTP 一致**：票据被拒（401/403）或服务端以 4401/4403 关闭连接时，
 *     桥进入 `revoked` 并停止重连 —— 和 HTTP 401 的处理同口径，而不是"断线重试"。
 */

const fs = require('node:fs')
const fsp = fs.promises
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')

const { isWithin, isUncPath, stripWindowsVerbatim } = require('./approved-roots.cjs')
const {
  buildDeviceProof,
  ensureDeviceIdentity,
} = require('./device-identity.cjs')

const MAX_READ_BYTES = 8 << 20
const MAX_LIST_ENTRIES = 500
const MAX_MIRROR_FILES = 5000
const MAX_MIRROR_BYTES = 200 * 1024 * 1024
const MAX_GIT_OUTPUT = 8 << 20
const GIT_TIMEOUT_MS = 30000
const RECONNECT_MS = 3000
const RECONNECT_MAX_MS = 30000
const TICKET_PATH = '/v1/device-fs/ticket'
/** WebSocket close code：网关用它们表达"票/设备被撤销"，与 HTTP 401/403 同义。 */
const WS_CLOSE_UNAUTHORIZED = 4401
const WS_CLOSE_FORBIDDEN = 4403
// WebSocket.OPEN：不直接引用 WebSocket.OPEN，测试里的假实现也能用同一份判断。
const WS_OPEN = 1
const NOISE_DIRS = new Set([
  'node_modules', 'dist', 'build', 'target', '__pycache__', 'vendor', '.next', 'coverage',
])

/**
 * 能力白名单：不在这张表里的 op 一律拒绝（连路径解析都不做）。
 *
 * 修复前是"一路 if 到底"：未知 op 会走到函数末尾才抛错，中途的副作用已经发生。
 */
const DEVICE_OPS = new Set([
  'canonicalize', 'stat', 'list', 'read', 'mkdir', 'remove', 'rename', 'git', 'hash', 'walk', 'write',
])

const GIT_NULL = process.platform === 'win32' ? 'NUL' : os.devNull
const DEVICE_GIT_SAFE_CONFIG = [
  ['core.hooksPath', GIT_NULL],
  ['core.fsmonitor', 'false'],
  ['core.pager', 'cat'],
  ['core.quotepath', 'false'],
  ['core.alternateRefsCommand', ''],
  ['credential.helper', ''],
  ['diff.external', ''],
  ['commit.gpgSign', 'false'],
  ['tag.gpgSign', 'false'],
  ['log.showSignature', 'false'],
  ['gpg.program', ''],
  ['core.sshCommand', ''],
  ['core.gitProxy', ''],
  ['core.askPass', ''],
  ['protocol.allow', 'never'],
  // 文件协议与 ext 协议都可能把仓库内容变成"命令执行"（`ext::sh -c …`），
  // 设备桥只做只读查询，一个都不需要。
  ['protocol.file.allow', 'never'],
  ['protocol.ext.allow', 'never'],
]
const DEVICE_GIT_SERVER_PREFIX = [
  '--no-optional-locks',
  '-c', 'core.quotepath=false',
  '-c', 'core.fsmonitor=false',
  '-c', 'core.alternateRefsCommand=',
  '-c', 'diff.external=',
  '-c', 'core.pager=cat',
]
const DEVICE_GIT_COMMANDS = new Map([
  ['rev-parse', [
    ['rev-parse', '--is-inside-work-tree'],
    ['rev-parse', '--show-toplevel'],
    ['rev-parse', '--short', 'HEAD'],
    ['rev-parse', '--short=8', 'HEAD'],
    ['rev-parse', '--abbrev-ref', 'HEAD'],
  ]],
  ['status', [
    ['status', '--porcelain=v1', '-z'],
    ['status', '--porcelain=v1', '-z', '--untracked-files=normal'],
  ]],
  ['symbolic-ref', [['symbolic-ref', '--short', '-q', 'HEAD']]],
  ['init', [['init', '-q']]],
])
const DEVICE_GIT_ENV_KEYS = new Set([
  'PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR',
])

function isMirrorSecretFile(name) {
  const lower = name.toLowerCase()
  return lower === '.env' || lower.startsWith('.env.') || lower === '.npmrc' ||
    lower === '.netrc' || lower.startsWith('id_rsa') || lower.startsWith('id_ed25519') ||
    lower.startsWith('id_ecdsa') || lower.startsWith('id_dsa') || lower.startsWith('credentials') ||
    /\.(pem|key|p12|pfx|keystore|jks|ppk|kdbx)$/.test(lower)
}

/** 项目身份键：与网关 root_key 同口径（realpath → 归一 → 大小写按 OS）。 */
function canonicalRootKey(input) {
  let p = String(input || '').trim()
  if (!p) return ''
  try {
    p = fs.realpathSync.native(p)
  } catch {
    p = path.resolve(p)
  }
  p = stripWindowsVerbatim(p)
  const isUnc = p.startsWith('\\\\') || p.startsWith('//')
  let q = p.replace(/\\/g, '/')
  q = path.posix.normalize(q)
  if (isUnc && !q.startsWith('//')) q = '/' + q
  if (process.platform === 'win32' || process.platform === 'darwin') q = q.toLowerCase()
  return q
}

/** realpath，失败返回空串（不回退词法值 —— 回退就是逃逸漏洞的来源）。 */
function realpathStrict(p) {
  try {
    // EvalSymlinks/realpath 在 Windows 上可能返回 `\\?\` 扩展前缀；剥掉后再参与
    // 目录包含比较，否则同一目录的两种写法会被 path.relative 判成不同路径。
    return stripWindowsVerbatim(fs.realpathSync.native(p))
  } catch {
    return ''
  }
}

/**
 * 把任意路径解析成 canonical 形式：最深的已存在祖先走 realpath，不存在的尾部
 * 按字面拼回去。
 *
 * 这样"路径还不存在"（write/mkdir 的常见情况）也有 realpath 语义：
 * 一个指向根外的 junction 目录必然在祖先链上，realpath 一定看得见。
 *
 * @returns {{ path: string }|null} 无法解析（没有绝对路径 / 触碰顶层）时 null
 */
function canonicalCandidate(input) {
  const raw = stripWindowsVerbatim(String(input || '').trim())
  if (!raw || !path.isAbsolute(raw)) return null
  let probe = path.resolve(raw)
  const tail = []
  let base = realpathStrict(probe)
  while (!base) {
    const parent = path.dirname(probe)
    if (parent === probe) return null
    tail.unshift(path.basename(probe))
    probe = parent
    base = realpathStrict(probe)
  }
  for (const segment of tail) {
    if (!segment || segment === '.' || segment === '..' || path.isAbsolute(segment)) return null
  }
  return { path: tail.length ? path.join(base, ...tail) : base }
}

/**
 * 把 target 解析成 root 内的 canonical 绝对路径；出界 / 非法返回 null。
 *
 * realpath-only（H46）：只认 realpath，**没有词法回退分支**。修复前的实现是
 * "realpath 出了根就再按词法比一次"，那一层正是 symlink / NTFS junction 逃逸
 * 能成立的根因：词法上 `root\link\secret` 在根内，实际指向根外。
 *
 * 返回 canonical 路径而不是调用方给的原文：后续 fs 调用作用在**真实**位置上，
 * 不会出现"检查的是 A、操作的是 B"。
 */
function resolveInsideRoot(root, target) {
  const rawTarget = stripWindowsVerbatim(String(target || '').trim())
  if (!rawTarget || !path.isAbsolute(rawTarget)) return null
  // UNC / 网络路径一律不参与：既不是项目文件，也会把 SMB 凭据送到对端。
  if (isUncPath(rawTarget) || isUncPath(String(root || ''))) return null

  const rootCandidate = canonicalCandidate(root)
  if (!rootCandidate) return null
  let rootStat = null
  try {
    rootStat = fs.statSync(rootCandidate.path)
  } catch {
    rootStat = null
  }
  if (rootStat && !rootStat.isDirectory()) return null
  // 根还不存在时只做路径语义（canonical 形式上的包含判断）：
  // "能不能在这个根上操作"由 ensureRootApproved 的批准名单回答，不由这里回答。
  const targetCandidate = canonicalCandidate(rawTarget)
  if (!targetCandidate) return null
  return isWithin(rootCandidate.path, targetCandidate.path) ? targetCandidate.path : null
}

/**
 * 兼容入口：返回目标在 root 内的 canonical 绝对路径，出界返回 null。
 *
 * 与 `resolveInsideRoot` 同一套 realpath-only 规则；保留这个名字是因为既有调用点
 * 与测试都在用它。
 */
function ensureInside(root, target) {
  return resolveInsideRoot(root, target)
}


function sameArgs(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function validateDeviceGitArgs(args) {
  if (!Array.isArray(args) || args.length === 0 || args.length > 64) {
    throw new Error('invalid git args')
  }
  if (args.some(arg => typeof arg !== 'string' || arg.length > 2048 || arg.includes('\u0000'))) {
    throw new Error('invalid git args')
  }
  // Strip the gateway's exact reviewed prefix; refuse every other -c.
  const commandArgs = sameArgs(args.slice(0, DEVICE_GIT_SERVER_PREFIX.length), DEVICE_GIT_SERVER_PREFIX)
    ? args.slice(DEVICE_GIT_SERVER_PREFIX.length)
    : args
  const shapes = DEVICE_GIT_COMMANDS.get(commandArgs[0])
  if (!shapes || !shapes.some(shape => sameArgs(commandArgs, shape))) {
    throw new Error('invalid git args: disallowed git command or option')
  }
  return commandArgs
}

function deviceGitEnvironment() {
  const env = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (DEVICE_GIT_ENV_KEYS.has(key.toUpperCase())) env[key] = value
  }
  return {
    ...env,
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_GLOBAL: GIT_NULL,
    GIT_TERMINAL_PROMPT: '0',
    GIT_PAGER: 'cat',
    PAGER: 'cat',
    GIT_OPTIONAL_LOCKS: '0',
    GIT_ALLOW_PROTOCOL: '',
    GIT_PROTOCOL_FROM_USER: '0',
    LC_ALL: 'C',
    LANG: 'C',
  }
}

function deviceGitPrefix() {
  const prefix = ['--no-pager', '--no-optional-locks']
  for (const [key, value] of DEVICE_GIT_SAFE_CONFIG) prefix.push('-c', `${key}=${value}`)
  return prefix
}

function deviceGitConfigOverrides(stdout) {
  const filters = new Set()
  const diffDrivers = new Set()
  const aliases = new Set()
  const pagers = new Set()
  for (const entry of stdout.split('\0')) {
    if (!entry) continue
    const separator = entry.indexOf('\n')
    if (separator <= 0) throw new Error('malformed git configuration listing')
    const key = entry.slice(0, separator)
    const lowered = key.toLowerCase()
    const attribute = lowered.slice(lowered.lastIndexOf('.') + 1)
    if (lowered.startsWith('filter.') && ['clean', 'smudge', 'process'].includes(attribute)) {
      const driver = key.slice('filter.'.length, -(attribute.length + 1))
      if (!driver) throw new Error('malformed git filter configuration')
      filters.add(driver)
    } else if (lowered.startsWith('diff.') && ['textconv', 'command'].includes(attribute)) {
      const driver = key.slice('diff.'.length, -(attribute.length + 1))
      if (!driver) throw new Error('malformed git diff configuration')
      diffDrivers.add(driver)
    } else if (lowered.startsWith('alias.') && lowered.length > 'alias.'.length) {
      // 别名可以把任意字符串变成"看起来无害的子命令"。本端只跑白名单里的
      // 内置子命令，别名本来插不进来 —— 但仍然显式清空：仓库配置里留着一个
      // `alias.status=!curl …` 没有任何正当理由，而且将来放宽白名单时它就是后门。
      const name = key.slice('alias.'.length)
      if (!name) throw new Error('malformed git alias configuration')
      aliases.add(name)
    } else if (lowered.startsWith('pager.') && lowered.length > 'pager.'.length) {
      // `pager.<cmd>` 在分配 tty 时会执行任意命令；即使本端全用管道也不留口子。
      const name = key.slice('pager.'.length)
      if (!name) throw new Error('malformed git pager configuration')
      pagers.add(name)
    }
  }
  const overrides = []
  for (const driver of [...filters].sort()) {
    for (const attribute of ['clean', 'smudge', 'process']) {
      overrides.push('-c', `filter.${driver}.${attribute}=`)
    }
    overrides.push('-c', `filter.${driver}.required=false`)
  }
  for (const driver of [...diffDrivers].sort()) {
    overrides.push('-c', `diff.${driver}.textconv=`, '-c', `diff.${driver}.command=`)
  }
  for (const name of [...aliases].sort()) overrides.push('-c', `alias.${name}=`)
  for (const name of [...pagers].sort()) overrides.push('-c', `pager.${name}=cat`)
  return overrides
}

/**
 * 根授权检查（H46）。
 *
 * deps 里给出 `isRootGranted` 时才生效 —— 它是主进程注入的**用户批准名单**
 * （`approved-roots.cjs`）。单测直接调 `handleDeviceOp` 时不给 deps，走的是
 * 纯 op 逻辑；真实链路（`createDeviceFSBridge`）永远注入，所以生产路径没有
 * "忘了检查"的可能。
 *
 * 未授权时再给一次机会：`requestRootApproval` 由主进程弹原生对话框问用户，
 * 用户当场批准就继续。修复前这里根本没有这一层 —— 服务端说哪个 root 就是哪个。
 */
async function ensureRootApproved(msg, deps = {}) {
  const check = typeof deps.isRootGranted === 'function' ? deps.isRootGranted : null
  if (!check) return
  const op = String((msg && msg.op) || '')
  const candidates = []
  if (msg && msg.root) candidates.push(String(msg.root))
  // canonicalize 不带 root：它要探测的路径本身就是"待批准候选"。
  if (op === 'canonicalize' && msg && msg.path) candidates.push(String(msg.path))
  for (const candidate of candidates) {
    if (check(candidate)) continue
    let approved = false
    if (typeof deps.requestRootApproval === 'function') {
      try {
        approved = Boolean(await deps.requestRootApproval({
          root: candidate,
          op,
          path: String((msg && msg.path) || ''),
        }))
      } catch {
        approved = false
      }
    }
    if (!approved || !check(candidate)) throw new Error('root not granted')
  }
}

/** 执行一条设备 op。纯函数化便于单测（不依赖连接状态）。 */
async function handleDeviceOp(msg, deps = {}) {
  const op = String((msg && msg.op) || '')
  if (!DEVICE_OPS.has(op)) throw new Error(`unsupported op: ${op}`)
  await ensureRootApproved(msg, deps)
  const rawPath = String((msg && msg.path) || '').trim()
  if (op === 'canonicalize') {
    if (!rawPath || !path.isAbsolute(rawPath)) throw new Error('path must be absolute')
    if (isUncPath(rawPath)) throw new Error('unc path rejected')
    const clean = realpathStrict(rawPath) || stripWindowsVerbatim(path.resolve(rawPath))
    let st = null
    try {
      st = await fsp.stat(clean)
    } catch {
      /* 路径还不存在：canonicalize 仍返回词法键 */
    }
    return {
      clean_path: clean,
      root_key: canonicalRootKey(clean),
      exists: Boolean(st),
      is_dir: Boolean(st && st.isDirectory()),
    }
  }

  const root = String((msg && msg.root) || '')
  const abs = ensureInside(root, rawPath)
  if (!abs) throw new Error('path outside project root')

  if (op === 'stat') {
    const st = await fsp.stat(abs)
    return {
      stat: {
        size: st.size,
        is_dir: st.isDirectory(),
        mode: st.isFile() ? 'regular' : st.isDirectory() ? 'dir' : 'other',
      },
      is_dir: st.isDirectory(),
    }
  }

  if (op === 'list') {
    const entries = await fsp.readdir(abs, { withFileTypes: true })
    const truncated = entries.length > MAX_LIST_ENTRIES
    const list = entries.slice(0, MAX_LIST_ENTRIES).map(e => ({
      name: e.name,
      is_dir: e.isDirectory(),
      size: 0,
    }))
    return { entries: list, truncated }
  }

  if (op === 'read') {
    const max = Math.max(1, Math.min(Number(msg.max_bytes) || 65536, MAX_READ_BYTES))
    const offset = Math.max(0, Number(msg.offset) || 0)
    const fh = await fsp.open(abs, 'r')
    try {
      const buf = Buffer.alloc(max + 1)
      const { bytesRead } = await fh.read(buf, 0, max + 1, offset)
      const truncated = bytesRead > max
      const data = buf.subarray(0, Math.min(bytesRead, max))
      return { data_b64: data.toString('base64'), truncated }
    } finally {
      await fh.close()
    }
  }

  if (op === 'mkdir') {
    await fsp.mkdir(abs, { recursive: true })
    return {}
  }

  if (op === 'remove') {
    let st = null
    try {
      st = await fsp.lstat(abs)
    } catch (err) {
      if (err && err.code === 'ENOENT') return {}
      throw err
    }
    const expected = String(msg.expected_hash || '').toLowerCase()
    if (expected && !st.isDirectory()) {
      const current = await sha256File(abs)
      if (current !== expected) throw new Error('conflict')
    }
    if (st.isDirectory()) {
      await fsp.rm(abs, { recursive: true, force: true })
    } else {
      await fsp.unlink(abs)
    }
    return {}
  }

  if (op === 'rename') {
    const to = ensureInside(root, String(msg.to || ''))
    if (!to) throw new Error('path outside project root')
    await fsp.rename(abs, to)
    return {}
  }

  // Git accepts only the fixed commands used by the gateway; the device adds its own safeguards.
  if (op === 'git') {
    const args = Array.isArray(msg.args) ? msg.args : []
    validateDeviceGitArgs(args)
    const timeout = Math.max(1000, Math.min(Number(msg.timeout_ms) || GIT_TIMEOUT_MS, 120000))
    return await runDeviceGit(abs, args, timeout)
  }

  if (op === 'hash') {
    return { hash: await sha256File(abs) }
  }

  // walk：递归列出项目内文件（相对路径）。用于服务端镜像，不是通用文件浏览。
  // 超过上限时 truncated=true —— 服务端据此拒绝镜像，而不是让 agent 以为文件不存在。
  if (op === 'walk') {
    // walk 的根同样只认 realpath：root 本身穿越 junction 时，后面所有相对路径
    // 都会指到别的目录里去。
    const rootAbs = realpathStrict(root)
    if (!rootAbs) throw new Error('path outside project root')
    // 目录项若是 symlink/junction，Dirent 会报 isFile=false / isSymbolicLink=true —
    // 这里直接跳过，避免镜像把根外的内容抄走。
    const files = []
    let bytes = 0
    let truncated = false
    const stack = ['']
    while (stack.length) {
      const rel = stack.pop()
      const dirAbs = rel ? path.join(rootAbs, rel) : rootAbs
      let entries
      try {
        entries = await fsp.readdir(dirAbs, { withFileTypes: true })
      } catch {
        continue
      }
      for (const e of entries) {
        // .git 可能远大于项目源码且含本机路径，不能全量走设备桥；其它点文件
        // （.gitignore/.github 等）属于正常项目内容，继续受文件数/字节上限约束。
        if (e.name === '.git' || (e.isDirectory() && e.name === '.ssh') ||
          (!e.isDirectory() && isMirrorSecretFile(e.name))) continue
        const childRel = rel ? `${rel}/${e.name}` : e.name
        if (e.isDirectory()) {
          if (NOISE_DIRS.has(e.name)) continue
          stack.push(childRel)
          continue
        }
        if (!e.isFile()) continue
        let st
        try {
          st = await fsp.stat(path.join(rootAbs, childRel))
        } catch {
          continue
        }
        bytes += st.size
        if (files.length >= MAX_MIRROR_FILES || bytes > MAX_MIRROR_BYTES) {
          truncated = true
          break
        }
        files.push({ name: childRel, is_dir: false, size: st.size })
      }
      if (truncated) break
    }
    return { entries: files, truncated }
  }

  if (op === 'write') {
    const data = Buffer.from(String(msg.data_b64 || ''), 'base64')
    const expected = String(msg.expected_hash || '').toLowerCase()
    const existingHash = await sha256FileOrEmpty(abs)
    if (expected && existingHash && existingHash !== expected) {
      throw new Error('conflict')
    }
    if (expected && !existingHash) {
      throw new Error('conflict') // 期望覆盖已有文件，但它已不存在
    }
    await fsp.mkdir(path.dirname(abs), { recursive: true })
    const tmp = `${abs}.chiron-tmp-${process.pid}-${Date.now()}`
    await fsp.writeFile(tmp, data)
    await fsp.rename(tmp, abs)
    return { hash: await sha256File(abs) }
  }

  throw new Error(`unsupported op: ${op}`)
}

async function sha256FileOrEmpty(abs) {
  try {
    return await sha256File(abs)
  } catch (err) {
    if (err && err.code === 'ENOENT') return ''
    throw err
  }
}

async function sha256File(abs) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256')
    const stream = fs.createReadStream(abs)
    stream.on('data', chunk => h.update(chunk))
    stream.on('error', reject)
    stream.on('end', () => resolve(h.digest('hex')))
  })
}

function runGitProcess(cwd, args, timeoutMs, spawnGit) {
  return new Promise(resolve => {
    let child
    try {
      // 最小环境 + 天花板目录：git 不会沿着父目录往上找仓库，
      // 于是 cwd 之外的 `.git` 配置（hooks/aliases/filters）根本加载不到。
      const env = deviceGitEnvironment()
      env.GIT_CEILING_DIRECTORIES = path.dirname(path.resolve(cwd))
      child = spawnGit('git', args, {
        cwd,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env,
      })
    } catch (err) {
      const missing = err && err.code === 'ENOENT'
      resolve({
        code: missing ? 127 : -1,
        stdout: '',
        stderr: missing ? 'git not found' : String(err.message || err),
        truncated: false,
      })
      return
    }
    let stdout = []
    let stderr = []
    let stdoutBytes = 0
    let stderrBytes = 0
    let truncated = false
    let done = false
    let timer = null
    const outputText = chunks => Buffer.concat(chunks).toString('utf8')
    const finish = payload => {
      if (done) return
      done = true
      if (timer) clearTimeout(timer)
      resolve({ ...payload, stdout: outputText(stdout), stderr: outputText(stderr) })
    }
    timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* Already exited. */
      }
      stderr.push(Buffer.from(`git timeout after ${timeoutMs}ms`))
      finish({ code: -1, truncated })
    }, timeoutMs)
    child.stdout.on('data', chunk => {
      if (stdoutBytes >= MAX_GIT_OUTPUT) {
        truncated = true
        return
      }
      const data = chunk.length > MAX_GIT_OUTPUT - stdoutBytes
        ? chunk.subarray(0, MAX_GIT_OUTPUT - stdoutBytes)
        : chunk
      stdout.push(data)
      stdoutBytes += data.length
      if (data.length < chunk.length) truncated = true
    })
    child.stderr.on('data', chunk => {
      if (stderrBytes >= 64 * 1024) return
      const data = chunk.length > 64 * 1024 - stderrBytes
        ? chunk.subarray(0, 64 * 1024 - stderrBytes)
        : chunk
      stderr.push(data)
      stderrBytes += data.length
    })
    child.on('error', err => {
      const missing = err && err.code === 'ENOENT'
      stderr.push(Buffer.from(missing ? 'git not found' : String(err.message || err)))
      finish({ code: missing ? 127 : -1 })
    })
    child.on('close', code => finish({ code: code == null ? -1 : code }))
  })
}

/** Probe effective configuration before executing; an unreadable repository is never run unprotected. */
async function runDeviceGit(cwd, args, timeoutMs, deps = {}) {
  const commandArgs = validateDeviceGitArgs(args)
  const spawnGit = deps.spawnGit || spawn
  const prefix = deviceGitPrefix()
  const deadline = Date.now() + timeoutMs
  const probe = await runGitProcess(
    cwd,
    [...prefix, 'config', '--includes', '--null', '--list'],
    Math.min(timeoutMs, 10000),
    spawnGit,
  )
  if (probe.code === 127 && probe.stderr === 'git not found') return probe
  if (probe.code !== 0 || probe.truncated) {
    const detail = probe.stderr.trim() || `exit ${probe.code}`
    return {
      code: -1,
      stdout: '',
      stderr: `git repository configuration probe failed: ${detail}`,
      truncated: false,
    }
  }
  let overrides
  try {
    overrides = deviceGitConfigOverrides(probe.stdout)
  } catch (err) {
    return {
      code: -1,
      stdout: '',
      stderr: `git repository configuration probe failed: ${String(err.message || err)}`,
      truncated: false,
    }
  }
  return runGitProcess(
    cwd,
    [...prefix, ...overrides, ...commandArgs],
    Math.max(1, deadline - Date.now()),
    spawnGit,
  )
}

function toWsUrl(apiUrl) {
  const u = new URL(String(apiUrl || ''))
  if (u.protocol === 'https:') u.protocol = 'wss:'
  else if (u.protocol === 'http:') u.protocol = 'ws:'
  else throw new Error('unsupported api url protocol')
  u.pathname = '/ws/device-fs'
  u.search = ''
  u.hash = ''
  return u
}

/**
 * 用登录 token 换一张**短时一次性**连接票据。
 *
 * 为什么不是直接把 token 塞进 WS query（修复前的做法）：
 *   - query 串会被网关访问日志、反向代理、错误上报、崩溃转储完整记录；
 *   - WS 握手没有自定义头可用（浏览器/运行时限制），所以票据是唯一能"不落 token"
 *     的做法 —— 换取动作走普通 POST（可以带 Authorization 头），WS 只带票据。
 *
 * **fail-closed**：拿不到票据就返回失败，绝不退回 `?token=`。服务端未实现该端点时
 * 桥会停在 `lastError` 上，而不是"为了能用"降级到旧路径。
 *
 * 服务端 TODO（本 worktree 不改 Go）：
 *   - 实现 `POST /v1/device-fs/ticket`：校验 Ed25519 设备签名、把 public_key 与
 *     device_id 绑定、签发 ≤60s 且用后即焚的票据；
 *   - `GET /ws/device-fs` 只接受 `?ticket=`，并对已撤销设备回 401/403（HTTP）
 *     或 4401/4403（WS close code）。
 *
 * @param {object} options
 * @param {string} options.apiUrl 网关地址
 * @param {string} options.token 登录 token（只在 Authorization 头里出现）
 * @param {string} options.deviceId
 * @param {object} options.identity `ensureDeviceIdentity` 的结果
 * @param {Function} [options.fetchFn] 注入点（测试用）
 * @param {Function} [options.now]
 * @returns {Promise<{ ok: boolean, ticket?: string, expiresIn?: number, revoked?: boolean, message?: string }>}
 */
async function requestDeviceTicket({
  apiUrl,
  token,
  deviceId,
  identity,
  fetchFn = globalThis.fetch,
  now = Date.now,
}) {
  let url
  try {
    url = new URL(String(apiUrl || ''))
  } catch {
    return { ok: false, message: 'invalid api url' }
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return { ok: false, message: 'unsupported api url protocol' }
  }
  if (!identity || !identity.privateKey) {
    return { ok: false, message: 'device identity unavailable' }
  }
  if (typeof fetchFn !== 'function') {
    return { ok: false, message: 'fetch is unavailable in this runtime' }
  }
  url.pathname = TICKET_PATH
  url.search = ''
  url.hash = ''

  let proof
  try {
    proof = buildDeviceProof({ identity, deviceId, issuedAt: Number(now()) })
  } catch (err) {
    return { ok: false, message: `device proof failed: ${(err && err.message) || err}` }
  }

  let res
  try {
    res = await fetchFn(url.toString(), {
      method: 'POST',
      // token 只在头里；body 里连 token 的影子都不留，日志更安全。
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, ...proof }),
    })
  } catch (err) {
    return { ok: false, message: `ticket request failed: ${(err && err.message) || err}` }
  }

  const status = Number(res && res.status)
  if (status === 401 || status === 403) {
    // 与 HTTP 侧同一语义：凭据/设备被撤销 ⇒ 不要重连。
    return { ok: false, revoked: true, message: 'device bridge unauthorized (ticket rejected)' }
  }
  if (status === 404 || status === 405 || status === 501) {
    return {
      ok: false,
      message: 'device ticket endpoint unavailable (server support required)',
    }
  }
  if (!res.ok) return { ok: false, message: `ticket endpoint returned ${status}` }

  let payload
  try {
    payload = typeof res.json === 'function' ? await res.json() : null
  } catch {
    payload = null
  }
  const ticket = String((payload && payload.ticket) || '').trim()
  if (!ticket) return { ok: false, message: 'ticket endpoint returned no ticket' }
  return { ok: true, ticket, expiresIn: Number((payload && payload.expires_in) || 0) || 0 }
}

/**
 * 创建桥实例。只应由主进程在 cloud 模式启动；local 模式不连接。
 *
 * 长连接稳定性要点（P1）：
 *   - 同一时刻只允许一条连接：`dialing` 挡住并发 connect（schedule/start/换 token
 *     同时触发时会各建一条 socket，旧的那条再也不会被关闭）；
 *   - 回包必须发回**收到请求的那条 socket**，不能发到"当前"连接上，否则重连后
 *     旧请求的回包会被当成新连接的响应，服务端永远等不到自己的 id；
 *   - 旧 socket 后到的 close 事件不能把新连接标记为断开（否则 UI 抖动 + 重连风暴）；
 *   - 重连退避：3s 起，指数增长到 30s 封顶，连上后清零。
 *
 * 授权要点（H46）：
 *   - `isRootGranted`：主进程的批准名单，op 的 root 不在名单内就拒绝；
 *   - `requestRootApproval`：未授权 root 时由主进程弹原生对话框问用户；
 *   - 票据换取失败 / 被撤销 ⇒ `revoked`，停止重连，等 token 变化或显式 restart。
 *
 * @param {object} options
 * @param {import('electron').App} options.app
 * @param {() => Promise<string>} options.readToken  读取登录 token（safeStorage）
 * @param {(level:string,msg:string)=>void} [options.log]
 * @param {number} [options.reconnectMs]  初始重连间隔（测试可调小）
 * @param {number} [options.maxReconnectMs] 重连间隔上限
 * @param {(root:string)=>boolean} [options.isRootGranted] 用户批准的 canonical 根
 * @param {(info:object)=>Promise<boolean>} [options.requestRootApproval] 让用户批准根
 * @param {Function} [options.exchangeTicket] 换取票据（默认走 HTTP，测试可注入）
 * @param {object} [options.identity] 设备密钥（默认按 userData 现取）
 */
function createDeviceFSBridge({
  app,
  readToken,
  log = () => {},
  reconnectMs = RECONNECT_MS,
  maxReconnectMs = RECONNECT_MAX_MS,
  isRootGranted = null,
  requestRootApproval = null,
  exchangeTicket = null,
  identity = null,
  fetchFn = null,
}) {
  let ws = null
  let stopped = true
  let connected = false
  let lastError = ''
  let deviceId = ''
  let timer = null
  let currentApiUrl = ''
  let seq = 0
  let dialing = false
  let backoff = 0
  let revoked = false
  let cachedIdentity = identity

  function stateFile() {
    return path.join(app.getPath('userData'), 'device-id')
  }

  function deviceIdentity() {
    if (cachedIdentity) return cachedIdentity
    cachedIdentity = ensureDeviceIdentity({
      dir: app.getPath('userData'),
      log,
    })
    return cachedIdentity
  }

  function ensureDeviceId() {
    if (deviceId) return deviceId
    try {
      const raw = fs.readFileSync(stateFile(), 'utf8').trim()
      if (raw) {
        deviceId = raw
        return deviceId
      }
    } catch {
      /* 首次运行 */
    }
    deviceId = crypto.randomUUID()
    try {
      fs.mkdirSync(path.dirname(stateFile()), { recursive: true })
      fs.writeFileSync(stateFile(), deviceId, 'utf8')
      // 设备 id 不是凭据，只是同一台机器的稳定标识；文件权限收到用户可读写。
      fs.chmodSync(stateFile(), 0o600)
    } catch (err) {
      log('warn', `device-id persist failed: ${err.message}`)
    }
    return deviceId
  }

  function schedule(delay) {
    if (stopped) return
    if (timer) return
    let wait = delay
    if (wait == null) {
      wait = Math.min(reconnectMs * Math.pow(2, Math.min(backoff, 5)), maxReconnectMs)
      backoff = Math.min(backoff + 1, 6)
    }
    timer = setTimeout(() => {
      timer = null
      void connectOnce()
    }, wait)
  }

  /**
   * 把回包写回收到请求的那条连接。
   *
   * 断开的连接直接丢弃：服务端会把该 op 判成超时/离线，不会污染新连接。
   * 任何异常都只记日志 —— 长时间运行里一次 send 抛错不能变成未处理的 rejection
   * 把主进程带崩。
   */
  function sendOn(socket, payload) {
    if (!socket || socket.readyState !== WS_OPEN) return
    try {
      socket.send(payload)
    } catch (err) {
      log('warn', `device fs reply failed: ${(err && err.message) || err}`)
    }
  }

  async function connectOnce() {
    if (stopped || !currentApiUrl || dialing || revoked) return
    dialing = true
    try {
      let token = ''
      try {
        token = String((await readToken()) || '').trim()
      } catch {
        token = ''
      }
      if (stopped || !currentApiUrl) return
      if (!token) {
        lastError = 'not signed in'
        schedule(5000)
        return
      }
      let baseUrl
      try {
        baseUrl = toWsUrl(currentApiUrl)
      } catch (err) {
        lastError = err.message
        return
      }
      const id = ensureDeviceId()

      // 先换票据：拿不到就不连（fail-closed，绝不回退到 `?token=`）。
      const ticketResult = exchangeTicket
        ? await exchangeTicket({
          apiUrl: currentApiUrl,
          token,
          deviceId: id,
          identity: deviceIdentity(),
        })
        : await requestDeviceTicket({
          apiUrl: currentApiUrl,
          token,
          deviceId: id,
          identity: deviceIdentity(),
          fetchFn: fetchFn || globalThis.fetch,
        })
      if (stopped) return
      if (!ticketResult || !ticketResult.ok) {
        if (ticketResult && ticketResult.revoked) {
          // 撤销语义与 HTTP 一致：不重连、不重试，等 token 变化或显式 start。
          revoked = true
          lastError = String(ticketResult.message || 'device bridge revoked')
          log('error', `device fs bridge revoked: ${lastError}`)
          return
        }
        lastError = String((ticketResult && ticketResult.message) || 'ticket unavailable')
        log('warn', `device fs bridge ticket failed: ${lastError}`)
        schedule()
        return
      }

      const url = baseUrl
      url.searchParams.set('device_id', id)
      // 票据是一次性的短时凭据，可以出现在 URL 里；token 不行。
      url.searchParams.set('ticket', String(ticketResult.ticket))

      if (typeof WebSocket !== 'function') {
        lastError = 'WebSocket is unavailable in this runtime'
        return
      }
      let socket
      try {
        socket = new WebSocket(url.toString())
      } catch (err) {
        lastError = err.message
        schedule()
        return
      }
      ws = socket
      socket.addEventListener('open', () => {
        if (ws !== socket) return
        connected = true
        lastError = ''
        backoff = 0
        log('info', `device fs bridge connected (device=${id})`)
      })
      socket.addEventListener('message', ev => {
        let msg
        try {
          msg = JSON.parse(String(ev.data))
        } catch {
          return
        }
        if (!msg || !msg.id || msg.id === 'hello') return
        const replyId = msg.id
        void (async () => {
          let payload
          try {
            // 根授权在**执行前**再校验一次：连接建立后名单可能被用户撤销。
            const result = await handleDeviceOp(msg, { isRootGranted, requestRootApproval })
            payload = JSON.stringify({ id: replyId, ok: true, ...result })
          } catch (err) {
            payload = JSON.stringify({ id: replyId, ok: false, error: String((err && err.message) || err) })
          }
          sendOn(socket, payload)
        })()
      })
      socket.addEventListener('close', event => {
        // 旧连接的后到事件不能影响新连接的状态，也不能再排一次重连。
        if (ws !== socket) return
        connected = false
        ws = null
        const code = Number(event && event.code)
        if (code === WS_CLOSE_UNAUTHORIZED || code === WS_CLOSE_FORBIDDEN) {
          // 服务端撤销了票据/设备：与 HTTP 401/403 同口径，停止重连并落可观测状态。
          revoked = true
          lastError = `device bridge closed by server (${code})`
          log('error', `device fs bridge revoked: ${lastError}`)
          return
        }
        if (!stopped) {
          log('warn', 'device fs bridge disconnected; retrying')
          schedule()
        }
      })
      socket.addEventListener('error', () => {
        if (ws !== socket) return
        lastError = 'websocket error'
      })
    } finally {
      dialing = false
    }
  }

  function close() {
    connected = false
    const socket = ws
    // 先摘掉引用：本函数主动关闭的连接不应再触发一次重连调度（调用方自己会决定）。
    ws = null
    if (socket) {
      try {
        socket.close()
      } catch {
        /* ignore */
      }
    }
  }

  async function start(apiUrl) {
    const next = String(apiUrl || '').trim()
    if (!next) return
    if (!stopped && currentApiUrl === next && (connected || ws)) return
    stopped = false
    revoked = false
    currentApiUrl = next
    backoff = 0
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    close()
    await connectOnce()
  }

  function stop() {
    stopped = true
    backoff = 0
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    close()
    currentApiUrl = ''
  }

  function notifyTokenChanged() {
    if (stopped || !currentApiUrl) return
    // token 变了：撤销状态必须重判（用户可能刚重新登录，或管理员刚恢复设备）。
    revoked = false
    close()
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    void connectOnce()
  }

  function status() {
    return {
      deviceId: deviceId || null,
      connected,
      endpoint: currentApiUrl,
      lastError,
      revoked,
      seq: ++seq,
    }
  }

  return { start, stop, notifyTokenChanged, status, ensureDeviceId }
}

module.exports = {
  createDeviceFSBridge,
  handleDeviceOp,
  runDeviceGit,
  requestDeviceTicket,
  canonicalRootKey,
  ensureInside,
  resolveInsideRoot,
  toWsUrl,
  DEVICE_OPS,
  TICKET_PATH,
}
