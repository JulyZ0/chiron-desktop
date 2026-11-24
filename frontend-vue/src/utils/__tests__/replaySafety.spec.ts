import { describe, expect, it } from 'vitest'
import {
  IDEMPOTENCY_KEY_HEADER,
  isIdempotentMethod,
  isUploadPayload,
  readHeader,
  replayVerdict,
} from '../rateLimitBackoff'

/**
 * H66（PRD 9.10）**非幂等请求不得全局盲重试**。
 *
 * 判据从「状态码」换成「请求本身能不能重放」：
 * GET/HEAD/OPTIONS 可证明幂等；其它方法必须带 `Idempotency-Key`；
 * 上传与显式 `__noRetry` 一律否决。这里是纯函数的边界，接线见
 * `src/api/__tests__/nonIdempotentRetry.spec.ts`。
 */
describe('重放安全性（H66）', () => {
  it('只把 GET / HEAD / OPTIONS 视为可证明幂等的方法', () => {
    expect(isIdempotentMethod('get')).toBe(true)
    expect(isIdempotentMethod('GET')).toBe(true)
    expect(isIdempotentMethod('head')).toBe(true)
    expect(isIdempotentMethod('options')).toBe(true)
    // axios 会把未写 method 的请求当成 GET
    expect(isIdempotentMethod(undefined)).toBe(true)
    expect(isIdempotentMethod(null)).toBe(true)

    for (const m of ['post', 'POST', 'patch', 'put', 'delete', 'DELETE']) {
      expect(isIdempotentMethod(m)).toBe(false)
    }
  })

  it('multipart / FormData 判定为上传（请求体不可重放）', () => {
    expect(isUploadPayload(undefined, 'multipart/form-data; boundary=x')).toBe(true)
    expect(isUploadPayload(undefined, 'Multipart/Form-Data')).toBe(true)
    expect(isUploadPayload(new FormData(), undefined)).toBe(true)
    expect(isUploadPayload({ a: 1 }, 'application/json')).toBe(false)
    expect(isUploadPayload(undefined, undefined)).toBe(false)
  })

  it('读请求直接放行：行为与旧版一致', () => {
    expect(replayVerdict({ method: 'get' })).toEqual({ safe: true, reason: 'idempotent_method' })
  })

  it('写请求默认**不**放行（这就是 H66 的核心）', () => {
    expect(replayVerdict({ method: 'post' })).toEqual({ safe: false, reason: 'unsafe_method' })
    expect(replayVerdict({ method: 'patch' })).toEqual({ safe: false, reason: 'unsafe_method' })
    expect(replayVerdict({ method: 'delete' })).toEqual({ safe: false, reason: 'unsafe_method' })
  })

  it('带幂等键的写请求才放行（服务端按键去重）', () => {
    expect(replayVerdict({ method: 'post', idempotencyKey: 'k-1' })).toEqual({
      safe: true,
      reason: 'idempotency_key',
    })
    // 空白键等于没给
    expect(replayVerdict({ method: 'post', idempotencyKey: '   ' })).toEqual({
      safe: false,
      reason: 'unsafe_method',
    })
    expect(replayVerdict({ method: 'post', idempotencyKey: '' })).toEqual({
      safe: false,
      reason: 'unsafe_method',
    })
  })

  it('上传即使带幂等键也不重放；显式 __noRetry 优先于一切', () => {
    expect(replayVerdict({ method: 'post', isUpload: true, idempotencyKey: 'k-1' })).toEqual({
      safe: false,
      reason: 'upload_not_replayable',
    })
    expect(replayVerdict({ method: 'get', retryDisabled: true })).toEqual({
      safe: false,
      reason: 'retry_disabled',
    })
    expect(
      replayVerdict({ method: 'post', idempotencyKey: 'k-1', isUpload: true, retryDisabled: true }),
    ).toEqual({ safe: false, reason: 'retry_disabled' })
  })

  it('请求头读取不区分大小写，且兼容 AxiosHeaders 的 get()', () => {
    const plain = { 'Idempotency-Key': 'k-plain' }
    expect(readHeader(plain, IDEMPOTENCY_KEY_HEADER)).toBe('k-plain')
    expect(readHeader({ 'idempotency-key': 'k-lower' }, IDEMPOTENCY_KEY_HEADER)).toBe('k-lower')

    const axiosHeaders = {
      get: (name: string) => (name.toLowerCase() === 'idempotency-key' ? 'k-axios' : undefined),
    }
    expect(readHeader(axiosHeaders, IDEMPOTENCY_KEY_HEADER)).toBe('k-axios')

    expect(readHeader(undefined, IDEMPOTENCY_KEY_HEADER)).toBe('')
    expect(readHeader({}, IDEMPOTENCY_KEY_HEADER)).toBe('')
    expect(readHeader({ 'Idempotency-Key': '  ' }, IDEMPOTENCY_KEY_HEADER)).toBe('')
  })
})
