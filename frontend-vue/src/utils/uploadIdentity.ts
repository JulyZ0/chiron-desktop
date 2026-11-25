/**
 * 上传续传元数据的身份绑定（PRD 9.10 / H72）。
 *
 * 缺陷形态：`chunk-upload:*` 的键里只有 `purpose/parentId/文件名指纹` 与一个
 * **可能为空**的用户 id，而值就是裸 `upload_id`。于是：
 *   - `localStorage` 是浏览器级的：换账号/换租户后，同一个键还能读到**上一个身份**的
 *     `upload_id`；而 `upload_id` 属于另一个后端身份 ⇒ 续传会把别人的分片会话接着写，
 *     或者拿到 404 后永不清理（下次进来又撞同一把坏钥匙）。
 *   - 同一账号在**不同部署**（本地模式 ↔ 团队云、不同 origin）之间也不隔离：
 *     `upload_id` 是服务端自增/随机，跨实例毫无意义。
 *   - 键里只有文件名/大小/时间，**没有用途之外的归属**，无法在读取时判断"这份状态
 *     是不是我这个身份的"。
 *
 * 治法：值改成带版本的信封 `{v, uploadId, owner:{user,tenant,mode,origin}, fingerprint, savedAt}`，
 * 读取时**逐项比对**当前身份；任何不一致都视为"不可信"并丢弃（宁可重传，不可串号）。
 */

export interface UploadOwner {
  /** 账号 id；空串表示当前没有身份（未登录 / 首屏还没拉到 profile）。 */
  userId: string
  tenantId: string
  /** `local` | `cloud`。 */
  mode: string
  /** API 认证域（`scheme://host:port`），不同部署不共享 upload_id。 */
  origin: string
}

export interface UploadEnvelope {
  v: 2
  uploadId: string
  owner: UploadOwner
  /** 文件摘要（名/大小/修改时间/分片大小），用于确认"还是同一个文件"。 */
  fingerprint: string
  savedAt: string
}

export const UPLOAD_ENVELOPE_VERSION = 2

/** 缺字段也接受（老版本/测试里可能只给一部分），但缺的项会参与比对并被判为"不一致"。 */
export function normalizeUploadOwner(raw: Partial<UploadOwner> | undefined | null): UploadOwner {
  return {
    userId: String(raw?.userId ?? ''),
    tenantId: String(raw?.tenantId ?? ''),
    mode: String(raw?.mode ?? ''),
    origin: String(raw?.origin ?? '').trim(),
  }
}

/**
 * 两个身份是否可共享同一份续传状态。
 *
 * 刻意**严格**：任一项不同就 false。宁可让用户重传一次，也不要出现"接着别人的
 * upload_id 写分片"这种跨身份写入 —— 后者在服务端看起来是完全合法的请求。
 */
export function sameUploadOwner(a: UploadOwner, b: UploadOwner): boolean {
  return (
    a.userId === b.userId &&
    a.tenantId === b.tenantId &&
    a.mode === b.mode &&
    a.origin === b.origin
  )
}

/** 没有身份时不允许持久化续传状态（否则会落到共享的 "anonymous" 桶里）。 */
export function canPersistUpload(owner: UploadOwner): boolean {
  return Boolean(owner.userId) && Boolean(owner.origin)
}

/** 序列化：空 uploadId 或没有身份 ⇒ 返回空串（调用方据此删除键）。 */
export function encodeUploadEnvelope(input: {
  uploadId: string
  owner: UploadOwner
  fingerprint: string
  savedAt?: string
}): string {
  const uploadId = String(input.uploadId || '')
  const owner = normalizeUploadOwner(input.owner)
  if (!uploadId) return ''
  if (!canPersistUpload(owner)) return ''
  const envelope: UploadEnvelope = {
    v: UPLOAD_ENVELOPE_VERSION,
    uploadId,
    owner,
    fingerprint: String(input.fingerprint || ''),
    savedAt: input.savedAt || new Date().toISOString(),
  }
  return JSON.stringify(envelope)
}

export type UploadEnvelopeVerdict =
  | { usable: true; uploadId: string }
  | { usable: false; reason: 'empty' | 'legacy' | 'malformed' | 'owner-mismatch' | 'fingerprint-mismatch' }

/**
 * 读取判定：返回可用的 upload_id，或明确的不可用原因。
 *
 * `legacy`（旧版裸字符串）**不可用**：它没有任何归属信息，无法证明属于谁。
 * 这正是 H72 的核心 —— "读得到"不等于"这是你的"。
 */
export function decodeUploadEnvelope(
  raw: string | null | undefined,
  current: UploadOwner,
  expectedFingerprint?: string,
): UploadEnvelopeVerdict {
  const text = String(raw ?? '').trim()
  if (!text) return { usable: false, reason: 'empty' }

  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    // 旧版存的是裸 upload_id：没有任何归属元数据 ⇒ 不可信。
    return { usable: false, reason: 'legacy' }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { usable: false, reason: 'malformed' }
  }
  const env = parsed as Partial<UploadEnvelope>
  if (env.v !== UPLOAD_ENVELOPE_VERSION) return { usable: false, reason: 'malformed' }
  const uploadId = typeof env.uploadId === 'string' ? env.uploadId : ''
  if (!uploadId) return { usable: false, reason: 'malformed' }

  const storedOwner = normalizeUploadOwner(env.owner)
  if (!sameUploadOwner(storedOwner, normalizeUploadOwner(current))) {
    return { usable: false, reason: 'owner-mismatch' }
  }
  if (expectedFingerprint !== undefined && String(env.fingerprint || '') !== expectedFingerprint) {
    return { usable: false, reason: 'fingerprint-mismatch' }
  }
  return { usable: true, uploadId }
}

/** 键名：仍用 `chunk-upload:` 前缀（登出时按前缀整体清理），身份放进值里做校验。 */
export function uploadStorageKey(input: {
  purpose: string
  parentId?: string
  fingerprint: string
}): string {
  const parent = String(input.parentId || '')
  return `chunk-upload:${input.purpose}:${parent}:${input.fingerprint}`
}
