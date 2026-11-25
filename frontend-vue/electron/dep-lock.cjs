'use strict'
/**
 * 运行时依赖的供应链策略（PRD 9.2 / H51）。
 *
 * 缺陷形态：`uv pip install -r <清单>` 既不要求 hash 锁，也会在失败后**自动换第三方
 * 镜像**。两件事叠在一起意味着：装到 venv 里的包既没有被固定在某个确切字节序列上，
 * 也可能来自一个我们从未审计过的源 —— 而那个 venv 随后会以用户身份执行模型生成的代码。
 *
 * 这里的口径是"要么可验证，要么明确说不"（fail-closed）：
 *   1. 有 `requirements.lock` 且**每条都带 `--hash`** ⇒ 装，并加 `--require-hashes`；
 *   2. 有 lock 但缺 hash ⇒ **拒绝**（半个哈希锁比没有更糟：让人以为验证过了）；
 *   3. 没有 lock ⇒ 默认**拒绝联网安装**；除非显式 `CHIRON_ALLOW_UNPINNED_DEPS=1`
 *      （开发/离线演练），此时放行但标注 `unpinned: true`，日志与界面都要说清；
 *   4. 允许的源只有 HTTPS 的批准主机；**不静默换源** —— 每个候选都是运维显式配置。
 *
 * 纯函数，便于把每条判据单独钉住。
 */

/** 批准的依赖源（HTTPS + 主机白名单）。顺序即优先级。 */
const APPROVED_INDEXES = [
  'https://pypi.org/simple',
  'https://pypi.tuna.tsinghua.edu.cn/simple',
  'https://mirrors.aliyun.com/pypi/simple',
]

const APPROVED_INDEX_HOSTS = new Set(
  APPROVED_INDEXES.map((u) => new URL(u).hostname.toLowerCase()),
)

/** 该 URL 是否可用作依赖源：必须 https，且主机在批准名单里。 */
function approvedIndexUrl(raw) {
  const text = String(raw || '').trim()
  if (!text) return { ok: true, url: '' } // 空 = 用 uv 默认（pypi.org）
  let url
  try {
    url = new URL(text)
  } catch {
    return { ok: false, reason: 'unparsable-index-url' }
  }
  if (url.protocol !== 'https:') {
    return { ok: false, reason: 'index-not-https' }
  }
  if (!APPROVED_INDEX_HOSTS.has(url.hostname.toLowerCase())) {
    return { ok: false, reason: 'index-host-not-approved' }
  }
  return { ok: true, url: url.toString() }
}

/**
 * 解析 hash 锁清单，判断它是否"真的锁住了"。
 *
 * 判据（每条 requirement 都要满足）：
 *   · 有 `==` 精确版本（`>=`/`~=` 之类不算锁）；
 *   · 至少一个 `--hash=sha256:`/`--hash=sha512:`（md5 之类弱算法不算）。
 */
function inspectLock(text) {
  const entries = []
  let current = ''
  for (const rawLine of String(text || '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    if (line.startsWith('-') && current) {
      // 续行（`--hash` 常写在后续行）
      current += ' ' + line
      continue
    }
    if (current) entries.push(current)
    current = line
  }
  if (current) entries.push(current)

  if (entries.length === 0) return { ok: false, count: 0, reason: 'lock-empty' }

  const unpinned = []
  const unhashed = []
  for (const entry of entries) {
    const name = entry.split(/[\s#]/)[0]
    if (!/[=]=/.test(entry) || /[<>~]=/.test(entry)) unpinned.push(name)
    if (!/--hash=(sha256|sha512):[0-9a-fA-F]+/.test(entry)) unhashed.push(name)
  }
  if (unpinned.length) return { ok: false, count: entries.length, reason: 'lock-not-pinned', packages: unpinned }
  if (unhashed.length) return { ok: false, count: entries.length, reason: 'lock-not-hashed', packages: unhashed }
  return { ok: true, count: entries.length }
}

/**
 * 决定这次安装能不能联网进行。
 *
 * @param {object} input
 * @param {string} [input.lockText] `requirements.lock` 内容（不存在时省略）
 * @param {string} [input.indexUrl] 生效的依赖源（已通过 approvedIndexUrl 校验）
 * @param {boolean} [input.allowUnpinned] 是否允许无锁联网安装（默认否）
 * @returns {{ok: boolean, requireHashes?: boolean, unpinned?: boolean, reason?: string, message?: string}}
 */
function installPolicy(input = {}) {
  const lockText = typeof input.lockText === 'string' ? input.lockText : ''
  const allowUnpinned = input.allowUnpinned === true

  if (lockText.trim()) {
    const lock = inspectLock(lockText)
    if (!lock.ok) {
      return {
        ok: false,
        reason: lock.reason,
        message:
          `requirements.lock 不可信（${lock.reason}` +
          (lock.packages ? `：${lock.packages.slice(0, 3).join(', ')}` : '') +
          '）。依赖必须精确固定版本并带 sha256/sha512 哈希。',
      }
    }
    return { ok: true, requireHashes: true, unpinned: false, packages: lock.count }
  }

  if (!allowUnpinned) {
    return {
      ok: false,
      reason: 'no-lock',
      message:
        '缺少 requirements.lock：默认拒绝联网安装依赖（无法证明装进来的字节是哪一份）。' +
        '如需临时放行，请显式设置 CHIRON_ALLOW_UNPINNED_DEPS=1。',
    }
  }
  return {
    ok: true,
    requireHashes: false,
    unpinned: true,
    message: '未启用哈希锁（CHIRON_ALLOW_UNPINNED_DEPS=1）：本次安装的依赖不可验证。',
  }
}

/**
 * 组装 uv 参数里的供应链相关部分。
 *
 * 顺序固定：`--require-hashes` 在 `-r` 之前 —— uv 对参数顺序不敏感，但保持固定能让
 * 日志与测试断言稳定。
 */
function supplyChainArgs(policy, lockPath, indexUrl) {
  const args = []
  if (policy && policy.ok && policy.requireHashes) args.push('--require-hashes')
  if (lockPath) args.push('-r', lockPath)
  if (indexUrl) args.push('--index-url', indexUrl)
  return args
}

module.exports = {
  APPROVED_INDEXES,
  APPROVED_INDEX_HOSTS,
  approvedIndexUrl,
  inspectLock,
  installPolicy,
  supplyChainArgs,
}
