/**
 * H65：上传响应的 URL 契约（PRD 9.10）。
 *
 * 已修的部分（早前几轮）：严格 `{success,data}` 信封、缺 ID/file_url 立即失败、
 * 不再伪造 `Date.now()` ID。
 *
 * 本轮补的部分：**返回的 `file_url` 也要过来源/协议校验**。这个字段随后会被当成
 * `<img src>` / `<a href>`；只校验信封形状而不看 URL，等于"服务端被误配时前端替它兜底"。
 * 另外分片路径与单请求路径必须**同一套契约**（它们由不同 handler 产出）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

const uploaderMocks = vi.hoisted(() => ({ createChunkUpload: vi.fn() }))
vi.mock('../../utils/uploader', () => uploaderMocks)

import { api, uploadFile } from '../index'

function smallFile(): File {
  return new File(['hello'], 'a.txt', { type: 'text/plain', lastModified: 1 })
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('uploadFile 的 URL 契约 (H65)', () => {
  it.each([
    ['javascript:alert(1)'],
    ['file:///C:/Windows/win.ini'],
    ['//evil.example/x.png'],
    ['https://evil.example/x.png'],
    ['data:text/html,<script>alert(1)</script>'],
  ])('不可信的 file_url 必须让上传失败而不是返回给界面：%s', async url => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { success: true, data: { id: 'asset-1', file_url: url, size: 5 } },
    } as never)
    await expect(uploadFile(smallFile())).rejects.toThrow('invalid media upload response')
  })

  it('站内 /media/ 路径照常通过（包括签名 URL）', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { success: true, data: { id: 'asset-1', file_url: '/media/s/asset-1?exp=1&sig=abc', size: 5 } },
    } as never)
    const result = await uploadFile(smallFile())
    expect(result.url).toBe('/media/s/asset-1?exp=1&sig=abc')
    expect(result.id).toBe('asset-1')
  })

  it('分片路径同样校验 URL（两条路不能各自为政）', async () => {
    const big = new File([new Uint8Array(8)], 'big.bin', { type: 'application/octet-stream', lastModified: 1 })
    Object.defineProperty(big, 'size', { value: 51 * 1024 * 1024 })
    uploaderMocks.createChunkUpload.mockResolvedValue({
      uploadId: 'u-1',
      done: Promise.resolve({ asset_id: 'asset-2', file_url: 'javascript:alert(1)', upload_id: 'u-1', size: 51 }),
    } as never)
    await expect(uploadFile(big)).rejects.toThrow('invalid media upload response')
  })

  it('分片路径缺 asset_id 也失败（不伪造 ID）', async () => {
    const big = new File([new Uint8Array(8)], 'big.bin', { type: 'application/octet-stream', lastModified: 1 })
    Object.defineProperty(big, 'size', { value: 51 * 1024 * 1024 })
    uploaderMocks.createChunkUpload.mockResolvedValue({
      uploadId: 'u-1',
      done: Promise.resolve({ file_url: '/media/s/x', upload_id: 'u-1', size: 51 }),
    } as never)
    await expect(uploadFile(big)).rejects.toThrow('invalid chunk upload response')
  })
})
