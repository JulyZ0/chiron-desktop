/**
 * `desktop:open-path` 的授权判定（H49）。
 *
 * ## 修复前的口径为什么不够
 *
 * 旧实现只查两件事：**是绝对路径** + **存在**。于是任何能把字符串塞进
 * `openPath({ path })` 的路径都能让系统按扩展名关联启动程序 —— 工作台的文件列表
 * 来自后端 API（`/v1/session-map/projects/:id/files`），一个被投毒或被入侵的服务端
 * 放一条 `{"name":"report.pdf.exe"}` 就能让用户"点开文件"时运行任意程序。
 *
 * ## 新口径
 *
 * 1. **canonical 根授权**：realpath 之后的路径必须落在用户批准过的项目根内；
 *    不在就**先问用户**（主进程原生对话框），用户拒绝即终止。
 *    盘根、文件系统根、UNC 一律不能成为批准条目（见 approved-roots.cjs）。
 * 2. **默认只做安全动作**：`reveal`（在文件管理器里定位，不执行）对已授权路径始终允许；
 *    打开非 `reveal` 时只放行白名单里的文档类型与目录。
 * 3. **可执行类型要独立确认**：可执行扩展名（exe/bat/cmd/ps1/js/lnk/msi/…）以及
 *    POSIX 上带执行位的文件，必须由主进程再弹一次**独立的、勾选式**确认才放行；
 *    渲染层传来一个 `confirmed: true` 不算数（这里根本不读这个字段）。
 * 4. **UNC / 网络路径直接拒绝**：打开 `\\host\share\x.exe` 会把 SMB 凭据送到对端，
 *    且断开的共享会挂起主进程；这类路径没有任何"确认一下就行"的余地。
 *
 * ## 为什么判定逻辑在纯模块里
 *
 * 对话框、`shell.openPath` 都是不可测的副作用。把"该不该开、开之前还要确认什么"
 * 抽成 `authorizeOpenPath`（可注入 `stat` / 两个确认回调）之后，就能用真实临时目录
 * 证明"可执行文件被拒""根外路径被拒""用户批准后才放行"。
 */
const fs = require('node:fs')
const path = require('node:path')

const { canonicalizeDir, isUncPath, isWithin, screenRootCandidate } = require('./approved-roots.cjs')

/**
 * 会执行代码的扩展名。命中即"必须独立确认"，不看关联程序怎么配。
 *
 * 为什么含 `.html` / `.svg`：它们在浏览器里能跑脚本，脚本又能读本地文件
 * （`file://` 同源策略对这种场景约束很弱），所以按"会执行"处理。
 */
const EXECUTABLE_EXTS = new Set([
  'exe', 'com', 'bat', 'cmd', 'ps1', 'psm1', 'psd1', 'vbs', 'vbe', 'js', 'jse', 'mjs', 'cjs',
  'wsf', 'wsh', 'msi', 'msp', 'mst', 'scr', 'pif', 'cpl', 'lnk', 'url', 'reg', 'hta', 'jar',
  'py', 'pyw', 'sh', 'bash', 'zsh', 'pl', 'rb', 'app', 'command', 'desktop', 'appimage',
  'dll', 'ocx', 'sys', 'drv', 'scf', 'gadget', 'chm', 'hh', 'apk', 'deb', 'rpm', 'pkg', 'dmg',
  'html', 'htm', 'svg', 'xhtml', 'shtml',
])

/** 打开即"看内容"的文档类型（不含能跑代码的格式）。 */
const SAFE_DOCUMENT_EXTS = new Set([
  'txt', 'md', 'markdown', 'rst', 'log', 'adoc', 'csv', 'tsv', 'json', 'jsonl', 'ndjson',
  'yaml', 'yml', 'toml', 'ini', 'conf', 'cfg', 'xml', 'sql', 'ipynb',
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'rtf', 'epub',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'tif', 'tiff', 'psd', 'heic',
  'mp3', 'wav', 'flac', 'ogg', 'm4a', 'mp4', 'mkv', 'avi', 'mov', 'webm',
  'zip', '7z', 'rar', 'gz', 'tgz', 'bz2', 'xz', 'tar', 'woff', 'woff2', 'ttf', 'otf',
])

function extensionOf(target) {
  const base = path.basename(String(target || ''))
  const dot = base.lastIndexOf('.')
  if (dot <= 0 || dot === base.length - 1) return ''
  return base.slice(dot + 1).toLowerCase()
}

/**
 * 目标类型：`dir` / `document` / `executable` / `unknown`。
 *
 * @param {{ target: string, isDirectory?: boolean, mode?: number, platform?: string }} input
 */
function classifyTarget(input = {}) {
  const platform = input.platform || process.platform
  if (input.isDirectory) return 'dir'
  const ext = extensionOf(input.target)
  if (EXECUTABLE_EXTS.has(ext)) return 'executable'
  if (SAFE_DOCUMENT_EXTS.has(ext)) return 'document'
  // POSIX 上的执行位：没有扩展名（典型 `./deploy`）也仍然可执行。
  if (platform !== 'win32' && Number(input.mode || 0) & 0o111) return 'executable'
  return 'unknown'
}

function fail(reason, message, extra = {}) {
  return { ok: false, reason, message, ...extra }
}

/**
 * 判定一次 `desktop:open-path` 请求。
 *
 * @param {object} request
 * @param {string} request.target 渲染层给出的路径
 * @param {boolean} [request.reveal] 只定位、不打开
 * @param {string} [request.root] 调用方声明的项目根（用于首次授权的提示）
 * @param {string[]} [request.roots] 已批准的 canonical 根
 * @param {string} [request.platform]
 * @param {(p: string) => Promise<{isDirectory: () => boolean, mode?: number}>} [request.stat]
 * @param {(info: { root: string, target: string }) => Promise<boolean>|boolean} [request.confirmRoot]
 * @param {(info: { path: string, kind: string, ext: string }) => Promise<boolean>|boolean} [request.confirmExecutable]
 * @returns {Promise<{ ok: boolean, action?: 'reveal'|'open', path?: string, kind?: string,
 *   reason?: string, message?: string, approvedRoot?: string }>}
 */
async function authorizeOpenPath(request = {}) {
  const platform = request.platform || process.platform
  const rawTarget = String(request.target || '').trim()
  const reveal = Boolean(request.reveal)
  const roots = Array.isArray(request.roots) ? request.roots.filter(Boolean) : []
  const stat = typeof request.stat === 'function' ? request.stat : fs.promises.stat

  if (!rawTarget) return fail('empty-path', '路径为空')
  if (!path.isAbsolute(rawTarget)) return fail('not-absolute', '只能打开绝对路径')
  if (isUncPath(rawTarget)) {
    return fail('unc-path', '不接受 UNC / 网络路径（可能泄露凭据或挂起）')
  }
  if (platform === 'win32' && /^[a-zA-Z]:$/.test(rawTarget)) {
    return fail('not-absolute', '只能打开绝对路径')
  }

  const target = canonicalizeDir(rawTarget)
  let st
  try {
    st = await stat(target)
  } catch {
    return fail('missing', '路径不存在')
  }

  // ── 1) 根授权 ────────────────────────────────────────────────────────────
  let approvedRoot = roots.find(root => isWithin(root, target)) || ''
  if (!approvedRoot) {
    const declared = String(request.root || '').trim()
    const candidate = declared
      ? screenRootCandidate(declared, { platform })
      : { ok: false, reason: 'no-declared-root', message: '路径不在已授权的项目根内' }
    if (!candidate.ok) {
      return fail('outside-approved-root',
        `路径不在已授权的项目根内（${candidate.message}）`, { kind: 'blocked' })
    }
    // 声明根必须真的覆盖目标，否则等于"用一个无关的根骗取授权"
    if (!isWithin(candidate.root, target)) {
      return fail('outside-approved-root', '路径不在声明的项目根内', { kind: 'blocked' })
    }
    const approved = typeof request.confirmRoot === 'function'
      ? Boolean(await request.confirmRoot({ root: candidate.root, target }))
      : false
    if (!approved) {
      return fail('root-not-approved', '用户未批准该项目根', { kind: 'blocked' })
    }
    approvedRoot = candidate.root
  }

  // ── 2) 类型 ──────────────────────────────────────────────────────────────
  const kind = classifyTarget({
    target,
    isDirectory: typeof st.isDirectory === 'function' ? st.isDirectory() : false,
    mode: st.mode,
    platform,
  })

  // reveal 不执行任何东西：定位即可，始终放行（已授权根内）。
  if (reveal) {
    return { ok: true, action: 'reveal', path: target, kind, approvedRoot }
  }
  if (kind === 'dir') {
    return { ok: true, action: 'open', path: target, kind, approvedRoot }
  }
  if (kind === 'document') {
    return { ok: true, action: 'open', path: target, kind, approvedRoot }
  }

  // ── 3) 可执行 / 未知类型：独立确认 ────────────────────────────────────────
  const confirm = typeof request.confirmExecutable === 'function' ? request.confirmExecutable : null
  const confirmed = confirm ? Boolean(await confirm({ path: target, kind, ext: extensionOf(target) })) : false
  if (!confirmed) {
    return fail(
      kind === 'executable' ? 'executable-needs-confirmation' : 'unknown-needs-confirmation',
      kind === 'executable'
        ? '该文件类型可执行，需要你在系统对话框中单独确认'
        : '该文件类型不在安全文档白名单内，需要你单独确认',
      { kind, path: target, approvedRoot },
    )
  }
  return { ok: true, action: 'open', path: target, kind, approvedRoot }
}

module.exports = {
  EXECUTABLE_EXTS,
  SAFE_DOCUMENT_EXTS,
  extensionOf,
  classifyTarget,
  authorizeOpenPath,
}
