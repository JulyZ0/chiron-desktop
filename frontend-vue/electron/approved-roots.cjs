/**
 * 用户批准的项目根（canonical root grant）。
 *
 * ## 存在的理由（H46 + H49 共用）
 *
 * 有两条入口会把「一个路径」交给主进程去操作，而路径的来源都**不可信**：
 *
 *   - 设备文件桥：服务端在 op 里直接下发 `root`，修复前本端接受任意绝对路径，
 *     随后就能 read/write/remove/rename；
 *   - `desktop:open-path`：工作台里的路径来自后端 API，修复前只查"绝对 + 存在"，
 *     一个被投毒的路径点一下就能用系统默认程序启动。
 *
 * 两处的正确口径是同一句话：**主进程只认用户批准过的 canonical 根**。本模块就是
 * 那份批准名单 —— 持久化在 userData 下，条目一律是 realpath 之后的形式，
 * 批准只能通过主进程自己弹出的原生对话框（见 main.cjs），渲染层无法自助批准。
 *
 * ## 为什么条目必须是 realpath
 *
 * 词法比较挡不住 symlink / NTFS junction：`D:\proj\link` 与 `D:\secrets` 是同一个
 * 目录，只有 `realpath` 之后才看得出。批准时归一、判定时也归一，两者不同步就会
 * 出现"批准了 A、实际操作了 B"的漏洞。
 *
 * ## 拒绝的根
 *
 * 盘根（`C:\`）、文件系统根（`/`）与 UNC（`\\server\share`）不能成为批准条目：
 * 批准它们的语义等价于"整机放行"，而对话框里显示一个 `C:\` 也起不到告知作用。
 */
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

const STATE_FILE = 'approved-roots.json'
const MAX_ROOTS = 64

function stripWindowsVerbatim(p) {
  const value = String(p || '')
  if (value.startsWith('\\\\?\\UNC\\')) return '\\\\' + value.slice(8)
  if (value.startsWith('\\\\?\\') || value.startsWith('\\\\.\\')) return value.slice(4)
  return value
}

/** canonical 形式：realpath（剥 `\\?\`），失败时退回词法绝对路径。 */
function canonicalizeDir(input) {
  const raw = stripWindowsVerbatim(String(input || '').trim())
  if (!raw) return ''
  try {
    return stripWindowsVerbatim(fs.realpathSync.native(raw))
  } catch {
    return stripWindowsVerbatim(path.resolve(raw))
  }
}

/** 比较键：Windows/macOS 大小写不敏感，统一小写 + 正斜杠。 */
function compareKey(p) {
  const value = stripWindowsVerbatim(String(p || '')).replace(/\\/g, '/').replace(/\/+$/, '')
  if (!value) return ''
  return process.platform === 'win32' || process.platform === 'darwin' ? value.toLowerCase() : value
}

/** target 是否等于 root 或位于其下（两边都应是 canonical 形式）。 */
function isWithin(root, target) {
  const r = compareKey(root)
  const t = compareKey(target)
  if (!r || !t) return false
  if (t === r) return true
  return t.startsWith(`${r}/`)
}

function isUncPath(p) {
  const value = stripWindowsVerbatim(String(p || '').trim())
  return value.startsWith('\\\\') || value.startsWith('//')
}

/**
 * 这个根能不能被批准。
 *
 * @param {string} input 用户/调用方给出的候选根
 * @param {{ platform?: string }} [options]
 * @returns {{ ok: boolean, root?: string, reason?: string, message?: string }}
 */
function screenRootCandidate(input, options = {}) {
  const platform = options.platform || process.platform
  const raw = String(input || '').trim()
  if (!raw) return { ok: false, reason: 'empty-root', message: '项目根为空' }
  if (!path.isAbsolute(raw)) return { ok: false, reason: 'root-not-absolute', message: '项目根必须是绝对路径' }
  if (isUncPath(raw)) {
    return { ok: false, reason: 'unc-root', message: '不接受 UNC / 网络共享作为项目根' }
  }
  const canonical = canonicalizeDir(raw)
  if (!canonical) return { ok: false, reason: 'empty-root', message: '项目根为空' }
  let st
  try {
    st = fs.statSync(canonical)
  } catch {
    return { ok: false, reason: 'root-missing', message: '项目根不存在' }
  }
  if (!st.isDirectory()) return { ok: false, reason: 'root-not-dir', message: '项目根不是目录' }
  const parsed = path.parse(canonical)
  if (compareKey(parsed.root) === compareKey(canonical) || canonical === parsed.root) {
    return { ok: false, reason: 'root-too-broad', message: '项目根不能是盘根或文件系统根' }
  }
  if (platform === 'win32' && canonical.length <= 3) {
    return { ok: false, reason: 'root-too-broad', message: '项目根不能是盘根' }
  }
  return { ok: true, root: canonical }
}

/**
 * 创建批准名单。
 *
 * @param {object} options
 * @param {string} options.dir 存放 JSON 的目录（main 传 `app.getPath('userData')`）
 * @param {(level: string, message: string) => void} [options.log]
 * @param {string} [options.file] 覆盖文件名（测试用）
 */
function createApprovedRoots(options = {}) {
  const dir = String(options.dir || '').trim()
  const log = typeof options.log === 'function' ? options.log : () => {}
  const file = path.join(dir, options.file || STATE_FILE)
  let roots = null

  function load() {
    if (roots) return roots
    roots = []
    try {
      const raw = JSON.parse(fs.readFileSync(file, 'utf8'))
      const list = Array.isArray(raw?.roots) ? raw.roots : []
      roots = list
        .filter(item => item && typeof item.root === 'string')
        .map(item => ({ root: item.root, addedAt: Number(item.addedAt) || 0 }))
        .filter(item => isAbsoluteRoot(item.root))
        .slice(0, MAX_ROOTS)
    } catch {
      roots = []
    }
    return roots
  }

  function isAbsoluteRoot(root) {
    return Boolean(root) && path.isAbsolute(root) && !isUncPath(root)
  }

  function persist() {
    if (!dir) return false
    try {
      fs.mkdirSync(dir, { recursive: true })
      const tmp = `${file}.tmp-${process.pid}-${crypto.randomUUID()}`
      fs.writeFileSync(tmp, JSON.stringify({ version: 1, roots: load() }, null, 2), { mode: 0o600 })
      fs.renameSync(tmp, file)
      return true
    } catch (err) {
      log('warn', `批准根持久化失败：${err?.message || err}`)
      return false
    }
  }

  return {
    /** 已批准的 canonical 根（副本）。 */
    list() {
      return load().map(item => item.root)
    },
    /** 批准一个根；候选不合格时抛错（调用方要能区分"用户拒绝"与"根不合法"）。 */
    grant(candidate) {
      const screened = screenRootCandidate(candidate)
      if (!screened.ok) {
        const err = new Error(screened.message)
        err.reason = screened.reason
        throw err
      }
      const entries = load()
      if (!entries.some(item => compareKey(item.root) === compareKey(screened.root))) {
        if (entries.length >= MAX_ROOTS) throw new Error('批准的项目根过多')
        entries.push({ root: screened.root, addedAt: Date.now() })
        persist()
      }
      return screened.root
    },
    revoke(candidate) {
      const key = compareKey(canonicalizeDir(candidate) || candidate)
      const entries = load()
      const next = entries.filter(item => compareKey(item.root) !== key)
      if (next.length === entries.length) return false
      roots = next
      persist()
      return true
    },
    /** target 是否落在某个已批准根内。 */
    covers(target) {
      const canonical = canonicalizeDir(target)
      if (!canonical) return false
      return load().some(item => isWithin(item.root, canonical))
    },
    /** 找到覆盖 target 的那个根（没有则 null）。 */
    rootFor(target) {
      const canonical = canonicalizeDir(target)
      if (!canonical) return null
      const hit = load().find(item => isWithin(item.root, canonical))
      return hit ? hit.root : null
    },
  }
}

module.exports = {
  STATE_FILE,
  canonicalizeDir,
  compareKey,
  isWithin,
  isUncPath,
  stripWindowsVerbatim,
  screenRootCandidate,
  createApprovedRoots,
}
