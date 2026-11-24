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
 * ## H52：收口之后必须**审计**，且失败要能拦住落盘
 *
 * `/grant:r` + `/inheritance:r` 只能保证「当前账户与 SYSTEM 有完全控制」和
 * 「不再继承父目录」，**不会清掉别人已经在这台机器上写下的显式 ACE**（例如
 * 之前用别的账户跑过一次、或管理员加过 `BUILTIN\Users:(R)`）。只做前两步时，
 * 目录看起来"加固成功"，实际仍然对未授权主体开放。
 *
 * 因此加固在 `audit: true` 时变成五步：
 *   1) 补授权（避免继承移除瞬间无人可访问）；2) 移除继承；
 *   3) 读取并解析 DACL（显式 ACE 才看得见）；4) 逐个移除未授权主体；
 *   5) 再读一次验证 —— 仍有残留就返回 `ok:false`，由调用方拒绝写入密钥。
 *
 * 审计输出里的主体名做**脱敏**后再进日志/返回值：排障需要的是"还有几个未授权
 * 主体、属于哪个类别"，而不是把用户的域账户名再抄一份到日志里。
 *
 * ## 能力的边界（写进注释，避免后来者以为能防住更多）
 *
 * 收口只能挡住**其他 OS 用户**。同一用户下的任意进程都能读写这棵树 ——
 * 这是操作系统语义：用户对自己的配置文件目录有完全控制权。同用户场景的防线是
 * `secrets.cjs` 的 DPAPI 加密（密文只在原 OS 用户下可解），不是 ACL。
 *
 * Administrators 组成员同样可以 take-ownership 后读取，这一点无法用 ACL 阻止，
 * 也不在本模块的目标内（那需要 BitLocker 级别的全盘加密）。默认策略仍会移除
 * Administrators 的显式 ACE（它不在"当前账户 + SYSTEM"这条白名单里）；若宿主
 * 环境需要保留，由调用方显式传 `allowedExtra`。
 *
 * ## 失败不阻断启动，但必须阻断**落盘**
 *
 * 加固本身失败不该让应用起不来（权限操作可能因策略、网络盘或非标准文件系统
 * 失败）。调用方拿到 `{ ok: false, reason }` 后：记录脱敏 DACL、并**拒绝**把
 * token / secret 写进这个目录（见 `sensitive-store-gate.cjs`）——"没有加固成功
 * 就把密钥写进一个别人可能读得到的目录"不是可接受的降级。
 */
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

/** 目录树应有的权限串：OI=对象继承、CI=容器继承、F=完全控制 */
const OWNER_GRANT = '(OI)(CI)F'

/**
 * 允许出现在敏感目录 DACL 里的主体（归一化后比较：大写、去掉 `NT AUTHORITY\` 前缀）。
 *
 * `CREATOR OWNER` 不是普通 ACE 主体（只出现在继承模板里），不参与残余判定。
 */
const BUILTIN_ALLOWED_PRINCIPALS = new Set(['SYSTEM', 'NT AUTHORITY\\SYSTEM', 'CREATOR OWNER'])

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

/** 主体名归一化：icacls 在不同 locale 下 `NT AUTHORITY\SYSTEM` 与 `SYSTEM` 混用。 */
function normalizePrincipal(name) {
  return String(name || '').trim().replace(/\s+/g, ' ').toUpperCase()
}

/**
 * 脱敏主体名：脱敏后仍能区分"哪个类别"，但读不出完整账户名。
 *
 * `CORP\alice` → `C***\a***e`，SID（`S-1-5-21-...`）→ `S-1-5-***`，
 * 无法识别的短名 → `***`。日志里出现的是它，不是原文。
 */
function maskPrincipal(name) {
  const raw = String(name || '').trim()
  if (!raw) return '***'
  if (/^S-\d/i.test(raw)) {
    const parts = raw.split('-')
    return `${parts.slice(0, 3).join('-')}-***`
  }
  const cut = raw.lastIndexOf('\\')
  const domain = cut >= 0 ? raw.slice(0, cut) : ''
  const user = cut >= 0 ? raw.slice(cut + 1) : raw
  const maskUser = user.length <= 1
    ? '***'
    : `${user[0]}***${user.length > 2 ? user[user.length - 1] : ''}`
  if (!domain) return maskUser
  return `${domain[0]}***\\${maskUser}`
}

/**
 * 解析 `icacls <dir>` 的文本输出为 ACE 列表。
 *
 * 输出形如（首行带路径前缀，后续行只缩进主体名）：
 *
 *   C:\data DESKTOP\alice:(OI)(CI)(F)
 *           NT AUTHORITY\SYSTEM:(OI)(CI)(F)
 *           BUILTIN\Users:(I)(OI)(CI)(RX)
 *
 * 解析要点：
 *   - 权限串只由 `(X)` 组成，用行尾锚定的正则切出来，避免把主体名里的括号卷进去；
 *   - 首行前面的路径用**本次审计的目录**剥掉（大小写与分隔符不敏感），
 *     这样即使目录名带空格、主体名带空格也不会切错；
 *   - `成功处理/失败处理` 之类的本地化收尾行不含 `:(`，自然被忽略。
 *
 * @param {string} text icacls 输出
 * @param {string} [dir] 被审计目录，用于剥离首行路径前缀
 * @returns {Array<{ principal: string, perms: string, inherited: boolean, deny: boolean }>}
 */
function parseIcaclsAcl(text, dir = '') {
  const out = []
  const lines = String(text || '').split(/\r?\n/)
  let strippedDir = String(dir || '').replace(/[\\/]+$/, '')
  for (const rawLine of lines) {
    const line = rawLine.trim()
    if (!line) continue
    const tail = line.match(/:((?:\([^)]*\))+)\s*$/)
    if (!tail) continue
    let head = line.slice(0, tail.index)
    if (strippedDir && head.toLowerCase().startsWith(strippedDir.toLowerCase())) {
      head = head.slice(strippedDir.length)
    }
    const principal = head.trim()
    if (!principal) continue
    const perms = tail[1]
    out.push({
      principal,
      perms,
      inherited: /\(I\)/i.test(perms),
      deny: /\(DENY\)|\(D;/i.test(perms),
    })
  }
  return out
}

/**
 * 找出未授权 ACE。
 *
 * 白名单：当前账户、SYSTEM（含 `NT AUTHORITY\SYSTEM`）、调用方显式允许的额外主体。
 * 继承来的 ACE 在 `inheritance:r` 之后本不该存在；若仍出现，同样算未授权
 * （它们无法单独移除，只能整条继承清掉 —— 那种情况必须报失败而不是忽略）。
 *
 * @param {Array<object>} aces `parseIcaclsAcl` 的结果
 * @param {{ account?: string, allowedExtra?: string[] }} options
 */
function unauthorizedAces(aces, options = {}) {
  const allowed = new Set(BUILTIN_ALLOWED_PRINCIPALS)
  const account = normalizePrincipal(options.account)
  if (account) allowed.add(account)
  // 账户名可能与 USERDOMAIN 不同写法（`MACHINE\alice` vs `alice`）：
  // 只要名字部分相同就当作本人，否则所有本地账户都会被判成未授权。
  const accountUser = account.includes('\\') ? account.slice(account.lastIndexOf('\\') + 1) : account
  for (const extra of options.allowedExtra || []) {
    const normalized = normalizePrincipal(extra)
    if (normalized) allowed.add(normalized)
  }
  const result = []
  for (const ace of aces || []) {
    const normalized = normalizePrincipal(ace.principal)
    if (!normalized) continue
    if (allowed.has(normalized)) continue
    const user = normalized.includes('\\') ? normalized.slice(normalized.lastIndexOf('\\') + 1) : normalized
    if (accountUser && user === accountUser) continue
    if (normalized === 'CREATOR OWNER') continue
    result.push(ace)
  }
  return result
}

/** 脱敏后的 DACL 摘要：只保留主体类别与权限串，主体名已脱敏。 */
function describeDacl(aces) {
  return (aces || []).map(ace => ({
    principal: maskPrincipal(ace.principal),
    perms: String(ace.perms || ''),
    inherited: Boolean(ace.inherited),
  }))
}

/**
 * 给数据目录收口。
 *
 * @param {object} options
 * @param {string} options.dir 目标目录（绝对路径）
 * @param {string} [options.platform] 默认 `process.platform`，测试可注入
 * @param {string} [options.account] 默认由 `currentAccount()` 推断
 * @param {string[]} [options.allowedExtra] 额外允许保留的主体（归一化比较）
 * @param {boolean} [options.audit] 显式审计并移除未授权 ACE（H52）。生产调用方必须开。
 * @param {Function} [options.exec] 执行器，签名 `(file, args) => string|void`，默认 `execFileSync`
 * @returns {{ ok: boolean, reason?: string, skipped?: boolean, dacl?: object }}
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
      return { ok: true, dacl: { platform, mode: '0700' } }
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
      : (file, args) => require('node:child_process').execFileSync(file, args, {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      })

  try {
    for (const args of buildIcaclsArgs(dir, account)) exec('icacls', args)
  } catch (err) {
    return { ok: false, reason: `icacls-failed: ${err?.message || err}` }
  }

  if (!options.audit) return { ok: true }

  // ── 显式审计：读一次 DACL，移除未授权主体，再验证 ──
  const auditOptions = { account, allowedExtra: options.allowedExtra }
  let aces
  try {
    aces = parseIcaclsAcl(String(exec('icacls', [dir]) ?? ''), dir)
  } catch (err) {
    return { ok: false, reason: `acl-audit-failed: ${err?.message || err}` }
  }
  if (!aces.length) {
    // 读不出任何 ACE 说明解析或调用出了问题：不能把"没看见"当成"很干净"。
    return { ok: false, reason: 'acl-audit-empty' }
  }
  const offenders = unauthorizedAces(aces, auditOptions)
  if (!offenders.length) {
    return { ok: true, dacl: { entries: describeDacl(aces), residual: [] } }
  }

  // 逐主体移除。允许失败（可能是继承条目），失败后由再审计兜底。
  for (const ace of offenders) {
    try {
      exec('icacls', [dir, ace.deny ? '/remove:d' : '/remove:g', ace.principal])
    } catch {
      /* 交给再审计判定 */
    }
  }

  let after
  try {
    after = parseIcaclsAcl(String(exec('icacls', [dir]) ?? ''), dir)
  } catch (err) {
    return { ok: false, reason: `acl-reaudit-failed: ${err?.message || err}` }
  }
  const residual = unauthorizedAces(after, auditOptions)
  if (residual.length) {
    return {
      ok: false,
      reason: 'acl-residual',
      dacl: { entries: describeDacl(after), residual: describeDacl(residual) },
    }
  }
  return { ok: true, dacl: { entries: describeDacl(after), residual: [] } }
}

/**
 * 一行的脱敏 DACL 摘要，给日志用。
 *
 * 失败时优先列出残余主体数量与类别（不回显原文）；成功时给出条目数。
 */
function summarizeDacl(result) {
  const dacl = (result && result.dacl) || {}
  const entries = Array.isArray(dacl.entries) ? dacl.entries : []
  const residual = Array.isArray(dacl.residual) ? dacl.residual : []
  if (!entries.length && !residual.length) return 'dacl=<none>'
  const parts = [`entries=${entries.length}`, `residual=${residual.length}`]
  if (residual.length) parts.push(`residual_principals=${residual.map(r => r.principal).join(',')}`)
  return `dacl=(${parts.join(' ')})`
}

module.exports = {
  OWNER_GRANT,
  currentAccount,
  buildIcaclsArgs,
  parseIcaclsAcl,
  unauthorizedAces,
  describeDacl,
  maskPrincipal,
  normalizePrincipal,
  summarizeDacl,
  hardenDataDir,
}
