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
function compareVersions(a, b) {
  const norm = v =>
    String(v || '')
      .trim()
      .replace(/^v/i, '')
      .split(/[+-]/)[0]
      .split('.')
      .map(n => parseInt(n, 10) || 0)
  const x = norm(a)
  const y = norm(b)
  const len = Math.max(x.length, y.length)
  for (let i = 0; i < len; i += 1) {
    const d = (x[i] || 0) - (y[i] || 0)
    if (d !== 0) return d > 0 ? 1 : -1
  }
  return 0
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
function pickLatestRelease(releases) {
  const list = Array.isArray(releases) ? releases : []
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
  if (!candidates.length) return null
  return candidates.reduce((best, cur) => (compareVersions(cur.tag, best.tag) > 0 ? cur : best))
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
  parseLatestYml,
  compareVersions,
  pickLatestRelease,
  manifestUrlForTag,
  installerUrlForTag,
}
