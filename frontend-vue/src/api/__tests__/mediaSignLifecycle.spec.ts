import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, invalidateMediaUrl, isMediaUrlExpired, resolveMediaUrl } from '../index'

const asset = { id: 'asset-expiry', file_url: '/media/asset-expiry.png' }
const signedUrl = (exp: number) => `/media/s/asset-expiry.png?exp=${exp}&sig=test`

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-24T12:00:00Z'))
  invalidateMediaUrl(asset.id)
})

afterEach(() => {
  invalidateMediaUrl(asset.id)
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('signed media URL lifecycle', () => {
  it('re-signs before the server expiry instead of serving a soon-dead cached URL', async () => {
    const now = Math.floor(Date.now() / 1000)
    const post = vi.spyOn(api, 'post')
      .mockResolvedValueOnce({ data: { data: { url: signedUrl(now + 20) } } } as never)
      .mockResolvedValueOnce({ data: { data: { url: signedUrl(now + 720) } } } as never)

    await expect(resolveMediaUrl(asset)).resolves.toBe(signedUrl(now + 20))
    await expect(resolveMediaUrl(asset)).resolves.toBe(signedUrl(now + 720))
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('does not request the removed public path when signing fails', async () => {
    vi.spyOn(api, 'post').mockRejectedValue(new Error('forbidden'))
    await expect(resolveMediaUrl(asset)).resolves.toBe(asset.file_url)

    await expect(resolveMediaUrl(asset, true)).rejects.toThrow('forbidden')
    await expect(resolveMediaUrl(asset, true)).rejects.toThrow('forbidden')
  })

  it('recognizes expired URLs and leaves URLs without expiry metadata alone', () => {
    expect(isMediaUrlExpired(signedUrl(Math.floor(Date.now() / 1000) - 1))).toBe(true)
    expect(isMediaUrlExpired(signedUrl(Math.floor(Date.now() / 1000) + 600))).toBe(false)
    expect(isMediaUrlExpired('/media/s/asset.png')).toBe(false)
  })
})
