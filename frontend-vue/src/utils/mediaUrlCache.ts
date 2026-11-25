/**
 * 媒体签名 URL 缓存（PRD 9.10 / H67）。
 *
 * 缺陷形态：`signedMediaCache` 是**只按 asset id** 做的全局 Map。
 *   - 换账号/换租户/换部署后，缓存里仍然是上一个身份的签名 URL（签名里带着那个身份的
 *     授权主体），界面继续用它渲染 —— 于是一个已经无权访问的资产"看起来还能看"，
 *     直到 URL 过期才报错；
 *   - 登出不清缓存，同一个浏览器里换人登录会复用别人的签名；
 *   - 私桶场景下 `resolveMediaUrl` 对非 `/media/` 前缀**直接返回裸对象 URL**，
 *     等于把对象存储地址交给 renderer（公开桶绕过授权语义，私桶则直接加载失败）。
 *
 * 这里把"缓存作用域"显式化：作用域 = user + tenant + mode + origin。
 * 作用域一变（登录/登出/切账号/切部署），整个缓存立即作废。
 */

export interface MediaCacheScope {
  userId: string
  tenantId: string
  mode: string
  origin: string
}

export function normalizeMediaScope(raw: Partial<MediaCacheScope> | undefined | null): MediaCacheScope {
  return {
    userId: String(raw?.userId ?? ''),
    tenantId: String(raw?.tenantId ?? ''),
    mode: String(raw?.mode ?? ''),
    origin: String(raw?.origin ?? ''),
  }
}

export function mediaScopeKey(raw: Partial<MediaCacheScope> | undefined | null): string {
  const s = normalizeMediaScope(raw)
  // 顺序固定，便于测试与日志里肉眼比对。
  return `${s.origin}|${s.mode}|${s.tenantId}|${s.userId}`
}

export interface MediaCacheEntry {
  url: string
  /** 过期时间戳（毫秒）。 */
  exp: number
}

/** 提前量：避免"刚好在过期边界上用出去"。 */
export const MEDIA_CACHE_EXPIRY_MARGIN_MS = 30_000

export class MediaUrlCache {
  private entries = new Map<string, MediaCacheEntry>()
  private scope = ''

  /** 切换作用域；若与当前不同则**清空**缓存（换身份后旧 URL 一律不可复用）。 */
  setScope(raw: Partial<MediaCacheScope> | undefined | null): void {
    const next = mediaScopeKey(raw)
    if (next !== this.scope) {
      this.entries.clear()
      this.scope = next
    }
  }

  get currentScope(): string {
    return this.scope
  }

  private key(id: string): string {
    return `${this.scope}::${id}`
  }

  get(id: string, now = Date.now()): MediaCacheEntry | null {
    const hit = this.entries.get(this.key(id))
    if (!hit) return null
    if (hit.exp - MEDIA_CACHE_EXPIRY_MARGIN_MS <= now) {
      this.entries.delete(this.key(id))
      return null
    }
    return hit
  }

  set(id: string, url: string, exp: number): void {
    this.entries.set(this.key(id), { url, exp })
  }

  invalidate(id: string): void {
    this.entries.delete(this.key(id))
  }

  clear(): void {
    this.entries.clear()
  }

  /** 清掉过期项（`resolveMediaUrl` 每次调用前顺手做）。 */
  prune(now = Date.now()): void {
    for (const [key, value] of this.entries) {
      if (value.exp - MEDIA_CACHE_EXPIRY_MARGIN_MS <= now) this.entries.delete(key)
    }
  }

  get size(): number {
    return this.entries.size
  }
}

/**
 * 判断一个 `file_url` 是否可以**直接**交给 renderer（H67）。
 *
 * 只有三类可以：
 *   1. 站内相对路径（`/media/…`、`/v1/…`）；
 *   2. 应用自己的 origin / 显式配置的对象存储 origin（同为受信任）；
 *   3. `blob:` 与图片类 `data:`（应用自己造的对象 URL）。
 *
 * 其余（含**裸 S3 对象 URL**）一律不可直接使用 —— 私桶里它根本加载不出来，
 * 公开桶里它绕过授权语义（对象地址一泄漏就是永久可读）。
 */
export function canRenderFileUrlDirectly(
  raw: string,
  options: { selfOrigin?: string; trustedOrigins?: string[] } = {},
): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  if (text.startsWith('//')) return false
  if (text.startsWith('/')) return text.startsWith('/media/') || text.startsWith('/v1/')
  const lower = text.toLowerCase()
  if (lower.startsWith('blob:')) return true
  if (lower.startsWith('data:')) {
    return ['data:image/png', 'data:image/jpeg', 'data:image/webp', 'data:image/gif'].some(p =>
      lower.startsWith(p),
    )
  }
  let url: URL
  try {
    url = new URL(text)
  } catch {
    return false
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false
  const trusted = new Set<string>()
  const self = options.selfOrigin ? normalizeOrigin(options.selfOrigin) : ''
  if (self) trusted.add(self)
  for (const origin of options.trustedOrigins ?? []) {
    const normalized = normalizeOrigin(origin)
    if (normalized) trusted.add(normalized)
  }
  return trusted.has(url.origin)
}

function normalizeOrigin(value: string): string {
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
    return url.origin
  } catch {
    return ''
  }
}
