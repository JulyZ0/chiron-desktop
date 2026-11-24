/**
 * 设备身份证明（H46）。
 *
 * ## 问题
 *
 * 设备文件桥原先只带一个 `device_id` 查询参数，而服务端对它的唯一要求是"非空"。
 * 于是一个人只要知道某个 `device_id`（它在日志、项目归属里到处出现），就能冒充
 * 那台机器连上来。`device_id` 是**标识**，不是**凭据**。
 *
 * ## 做法
 *
 * 每台设备在 userData 下生成一对 Ed25519 密钥（首次使用即创建，私钥 0600）：
 *
 *   - 公钥随票据申请一起上报，服务端把它与 `device_id` 绑定（首次信任 / 预注册）；
 *   - 每次申请连接票据都对 `device_id + issued_at + nonce` 签名 —— 没有私钥就签不出；
 *   - 票据是短时、一次性的，用来换 WebSocket 连接，**token 不再出现在 URL 里**。
 *
 * 签名文本用换行分隔的固定字段顺序，避免"拼接歧义"（`a` + `bc` 与 `ab` + `c`）。
 * 校验侧（服务端）必须用同一份文本重建，所以这里单独导出 `proofMessage`。
 *
 * ## 仍需服务端配合（TODO，不在本 worktree 的改动范围）
 *
 *   - `POST /v1/device-fs/ticket`：校验 Ed25519 签名、把 `public_key` 与 `device_id`
 *     绑定、签发一次性短时票据（建议 ≤60s、用后即焚）；
 *   - `GET /ws/device-fs?ticket=…`：只认票据，不再接受 `?token=`；
 *   - 撤销：与 HTTP 侧同一套语义 —— 票据无效/设备被吊销时用 401/403（HTTP）
 *     或 WebSocket close code 4401/4403（WS）拒绝，而不是仅断开连接。
 *
 * 客户端这一侧已经是 fail-closed：拿不到票据就不连，不会退回 query token。
 */
const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

const KEY_FILE = 'device-key.pem'
const PROOF_VERSION = 1

/** 签名文本：版本 + 设备 id + 签发时间 + nonce，换行分隔。 */
function proofMessage({ deviceId, issuedAt, nonce }) {
  return [
    `chiron-device-proof/v${PROOF_VERSION}`,
    `device_id=${deviceId}`,
    `issued_at=${issuedAt}`,
    `nonce=${nonce}`,
  ].join('\n')
}

/**
 * 读取或生成设备密钥对。
 *
 * 私钥只落本机（`0600`，Windows 上靠 DPAPI 之外的 ACL 收口，见 data-dir-acl.cjs）。
 * 生成失败时返回 `{ ok: false }` —— 调用方据此拒绝连接，而不是退化成无签名设备。
 *
 * @param {{ dir: string, log?: (level: string, message: string) => void, keyFile?: string }} options
 */
function ensureDeviceIdentity(options = {}) {
  const dir = String(options.dir || '').trim()
  const log = typeof options.log === 'function' ? options.log : () => {}
  const file = path.join(dir, options.keyFile || KEY_FILE)
  if (!dir) return { ok: false, reason: 'no-dir' }

  try {
    const existing = fs.readFileSync(file, 'utf8')
    const privateKey = crypto.createPrivateKey(existing)
    return describeIdentity(privateKey)
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      // 存在但读不出来：不要悄悄换一把新钥匙（那等于把服务端已绑定的身份丢掉）
      log('warn', `device identity 读取失败：${err?.message || err}`)
      return { ok: false, reason: 'read-failed' }
    }
  }

  try {
    const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519')
    const pem = privateKey.export({ type: 'pkcs8', format: 'pem' })
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(file, pem, { mode: 0o600 })
    return describeIdentity(privateKey)
  } catch (err) {
    log('warn', `device identity 生成失败：${err?.message || err}`)
    return { ok: false, reason: 'generate-failed' }
  }
}

function describeIdentity(privateKey) {
  const publicKey = crypto.createPublicKey(privateKey)
  const publicRaw = publicKey.export({ type: 'spki', format: 'der' })
  return {
    ok: true,
    privateKey,
    publicKey,
    publicKeySpki: publicRaw.toString('base64'),
    keyId: crypto.createHash('sha256').update(publicRaw).digest('hex').slice(0, 32),
  }
}

/**
 * 生成一次设备归属证明。
 *
 * @param {{ identity: object, deviceId: string, nonce?: string, issuedAt?: number }} input
 * @returns {{ version: number, nonce: string, issued_at: number, signature: string,
 *   public_key: string, key_id: string }}
 */
function buildDeviceProof(input = {}) {
  const identity = input.identity
  if (!identity || !identity.privateKey) throw new Error('device identity unavailable')
  const deviceId = String(input.deviceId || '').trim()
  if (!deviceId) throw new Error('device id required')
  const issuedAt = Number(input.issuedAt) || Date.now()
  const nonce = String(input.nonce || crypto.randomBytes(16).toString('hex'))
  const signature = crypto.sign(
    null,
    Buffer.from(proofMessage({ deviceId, issuedAt, nonce }), 'utf8'),
    identity.privateKey,
  ).toString('base64')
  return {
    version: PROOF_VERSION,
    nonce,
    issued_at: issuedAt,
    signature,
    public_key: identity.publicKeySpki,
    key_id: identity.keyId,
  }
}

/**
 * 校验一次设备归属证明（服务端要做的同一件事；本端单测也用它）。
 *
 * @param {{ proof: object, deviceId: string, publicKey?: object }} input
 */
function verifyDeviceProof(input = {}) {
  const proof = input.proof || {}
  if (proof.version !== PROOF_VERSION) return false
  const deviceId = String(input.deviceId || '').trim()
  if (!deviceId) return false
  let publicKey = input.publicKey
  if (!publicKey) {
    try {
      publicKey = crypto.createPublicKey({
        key: Buffer.from(String(proof.public_key || ''), 'base64'),
        format: 'der',
        type: 'spki',
      })
    } catch {
      return false
    }
  }
  try {
    return crypto.verify(
      null,
      Buffer.from(proofMessage({
        deviceId,
        issuedAt: proof.issued_at,
        nonce: proof.nonce,
      }), 'utf8'),
      publicKey,
      Buffer.from(String(proof.signature || ''), 'base64'),
    )
  } catch {
    return false
  }
}

module.exports = {
  KEY_FILE,
  PROOF_VERSION,
  proofMessage,
  ensureDeviceIdentity,
  buildDeviceProof,
  verifyDeviceProof,
}
