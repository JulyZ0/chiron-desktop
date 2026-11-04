/**
 * 通用分片上传 SDK（断点续传）
 *
 * 协议（后端 /v1/uploads）：
 *   POST /v1/uploads                    → { upload_id, chunk_size, chunk_count }
 *   PUT  /v1/uploads/{id}/chunks/{i}    → 上传一个分片（body 为原始字节，幂等）
 *   GET  /v1/uploads/{id}               → { received_chunks }（断点续传依据）
 *   POST /v1/uploads/{id}/complete      → 合并并按 purpose 落库 → { file_url }
 *
 * 特性：2MB 分片、并发 3、逐片进度、失败重试（指数退避）、暂停/恢复、中断续传
 * （localStorage 记录 upload_id，重进页面查询已收分片后续传）。
 */
import { api } from '../api'

export type UploadPurpose = 'media' | 'kb_doc' | 'generic'

export interface ChunkUploadOptions {
  purpose: UploadPurpose
  /** media: 文件夹 id；kb_doc: kb_id */
  parentId?: string
  category?: string
  chunkSize?: number
  concurrency?: number
}

export interface ChunkUploadHandle {
  uploadId: string
  onProgress: (fn: (pct: number) => void) => void
  pause: () => void
  resume: () => void
  abort: () => void
  /** asset_id 仅在 purpose=media 时由服务端返回（落 media_assets 后可用它换签名 URL） */
  done: Promise<{ file_url: string; upload_id: string; size: number; asset_id?: string }>
}

const DEFAULT_CHUNK = 2 * 1024 * 1024
const DEFAULT_CONCURRENCY = 3

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

async function uploadChunk(uploadId: string, index: number, blob: Blob): Promise<void> {
  await api.put(`/v1/uploads/${uploadId}/chunks/${index}`, blob, {
    headers: { 'Content-Type': 'application/octet-stream' },
    timeout: 120000,
  })
}

export async function createChunkUpload(file: File, opts: ChunkUploadOptions): Promise<ChunkUploadHandle> {
  const chunkSize = opts.chunkSize || DEFAULT_CHUNK
  const concurrency = Math.max(1, opts.concurrency || DEFAULT_CONCURRENCY)
  const chunkCount = Math.max(1, Math.ceil(file.size / chunkSize))

  // 断点续传：localStorage 按 文件指纹 + purpose 记录 upload_id。
  // 指纹必须能区分不同文件：仅用 name+size 会让「同名同大小」的文件
  //（导出文件、重复下载、批量截图很常见）复用同一个 upload_id，
  // 把一个文件的分片写进另一个的上传会话。补入 lastModified 与分片大小。
  const fileFingerprint = `${file.name}:${file.size}:${file.lastModified}:${chunkSize}`
  const storageKey = `chunk-upload:${opts.purpose}:${opts.parentId || ''}:${fileFingerprint}`
  let uploadId = localStorage.getItem(storageKey) || ''
  let received: number[] = []
  if (uploadId) {
    try {
      const res = await api.get(`/v1/uploads/${uploadId}`)
      received = (res.data?.data?.received_chunks as number[]) || []
    } catch {
      uploadId = ''
    }
  }
  if (!uploadId) {
    const res = await api.post('/v1/uploads', {
      name: file.name,
      size: file.size,
      mime_type: file.type || '',
      purpose: opts.purpose,
      parent_id: opts.parentId || '',
      category: opts.category || '',
      chunk_size: chunkSize,
    })
    uploadId = res.data?.data?.upload_id
    localStorage.setItem(storageKey, uploadId)
  }

  const receivedSet = new Set(received)
  let paused = false
  let aborted = false
  let doneCount = receivedSet.size
  const progressFns: Array<(p: number) => void> = []
  const notify = () => {
    const pct = Math.min(99, Math.round((doneCount / chunkCount) * 100))
    progressFns.forEach(fn => fn(pct))
  }

  const runChunk = async (index: number) => {
    if (aborted || receivedSet.has(index)) return
    const start = index * chunkSize
    const end = Math.min(file.size, start + chunkSize)
    const blob = file.slice(start, end)
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        await uploadChunk(uploadId, index, blob)
        receivedSet.add(index)
        doneCount++
        notify()
        return
      } catch (e) {
        if (aborted) return
        if (attempt === 2) throw e
        await sleep(1000 * 2 ** attempt)
      }
    }
  }

  const donePromise = (async () => {
    let idx = 0
    const worker = async () => {
      while (true) {
        if (aborted) throw new Error('upload aborted')
        if (paused) { await sleep(200); continue }
        const i = idx++
        if (i >= chunkCount) return
        await runChunk(i)
      }
    }
    const workers = Array.from({ length: Math.min(concurrency, chunkCount) }, () => worker())
    await Promise.all(workers)
    if (aborted) throw new Error('upload aborted')
    // 全部成功 → complete（合并落库）
    const res = await api.post(`/v1/uploads/${uploadId}/complete`)
    localStorage.removeItem(storageKey)
    const data = res.data?.data
    progressFns.forEach(fn => fn(100))
    return data as { file_url: string; upload_id: string; size: number; asset_id?: string }
  })()

  return {
    uploadId,
    onProgress: (fn) => { progressFns.push(fn); fn(Math.min(99, Math.round((doneCount / chunkCount) * 100))) },
    pause: () => { paused = true },
    resume: () => { paused = false },
    abort: () => { aborted = true },
    done: donePromise,
  }
}
