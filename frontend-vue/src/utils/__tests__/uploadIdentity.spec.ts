/**
 * H72：上传续传元数据必须按身份隔离（PRD 9.10）。
 *
 * 缺陷形态：`chunk-upload:*` 的值是**裸 `upload_id`**，键里只有一个可能为空的用户 id。
 * `localStorage` 是浏览器级的，于是：
 *   - 换账号/换租户后，同一个键还能读到上一个身份的 `upload_id`，续传会接着**别人的**
 *     分片会话写（在服务端看是完全合法的请求）；
 *   - 同一账号在不同部署（本地模式 ↔ 团队云、不同 origin）之间也互相污染：
 *     `upload_id` 跨实例毫无意义；
 *   - 旧版裸值没有任何归属信息，无法判断"这是谁的"。
 *
 * 治法：值改成 `{v, uploadId, owner, fingerprint}`，读取时逐项比对当前身份；
 * 任何不一致都丢弃（宁可重传一次，不可串号）。
 */
import { describe, it, expect } from 'vitest'
import {
  UPLOAD_ENVELOPE_VERSION,
  canPersistUpload,
  decodeUploadEnvelope,
  encodeUploadEnvelope,
  normalizeUploadOwner,
  sameUploadOwner,
  uploadStorageKey,
  type UploadOwner,
} from '../uploadIdentity'

const baseOwner: UploadOwner = {
  userId: 'u-alice',
  tenantId: '1',
  mode: 'cloud',
  origin: 'https://app.example.com',
}

describe('sameUploadOwner (H72)', () => {
  it('四项全等才算同一身份', () => {
    expect(sameUploadOwner(baseOwner, { ...baseOwner })).toBe(true)
    for (const patch of [
      { userId: 'u-bob' },
      { tenantId: '2' },
      { mode: 'local' },
      { origin: 'https://other.example.com' },
    ]) {
      expect(sameUploadOwner(baseOwner, { ...baseOwner, ...patch })).toBe(false)
    }
  })

  it('缺字段（空串）不等于"任意值"', () => {
    expect(sameUploadOwner(normalizeUploadOwner({}), baseOwner)).toBe(false)
  })
})

describe('canPersistUpload', () => {
  it('没有账号或没有 origin 时不允许持久化续传状态', () => {
    expect(canPersistUpload({ ...baseOwner, userId: '' })).toBe(false)
    expect(canPersistUpload({ ...baseOwner, origin: '' })).toBe(false)
    expect(canPersistUpload(baseOwner)).toBe(true)
  })
})

describe('encode/decode 信封 (H72)', () => {
  const finger = 'a.png:1024:1700000000000:2097152'

  it('同身份同指纹可复用', () => {
    const raw = encodeUploadEnvelope({ uploadId: 'up-1', owner: baseOwner, fingerprint: finger })
    expect(JSON.parse(raw).v).toBe(UPLOAD_ENVELOPE_VERSION)
    const verdict = decodeUploadEnvelope(raw, baseOwner, finger)
    expect(verdict).toEqual({ usable: true, uploadId: 'up-1' })
  })

  it('换账号 → 不可用（owner-mismatch）', () => {
    const raw = encodeUploadEnvelope({ uploadId: 'up-1', owner: baseOwner, fingerprint: finger })
    const verdict = decodeUploadEnvelope(raw, { ...baseOwner, userId: 'u-bob' }, finger)
    expect(verdict).toEqual({ usable: false, reason: 'owner-mismatch' })
  })

  it('换部署 origin / 换运行模式 → 不可用', () => {
    const raw = encodeUploadEnvelope({ uploadId: 'up-1', owner: baseOwner, fingerprint: finger })
    expect(decodeUploadEnvelope(raw, { ...baseOwner, origin: 'https://b.example.com' }, finger)).toEqual({
      usable: false,
      reason: 'owner-mismatch',
    })
    expect(decodeUploadEnvelope(raw, { ...baseOwner, mode: 'local' }, finger)).toEqual({
      usable: false,
      reason: 'owner-mismatch',
    })
  })

  it('换租户 → 不可用', () => {
    const raw = encodeUploadEnvelope({ uploadId: 'up-1', owner: baseOwner, fingerprint: finger })
    expect(decodeUploadEnvelope(raw, { ...baseOwner, tenantId: '2' }, finger).usable).toBe(false)
  })

  it('文件指纹不符 → 不可用（同名同大小改了内容也算不同文件）', () => {
    const raw = encodeUploadEnvelope({ uploadId: 'up-1', owner: baseOwner, fingerprint: finger })
    expect(decodeUploadEnvelope(raw, baseOwner, 'b.png:1024:1700000000000:2097152')).toEqual({
      usable: false,
      reason: 'fingerprint-mismatch',
    })
  })

  it('旧版裸 upload_id 一律不可用（无归属信息）', () => {
    expect(decodeUploadEnvelope('up-legacy-123', baseOwner, finger)).toEqual({
      usable: false,
      reason: 'legacy',
    })
  })

  it('空值 / 结构损坏 / 版本不符分别给明确原因', () => {
    expect(decodeUploadEnvelope('', baseOwner, finger)).toEqual({ usable: false, reason: 'empty' })
    expect(decodeUploadEnvelope('null', baseOwner, finger)).toEqual({ usable: false, reason: 'malformed' })
    expect(decodeUploadEnvelope('{"v":1,"uploadId":"x"}', baseOwner, finger)).toEqual({
      usable: false,
      reason: 'malformed',
    })
  })

  it('没有身份时不写盘（返回空串）', () => {
    expect(encodeUploadEnvelope({ uploadId: 'up-1', owner: { ...baseOwner, userId: '' }, fingerprint: finger })).toBe('')
    expect(encodeUploadEnvelope({ uploadId: '', owner: baseOwner, fingerprint: finger })).toBe('')
  })
})

describe('uploadStorageKey', () => {
  it('键仍带 chunk-upload 前缀（登出时按前缀整体清理）', () => {
    const key = uploadStorageKey({ purpose: 'media', parentId: 'f-1', fingerprint: 'x' })
    expect(key.startsWith('chunk-upload:')).toBe(true)
    expect(key).toContain('media')
    expect(key).toContain('f-1')
  })

  it('身份不进键名（避免登出清理漏键），而是进值做校验', () => {
    const key = uploadStorageKey({ purpose: 'media', fingerprint: 'x' })
    expect(key).not.toContain('u-alice')
  })
})
