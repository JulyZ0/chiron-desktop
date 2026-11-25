/**
 * H67：私密媒体必须走签名 URL，缓存按账号隔离（PRD 9.10）。
 *
 * 缺陷形态：
 *   1. `signedMediaCache` 是 `Map<assetId, {url,exp}>` —— **只按资产 id**。
 *      换账号/换租户/换部署后仍旧命中上一个身份的签名 URL（签名里带着那个身份的
 *      授权主体），登出也不清：一个已经无权访问的资产"看起来还能看"。
 *   2. `resolveMediaUrl` 对非 `/media/` 前缀**直接返回** `file_url`。
 *      于是裸 S3 对象 URL 被交给 renderer —— 私桶里加载失败，公开桶里绕过授权语义
 *      （对象地址一泄漏就是永久可读）。
 *
 * 现在：作用域 = user+tenant+mode+origin，作用域一变整表作废；裸对象 URL 不可直接
 * 渲染，必须换服务端 owner-bound 签名，换不到就给空串（界面显示"不可用"）。
 */
import { describe, it, expect } from 'vitest'
import {
  MEDIA_CACHE_EXPIRY_MARGIN_MS,
  MediaUrlCache,
  canRenderFileUrlDirectly,
  mediaScopeKey,
  normalizeMediaScope,
} from '../mediaUrlCache'

const scopeA = { userId: 'u-a', tenantId: '1', mode: 'cloud', origin: 'https://app.example.com' }
const scopeB = { ...scopeA, userId: 'u-b' }

describe('MediaUrlCache（H67）', () => {
  it('同作用域内按 asset id 命中', () => {
    const cache = new MediaUrlCache()
    cache.setScope(scopeA)
    cache.set('asset-1', '/media/s/asset-1?exp=' + Math.floor((Date.now() + 600_000) / 1000), Date.now() + 600_000)
    expect(cache.get('asset-1')?.url).toContain('asset-1')
  })

  it('换账号后旧条目立即不可用（不留上一个身份的签名）', () => {
    const cache = new MediaUrlCache()
    cache.setScope(scopeA)
    cache.set('asset-1', 'url-a', Date.now() + 600_000)
    expect(cache.get('asset-1')).not.toBeNull()

    cache.setScope(scopeB)
    expect(cache.get('asset-1'), '换账号后必须不命中').toBeNull()
    expect(cache.size, '换作用域应整表清空').toBe(0)
  })

  it('换租户 / 换部署 / 换运行模式同样作废', () => {
    for (const patch of [{ tenantId: '2' }, { origin: 'https://b.example.com' }, { mode: 'local' }]) {
      const cache = new MediaUrlCache()
      cache.setScope(scopeA)
      cache.set('a', 'u', Date.now() + 600_000)
      cache.setScope({ ...scopeA, ...patch })
      expect(cache.get('a'), JSON.stringify(patch)).toBeNull()
    }
  })

  it('同一作用域重复设置不会清缓存（避免每次解析都白丢）', () => {
    const cache = new MediaUrlCache()
    cache.setScope(scopeA)
    cache.set('a', 'u', Date.now() + 600_000)
    cache.setScope({ ...scopeA })
    expect(cache.get('a')).not.toBeNull()
  })

  it('过期（含提前量）即失效，prune 清掉', () => {
    const cache = new MediaUrlCache()
    cache.setScope(scopeA)
    const now = Date.now()
    cache.set('soon', 'u', now + MEDIA_CACHE_EXPIRY_MARGIN_MS - 1)
    expect(cache.get('soon', now)).toBeNull()

    cache.set('later', 'u', now + 600_000)
    expect(cache.get('later', now)).not.toBeNull()
    cache.prune(now + 700_000)
    expect(cache.size).toBe(0)
  })

  it('invalidate 只影响指定资产', () => {
    const cache = new MediaUrlCache()
    cache.setScope(scopeA)
    cache.set('a', 'ua', Date.now() + 600_000)
    cache.set('b', 'ub', Date.now() + 600_000)
    cache.invalidate('a')
    expect(cache.get('a')).toBeNull()
    expect(cache.get('b')).not.toBeNull()
  })

  it('作用域键覆盖全字段（缺字段也被区分）', () => {
    expect(mediaScopeKey(scopeA)).not.toBe(mediaScopeKey({ ...scopeA, tenantId: '2' }))
    expect(mediaScopeKey({})).toBe(normalizeMediaScope({}).origin + '|||')
  })
})

describe('canRenderFileUrlDirectly（H67：裸对象 URL 不得直接渲染）', () => {
  const opts = { selfOrigin: 'https://app.example.com', trustedOrigins: ['https://cdn.example.com'] }

  it('站内路径与受信任 origin 可直接渲染', () => {
    expect(canRenderFileUrlDirectly('/media/s/a.png', opts)).toBe(true)
    expect(canRenderFileUrlDirectly('/v1/media/a/download', opts)).toBe(true)
    expect(canRenderFileUrlDirectly('https://app.example.com/a.png', opts)).toBe(true)
    expect(canRenderFileUrlDirectly('https://cdn.example.com/a.png', opts)).toBe(true)
  })

  it('裸 S3 对象 URL 不可直接渲染（这正是本条的核心）', () => {
    expect(canRenderFileUrlDirectly('https://my-bucket.s3.amazonaws.com/asset-1.png', opts)).toBe(false)
    expect(canRenderFileUrlDirectly('https://s3.us-east-1.amazonaws.com/bucket/a', opts)).toBe(false)
    expect(canRenderFileUrlDirectly('https://storage.googleapis.com/bucket/a', opts)).toBe(false)
  })

  it('危险 scheme 与协议相对地址一律拒绝', () => {
    for (const raw of ['javascript:alert(1)', 'file:///etc/passwd', '//evil.example/a', 'ftp://a/b']) {
      expect(canRenderFileUrlDirectly(raw, opts), raw).toBe(false)
    }
  })

  it('blob: 与图片 data: 允许；其它 data: 拒绝', () => {
    expect(canRenderFileUrlDirectly('blob:https://app.example.com/1', opts)).toBe(true)
    expect(canRenderFileUrlDirectly('data:image/png;base64,AA', opts)).toBe(true)
    expect(canRenderFileUrlDirectly('data:text/html,<script>', opts)).toBe(false)
  })

  it('不在受信任集合里的站内其它路径也不放过', () => {
    expect(canRenderFileUrlDirectly('/admin/users', opts)).toBe(false)
    expect(canRenderFileUrlDirectly('', opts)).toBe(false)
  })
})
