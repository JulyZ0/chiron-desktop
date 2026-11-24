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

describe('uploadFile', () => {
  it('reads the asset from the gateway success/data envelope', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: {
        success: true,
        data: { id: 'asset-1', name: 'server.txt', file_url: '/media/s/asset-1', size: '5' },
      },
    } as never)

    await expect(uploadFile(smallFile())).resolves.toEqual({
      id: 'asset-1',
      url: '/media/s/asset-1',
      name: 'server.txt',
      size: 5,
      mimeType: 'text/plain',
    })
  })

  it.each([
    { success: true },
    { success: true, data: { id: 'asset-1' } },
    { success: true, data: { file_url: '/media/s/asset-1' } },
    { success: false, data: { id: 'fake', file_url: '/media/s/fake' } },
  ])('rejects malformed or unsuccessful responses instead of fabricating an attachment: %j', async body => {
    vi.spyOn(api, 'post').mockResolvedValue({ data: body } as never)
    await expect(uploadFile(smallFile())).rejects.toThrow('invalid media upload response')
  })

  it('returns the completed chunk upload asset', async () => {
    const file = smallFile()
    Object.defineProperty(file, 'size', { value: 50 * 1024 * 1024 + 1 })
    uploaderMocks.createChunkUpload.mockResolvedValue({
      done: Promise.resolve({
        asset_id: 'asset-2', upload_id: 'upload-2', file_url: '/media/s/asset-2', size: file.size,
      }),
    })

    await expect(uploadFile(file)).resolves.toEqual({
      id: 'asset-2',
      url: '/media/s/asset-2',
      name: 'a.txt',
      size: file.size,
      mimeType: 'text/plain',
    })
  })

  it.each([
    undefined,
    { upload_id: 'upload-1', file_url: '/media/s/upload-1', size: 1 },
    { asset_id: 'asset-1', file_url: '', size: 1 },
  ])('requires a real media asset id and URL from chunked uploads too: %j', async done => {
    const file = smallFile()
    Object.defineProperty(file, 'size', { value: 50 * 1024 * 1024 + 1 })
    uploaderMocks.createChunkUpload.mockResolvedValue({ done: Promise.resolve(done) })

    await expect(uploadFile(file)).rejects.toThrow('invalid chunk upload response')
  })
})
