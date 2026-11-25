/**
 * H70：媒体签名 URL 的失败分类与重签策略（PRD 9.10）。
 *
 * 缺陷形态（旧 `MediaView.vue`）：
 *   - `failedSignedUrls` 是**永久黑名单**：同一 URL 失败一次后本会话内永不重试。
 *     切网络、休眠唤醒、服务端一次 502 都足以让那张图永远不显示，用户只能刷新页面；
 *   - 404（真的没了）与 403（过期）走同一分支：前者被反复重签白费请求，
 *     后者在次数用尽后掉进同一个黑洞；
 *   - 界面上没有任何"为什么"，只剩一块空白。
 *
 * 现在按 HTTP 语义分类，再交给策略函数决定：`gone` 永久不可用且不再请求、
 * `expired`/`unauthorized` 有上限重签、`transient` 退避重试（永不永久失败）。
 */
import { describe, it, expect } from 'vitest'
import {
  MEDIA_RESIGN_LIMIT,
  MEDIA_TRANSIENT_BACKOFF_MAX_MS,
  MEDIA_TRANSIENT_BACKOFF_MS,
  classifyMediaFailure,
  mediaUnavailableKey,
  mediaUrlExpiry,
  nextMediaAction,
  recordMediaFailure,
  type MediaRetryState,
} from '../mediaUrlRetry'

describe('classifyMediaFailure (H70)', () => {
  it('404/410 = 资源真的没了（不要重签）', () => {
    expect(classifyMediaFailure({ status: 404 })).toBe('gone')
    expect(classifyMediaFailure({ status: 410 })).toBe('gone')
  })

  it('401/403 = 鉴权；带 expired 说明时是过期', () => {
    expect(classifyMediaFailure({ status: 401 })).toBe('unauthorized')
    expect(classifyMediaFailure({ status: 403 })).toBe('unauthorized')
    expect(classifyMediaFailure({ status: 403, body: 'link expired' })).toBe('expired')
  })

  it('网络错误/超时/5xx/429 = 瞬时失败', () => {
    expect(classifyMediaFailure({ status: 0 })).toBe('transient')
    expect(classifyMediaFailure({ status: 408 })).toBe('transient')
    expect(classifyMediaFailure({ status: 429 })).toBe('transient')
    expect(classifyMediaFailure({ status: 502 })).toBe('transient')
    expect(classifyMediaFailure({ status: 503 })).toBe('transient')
  })

  it('URL 自带的 exp 已过 = 过期（无需先发一次探测请求）', () => {
    const now = 1_700_000_000_000
    const expired = `http://x/media/s/a.png?exp=${Math.floor(now / 1000) - 10}&sig=abc`
    expect(classifyMediaFailure({ status: 0, url: expired, now })).toBe('expired')
  })

  it('exp 未过时仍按状态码分类（不能把"还有效"误判成过期）', () => {
    const now = 1_700_000_000_000
    const fresh = `http://x/media/s/a.png?exp=${Math.floor(now / 1000) + 600}`
    expect(classifyMediaFailure({ status: 502, url: fresh, now })).toBe('transient')
    expect(classifyMediaFailure({ status: 404, url: fresh, now })).toBe('gone')
  })
})

describe('mediaUrlExpiry', () => {
  it('秒与毫秒两种写法都能识别', () => {
    expect(mediaUrlExpiry('http://a/x?exp=1700000000')).toBe(1_700_000_000_000)
    expect(mediaUrlExpiry('http://a/x?exp=1700000000000')).toBe(1_700_000_000_000)
    expect(mediaUrlExpiry('http://a/x?expires=1700000000')).toBe(1_700_000_000_000)
  })

  it('缺失/非法返回 null', () => {
    expect(mediaUrlExpiry('http://a/x')).toBeNull()
    expect(mediaUrlExpiry('http://a/x?exp=abc')).toBeNull()
    expect(mediaUrlExpiry('')).toBeNull()
    expect(mediaUrlExpiry('http://a/x?exp=-5')).toBeNull()
  })
})

describe('nextMediaAction', () => {
  const fresh: MediaRetryState | undefined = undefined

  it('gone 直接判不可用（不重签、不等待）', () => {
    expect(nextMediaAction(fresh, 'gone')).toEqual({ action: 'unavailable', reason: 'gone' })
  })

  it('expired 立刻重签（这是最确定的"该换一张"信号）', () => {
    expect(nextMediaAction(fresh, 'expired')).toEqual({ action: 'resign', reason: 'expired' })
  })

  it('重签次数用尽后转不可用（防打爆服务端）', () => {
    const used: MediaRetryState = { attempts: MEDIA_RESIGN_LIMIT, nextAt: 0, lastKind: 'expired' }
    expect(nextMediaAction(used, 'expired')).toEqual({ action: 'unavailable', reason: 'expired' })
    expect(nextMediaAction(used, 'unauthorized')).toEqual({ action: 'unavailable', reason: 'unauthorized' })
  })

  it('transient 走退避，**永不**转不可用', () => {
    let state: MediaRetryState | undefined
    const now = 1_700_000_000_000
    const seen: number[] = []
    for (let i = 0; i < 10; i += 1) {
      const decision = nextMediaAction(state, 'transient', now)
      expect(decision.action).toBe('wait')
      if (decision.action === 'wait') seen.push(decision.until - now)
      state = recordMediaFailure(state, 'transient', now)
    }
    // 指数退避且封顶
    expect(seen[0]).toBe(MEDIA_TRANSIENT_BACKOFF_MS)
    expect(seen[1]).toBe(MEDIA_TRANSIENT_BACKOFF_MS * 2)
    expect(Math.max(...seen)).toBeLessThanOrEqual(MEDIA_TRANSIENT_BACKOFF_MAX_MS * 2)
  })
})

describe('recordMediaFailure', () => {
  it('累计次数并记录最近分类（不修改入参）', () => {
    const first: MediaRetryState = { attempts: 0, nextAt: 0, lastKind: 'unknown' }
    const next = recordMediaFailure(first, 'unauthorized', 1000)
    expect(first.attempts).toBe(0) // 纯函数
    expect(next.attempts).toBe(1)
    expect(next.lastKind).toBe('unauthorized')
    expect(next.nextAt).toBe(0) // resign 不需要等待
  })

  it('transient 记录下次可重试时间', () => {
    const now = 5_000
    const next = recordMediaFailure(undefined, 'transient', now)
    expect(next.nextAt).toBe(now + MEDIA_TRANSIENT_BACKOFF_MS)
  })
})

describe('mediaUnavailableKey', () => {
  it('每种失败都给 i18n key（不返回中文字面量，否则过不了 i18n 契约）', () => {
    expect(mediaUnavailableKey('gone')).toBe('media.unavailable_gone')
    expect(mediaUnavailableKey('unauthorized')).toBe('media.unavailable_unauthorized')
    expect(mediaUnavailableKey('expired')).toBe('media.unavailable_expired')
    expect(mediaUnavailableKey('transient')).toBe('media.unavailable_transient')
    expect(mediaUnavailableKey('unknown')).toBe('media.unavailable_transient')
    expect(mediaUnavailableKey(undefined)).toBe('')
  })
})
