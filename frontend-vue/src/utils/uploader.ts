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
import {
  canPersistUpload,
  decodeUploadEnvelope,
  encodeUploadEnvelope,
  normalizeUploadOwner,
  uploadStorageKey,
  type UploadOwner,
} from './uploadIdentity'

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
  done: Promise<{ file_url: string; upload_id: string; size: number; asset_id?: string; duplicate?: boolean }>
}

const DEFAULT_CHUNK = 2 * 1024 * 1024
const DEFAULT_CONCURRENCY = 3

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

/**
 * 当前上传身份（H72）：user + tenant + mode + origin。
 *
 * 只有 user id 是不够的：同一账号在「本地模式 ↔ 团队云」或两个不同部署之间，
 * 服务端的 `upload_id` 毫无关系。把 mode/origin 也算进身份，续传状态才不会
 * 跨实例复用。
 */
function currentUploadOwner(): UploadOwner {
  let userId = ''
  let tenantId = ''
  try {
    const stored = JSON.parse(localStorage.getItem('user') || '{}') as {
      id?: unknown
      tenant_id?: unknown
      tenantId?: unknown
    }
    if (typeof stored.id === 'string') userId = stored.id
    const tenant = stored.tenant_id ?? stored.tenantId
    if (typeof tenant === 'string') tenantId = tenant
    else if (typeof tenant === 'number') tenantId = String(tenant)
  } catch {
    // 解析失败 = 没有可信身份，按"不可持久化续传"处理。
  }
  let mode = ''
  try {
    // 动态 require 避免把 deviceMode 的初始化副作用拉进纯函数单测。
    mode = String((globalThis as { __chironDeviceMode?: string }).__chironDeviceMode || '')
  } catch {
    mode = ''
  }
  return normalizeUploadOwner({
    userId,
    tenantId,
    mode,
    origin: typeof window !== 'undefined' ? window.location?.origin || '' : '',
  })
}

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
  const owner = currentUploadOwner()
  const storageKey = uploadStorageKey({
    purpose: opts.purpose,
    parentId: opts.parentId,
    fingerprint: fileFingerprint,
  })
  // H72：值不再是裸 upload_id，而是带归属的信封；读时比对身份，
  // 不一致就**丢弃**（宁可重传一次，也不要接着别人的会话写分片）。
  const storedRaw = localStorage.getItem(storageKey)
  const verdict = decodeUploadEnvelope(storedRaw, owner, fileFingerprint)
  let uploadId = verdict.usable ? verdict.uploadId : ''
  if (!verdict.usable && storedRaw) {
    // 旧版裸字符串、别人的状态、或结构损坏：一律清掉，避免下次再撞同一把坏钥匙。
    localStorage.removeItem(storageKey)
  }
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
    if (uploadId && canPersistUpload(owner)) {
      localStorage.setItem(
        storageKey,
        encodeUploadEnvelope({ uploadId, owner, fingerprint: fileFingerprint }),
      )
    }
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
    return data as { file_url: string; upload_id: string; size: number; asset_id?: string; duplicate?: boolean }
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
