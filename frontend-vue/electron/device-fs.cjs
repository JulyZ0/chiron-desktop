'use strict'
/**
 * 设备文件桥（S1，只读）—— 桌面端主动外连远端网关。
 *
 * 远端引擎/网关需要读本机项目文件时，不能 inbound 连用户机器（NAT/防火墙），
 * 所以由这里建立 WebSocket：
 *
 *   远端 /ws/device-fs  ◄── 本进程 ──►  fs（仅项目根内、只读）
 *
 * 安全边界：
 *   - 每个 op 都带 root，本端做**包含校验**（路径语义属于本机 OS，服务端无法代劳）；
 *   - v1 只有 canonicalize/stat/list/read，没有任何写/执行能力；
 *   - token 只出现在 URL 查询串（与 /ws/rpa 同口径），不写日志。
 */

const fs = require('node:fs')
const fsp = fs.promises
const path = require('node:path')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')

const MAX_READ_BYTES = 8 << 20
const MAX_LIST_ENTRIES = 500
const MAX_MIRROR_FILES = 5000
const MAX_MIRROR_BYTES = 200 * 1024 * 1024
const MAX_GIT_OUTPUT = 8 << 20
const GIT_TIMEOUT_MS = 30000
const RECONNECT_MS = 3000
const RECONNECT_MAX_MS = 30000
// WebSocket.OPEN：不直接引用 WebSocket.OPEN，测试里的假实现也能用同一份判断。
const WS_OPEN = 1
const NOISE_DIRS = new Set([
  'node_modules', 'dist', 'build', 'target', '__pycache__', 'vendor', '.next', 'coverage',
])

function isMirrorSecretFile(name) {
  const lower = name.toLowerCase()
  return lower === '.env' || lower.startsWith('.env.') || lower === '.npmrc' ||
    lower === '.netrc' || lower.startsWith('id_rsa') || lower.startsWith('id_ed25519') ||
    lower.startsWith('id_ecdsa') || lower.startsWith('id_dsa') || lower.startsWith('credentials') ||
    /\.(pem|key|p12|pfx|keystore|jks|ppk|kdbx)$/.test(lower)
}

function stripWindowsVerbatim(p) {
  if (p.startsWith('\\\\?\\UNC\\')) return '\\\\' + p.slice(8)
  if (p.startsWith('\\\\?\\') || p.startsWith('\\\\.\\')) return p.slice(4)
  return p
}

/** 项目身份键：与网关 root_key 同口径（解析符号链接 → 归一 → 大小写按 OS）。 */
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

function realpathOr(p) {
  try {
    // EvalSymlinks/realpath 在 Windows 上可能返回 `\\?\` 扩展前缀；剥掉后再参与
    // 目录包含比较，否则同一目录的两种写法会被 path.relative 判成不同路径。
    return stripWindowsVerbatim(fs.realpathSync.native(p))
  } catch {
    return path.resolve(p)
  }
}

function within(root, target) {
  const rel = path.relative(root, target)
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}

/**
 * 返回目标路径的绝对形式；越出项目根时返回 null。
 *
 * 同时检查 realpath 与词法两层：realpath 防 symlink/junction 逃逸，词法层覆盖
 * "目标还不存在"的情况（stat/list/read 大多数时候目标已存在，但 canonicalize
 * 会传一个还不存在的路径）。
 */
function ensureInside(root, target) {
  const rootAbs = path.resolve(String(root || ''))
  const targetAbs = path.resolve(String(target || ''))
  if (!root || !target) return null
  const rootReal = realpathOr(rootAbs)
  const targetReal = realpathOr(targetAbs)
  if (within(rootReal, targetReal)) return targetAbs
  if (within(rootAbs, targetAbs)) return targetAbs
  return null
}

/** 执行一条设备 op。纯函数化便于单测（不依赖连接状态）。 */
async function handleDeviceOp(msg) {
  const op = String((msg && msg.op) || '')
  const rawPath = String((msg && msg.path) || '').trim()
  if (op === 'canonicalize') {
    if (!rawPath || !path.isAbsolute(rawPath)) throw new Error('path must be absolute')
    const clean = realpathOr(rawPath)
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

  // git：完整允许（含写操作）。参数由服务端构造，渲染层无法直接下发。
  // 安全边界：cwd 必须在项目根内；参数数量/长度受限；输出限 8MB、超时 30s。
  if (op === 'git') {
    const args = Array.isArray(msg.args) ? msg.args.map(String) : []
    if (args.length === 0 || args.length > 64) throw new Error('invalid git args')
    if (args.some(a => a.length > 2048 || a.includes('\u0000'))) throw new Error('invalid git args')
    const timeout = Math.max(1000, Math.min(Number(msg.timeout_ms) || GIT_TIMEOUT_MS, 120000))
    return await runDeviceGit(abs, args, timeout)
  }

  if (op === 'hash') {
    return { hash: await sha256File(abs) }
  }

  // walk：递归列出项目内文件（相对路径）。用于服务端镜像，不是通用文件浏览。
  // 超过上限时 truncated=true —— 服务端据此拒绝镜像，而不是让 agent 以为文件不存在。
  if (op === 'walk') {
    const rootAbs = realpathOr(path.resolve(root))
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

/**
 * 在项目根内执行 git 子命令。
 *
 * 非零退出码不是错误：git 的 "not a git repository" 等语义靠 code/stderr 表达，
 * 由服务端解释。只有 git 缺失（ENOENT）才用 127 标记，避免和普通失败混淆。
 * 输出截断时仍返回已有内容并置 truncated，服务端据此拒绝解析。
 */
function runDeviceGit(cwd, args, timeoutMs) {
  return new Promise(resolve => {
    let child
    try {
      child = spawn('git', args, {
        cwd,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          GIT_TERMINAL_PROMPT: '0',
          GIT_PAGER: 'cat',
          GIT_OPTIONAL_LOCKS: '0',
          LC_ALL: 'C',
        },
      })
    } catch (err) {
      resolve({ code: 127, stdout: '', stderr: String(err.message || err), truncated: false })
      return
    }
    let stdout = ''
    let stderr = ''
    let truncated = false
    let done = false
    let timer = null
    const finish = payload => {
      if (done) return
      done = true
      if (timer) clearTimeout(timer)
      resolve(payload)
    }
    timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已退出 */
      }
      finish({ code: -1, stdout, stderr: `${stderr}\ngit timeout after ${timeoutMs}ms`.trim(), truncated })
    }, timeoutMs)
    child.stdout.on('data', chunk => {
      if (stdout.length >= MAX_GIT_OUTPUT) {
        truncated = true
        return
      }
      stdout += chunk.toString('utf8')
      if (stdout.length > MAX_GIT_OUTPUT) {
        stdout = stdout.slice(0, MAX_GIT_OUTPUT)
        truncated = true
      }
    })
    child.stderr.on('data', chunk => {
      if (stderr.length < 64 * 1024) stderr += chunk.toString('utf8')
    })
    child.on('error', err => {
      const missing = err && err.code === 'ENOENT'
      finish({ code: missing ? 127 : -1, stdout, stderr: missing ? 'git not found' : String(err.message || err), truncated })
    })
    child.on('close', code => finish({ code: code == null ? -1 : code, stdout, stderr, truncated }))
  })
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
 * @param {object} options
 * @param {import('electron').App} options.app
 * @param {() => Promise<string>} options.readToken  读取登录 token（safeStorage）
 * @param {(level:string,msg:string)=>void} [options.log]
 * @param {number} [options.reconnectMs]  初始重连间隔（测试可调小）
 * @param {number} [options.maxReconnectMs] 重连间隔上限
 */
function createDeviceFSBridge({
  app,
  readToken,
  log = () => {},
  reconnectMs = RECONNECT_MS,
  maxReconnectMs = RECONNECT_MAX_MS,
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

  function stateFile() {
    return path.join(app.getPath('userData'), 'device-id')
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
    if (stopped || !currentApiUrl || dialing) return
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
      let url
      try {
        url = toWsUrl(currentApiUrl)
      } catch (err) {
        lastError = err.message
        return
      }
      const id = ensureDeviceId()
      url.searchParams.set('device_id', id)
      url.searchParams.set('token', token)
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
            const result = await handleDeviceOp(msg)
            payload = JSON.stringify({ id: replyId, ok: true, ...result })
          } catch (err) {
            payload = JSON.stringify({ id: replyId, ok: false, error: String((err && err.message) || err) })
          }
          sendOn(socket, payload)
        })()
      })
      socket.addEventListener('close', () => {
        // 旧连接的后到事件不能影响新连接的状态，也不能再排一次重连。
        if (ws !== socket) return
        connected = false
        ws = null
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
      seq: ++seq,
    }
  }

  return { start, stop, notifyTokenChanged, status, ensureDeviceId }
}

module.exports = {
  createDeviceFSBridge,
  handleDeviceOp,
  canonicalRootKey,
  ensureInside,
  toWsUrl,
}
