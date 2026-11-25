/**
 * 在线升级的纯逻辑：清单解析、版本比较、公开版本选择。
 *
 * 放在 main.cjs 之外的原因与 shared.cjs 相同 —— main.cjs 一 require 就会起 Electron，
 * 单测没法覆盖；而"选哪个版本"的判据错了，用户要么永远收不到更新，要么被降级到旧包。
 *
 * ## 为什么不能只认 `releases/latest`
 *
 * 本仓的发布流程把安装包标成 **pre-release**（未签名，公开但不算"latest"）。
 * GitHub 的 `/releases/latest/download/latest.yml` 只看非预发布版本，于是必然 404 ——
 * 这不是"还没发布"，重试也不会变好。客户端必须能从公开的发布列表里自己挑最新版，
 * 否则每次检查更新都只能得到一条误导性的错误。
 */

/** 极简 latest.yml 解析：只取 version / files[].url / files[].sha512 / path。 */
function parseLatestYml(text) {
  const lines = String(text || '').split(/\r?\n/)
  let version = ''
  let path = ''
  const files = []
  let current = null
  for (const raw of lines) {
    const line = raw.trimEnd()
    const top = line.match(/^([A-Za-z0-9_]+):\s*(.*)$/)
    if (top) {
      const key = top[1]
      const value = top[2].trim().replace(/^['"]|['"]$/g, '')
      if (key === 'version') version = value
      if (key === 'path') path = value
      if (key === 'files') current = null
      continue
    }
    const item = line.match(/^\s*-\s*url:\s*(.*)$/)
    if (item) {
      current = { url: item[1].trim().replace(/^['"]|['"]$/g, '') }
      files.push(current)
      continue
    }
    const field = line.match(/^\s+(sha512|size):\s*(.*)$/)
    if (field && current) {
      current[field[1]] = field[2].trim().replace(/^['"]|['"]$/g, '')
    }
  }
  const installers = files.filter(f => f.url && /\.(exe|msi)$/i.test(f.url))
  const chosen = installers.find(f => /x64/i.test(f.url)) || installers[0] || null
  if (!chosen && path) {
    return { version, file: { url: path, sha512: '' } }
  }
  return { version, file: chosen }
}

/** 语义化版本比较（够用即可：只处理数字段与预发布后缀）。 */
/**
 * 解析 semver（H57）：`MAJOR.MINOR.PATCH[-prerelease][+build]`。
 *
 * 非法输入返回 `ok:false`，由调用方按"不可比较"处理 —— 绝不能把垃圾 tag 当成 0.0.0
 * 参与比较（旧实现就是这么做的：`parseInt('beta') || 0` 让 `2.0.0-beta` 与 `2.0.0`
 * 完全相等，于是**预发布版本永远拿不到更新**，而这是本仓的发版常态）。
 */
function parseSemver(input) {
  const text = String(input || '').trim().replace(/^v/i, '')
  const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$/.exec(text)
  if (!m) {
    // 容错：允许只有两位的版本（`1.2` → `1.2.0`），这是历史清单里真实存在的写法。
    const loose = /^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$/.exec(text)
    if (!loose) return { ok: false, raw: text }
    return {
      ok: true,
      raw: text,
      major: Number(loose[1] || 0),
      minor: Number(loose[2] || 0),
      patch: Number(loose[3] || 0),
      prerelease: loose[4] ? loose[4].split('.') : [],
      build: loose[5] || '',
    }
  }
  return {
    ok: true,
    raw: text,
    major: Number(m[1]),
    minor: Number(m[2]),
    patch: Number(m[3]),
    prerelease: m[4] ? m[4].split('.') : [],
    build: m[5] || '',
  }
}

/** 单个 prerelease 标识符比较：数字段按数值，其余按字典序，数字 < 字母（semver 规范）。 */
function comparePrereleaseIdentifier(a, b) {
  const aNum = /^\d+$/.test(a)
  const bNum = /^\d+$/.test(b)
  if (aNum && bNum) {
    const d = Number(a) - Number(b)
    return d === 0 ? 0 : d > 0 ? 1 : -1
  }
  if (aNum) return -1
  if (bNum) return 1
  if (a === b) return 0
  return a > b ? 1 : -1
}

/**
 * 语义化版本优先级比较（H57）：返回 1 / 0 / -1。
 *
 * 规则（semver §11）：数字段逐位比较；然后
 *   - 都是正式版 → 相等；
 *   - 一方有 prerelease → 有 prerelease 的**更低**（`2.0.0-beta < 2.0.0`）；
 *   - 都带 prerelease → 逐标识符比较，前缀相同则更长者更高。
 * `+build` 不参与优先级（只做标识）。
 */
function compareVersions(a, b) {
  const x = parseSemver(a)
  const y = parseSemver(b)
  if (!x.ok || !y.ok) {
    // 不可解析：退化到"字符串不等即视为不同"，且**不声称谁更高**（返回 0）。
    // 调用方 pickLatestRelease 会因此保留它已经选中的那个，而不是随机降级。
    return 0
  }
  for (const key of ['major', 'minor', 'patch']) {
    const d = x[key] - y[key]
    if (d !== 0) return d > 0 ? 1 : -1
  }
  const xp = x.prerelease
  const yp = y.prerelease
  if (!xp.length && !yp.length) return 0
  if (!xp.length) return 1
  if (!yp.length) return -1
  const len = Math.max(xp.length, yp.length)
  for (let i = 0; i < len; i += 1) {
    if (i >= xp.length) return -1
    if (i >= yp.length) return 1
    const d = comparePrereleaseIdentifier(xp[i], yp[i])
    if (d !== 0) return d
  }
  return 0
}

/** 是否是预发布版本（带 prerelease 段）。 */
function isPrereleaseVersion(input) {
  const parsed = parseSemver(input)
  return parsed.ok && parsed.prerelease.length > 0
}
/**
 * 从 GitHub Releases API 的列表里挑最新的**可升级**版本。
 *
 * - 草稿排除（API 对未授权请求本来也不返回草稿，这里是第二道保险）；
 * - 必须带 `latest.yml`：没有清单就没有 SHA-512，下载阶段会拒绝执行；
 * - 只按版本号取最大者，**不**因 `prerelease` 标记降级 —— 本仓的发布全部是
 *   预发布（未签名），按 GitHub 的"稳定版优先"语义筛一次就等于"永远没有更新"。
 *   预发布标记只影响 `/releases/latest` 的指向，不影响它能不能装。
 */
function pickLatestRelease(releases, options = {}) {
  const list = Array.isArray(releases) ? releases : []
  // 默认渠道**接受** prerelease：本仓的发版常态就是预发布（见文件头注释），
  // 默认设成 stable 等于"默认永远没有更新"。要只在正式版渠道时显式传 channel: 'stable'。
  const channel = options.channel ? String(options.channel).toLowerCase() : ''
  const allowPrerelease = channel === '' || channel === 'prerelease' || options.allowPrerelease === true
  // 撤回名单按 **semver 规范化后**比较：`v2.2.0` 与 `2.2.0` 是同一个版本。
  const normalizeTag = t => {
    const parsed = parseSemver(t)
    return parsed.ok ? parsed.raw.toLowerCase() : String(t || '').trim().toLowerCase()
  }
  const withdrawn = new Set(
    (Array.isArray(options.withdrawn) ? options.withdrawn : []).map(normalizeTag),
  )
  const pinned = String(options.pin || '').trim()
  const current = String(options.current || '').trim()

  const candidates = list
    .filter(r => r && r.draft !== true && typeof r.tag_name === 'string' && r.tag_name.trim())
    .map(r => ({
      tag: r.tag_name.trim(),
      htmlUrl: typeof r.html_url === 'string' ? r.html_url : '',
      prerelease: r.prerelease === true,
      hasManifest:
        Array.isArray(r.assets) &&
        r.assets.some(a => a && typeof a.name === 'string' && a.name.toLowerCase() === 'latest.yml'),
    }))
    .filter(r => r.hasManifest)
    // H57：撤回的版本绝不进候选 —— 撤回的语义是"这个包有问题"，
    // 光靠 tag 排序永远绕不过它（旧实现只按版本号取最大）。
    .filter(r => !withdrawn.has(normalizeTag(r.tag)))
    // H57：channel 语义 —— stable 渠道不接受带 prerelease 段的版本。
    // 默认仍是"接受"（本仓发布全是 prerelease，见文件头注释），
    // 但显式传 channel=stable 时必须真的过滤，否则这个参数就是摆设。
    .filter(r => allowPrerelease || !isPrereleaseVersion(r.tag))

  if (!candidates.length) return null

  // H57：pin 语义 —— 被 pin 的版本不自动"升级"到别的版本，也不自动降级。
  // 只有当候选里**恰好**存在被 pin 的那个 tag 时才返回它，否则视为"没有可升级项"。
  if (pinned) {
    const exact = candidates.find(c => c.tag.toLowerCase() === pinned.toLowerCase())
    return exact || null
  }

  const best = candidates.reduce((acc, cur) => (!acc || compareVersions(cur.tag, acc.tag) > 0 ? cur : acc), null)

  // 不降级：候选不比当前版本新时，什么都不做（返回 null 由调用方当成"已是最新"）。
  // 旧实现只取"最大"，在清单里混进旧 tag 时会把用户**降级**到旧包。
  if (current && compareVersions(best.tag, current) <= 0) return null
  return best
}
/** 指定 tag 的清单地址（草稿/预发布也能用，不经过 /releases/latest） */
function manifestUrlForTag(repo, tag) {
  return `https://github.com/${repo}/releases/download/${encodeURIComponent(tag)}/latest.yml`
}

/** 指定 tag 的安装包地址；tag 缺省时沿用 /releases/latest 语义 */
function installerUrlForTag(repo, tag, file) {
  const dir = tag
    ? `releases/download/${encodeURIComponent(tag)}`
    : 'releases/latest/download'
  return `https://github.com/${repo}/${dir}/${encodeURIComponent(file)}`
}

module.exports = {
  parseSemver,
  isPrereleaseVersion,
  comparePrereleaseIdentifier,
  parseLatestYml,
  compareVersions,
  pickLatestRelease,
  manifestUrlForTag,
  installerUrlForTag,
}
