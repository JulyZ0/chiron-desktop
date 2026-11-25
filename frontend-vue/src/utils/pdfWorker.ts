/**
 * G21 —— PDF 预解析的主线程薄封装。
 *
 * 修前会怎样：页数 / 页面尺寸这类纯字节计算没有 Worker 可去，只能压在主线程上；
 * 大 PDF 打开时滚动掉帧、输入延迟。现在主线程只负责 `await`：
 * 字节交给 `pdfParse.worker.ts`，算完把结果拿回来。
 *
 * **降级是硬要求**：Worker 不可用（老环境 / 被 CSP 拦 / 打包产物缺失）、构造抛错、
 * 回包形状不对、超时 —— 一律返回 `null`，绝不把异常或"假结果"抛给 UI。
 * 调用方拿到 `null` 就回退原路径（FileViewer 自己渲染），用户侧行为不变。
 */

import type { PdfSummary } from '../workers/pdfStructure'

export type { PdfSummary }

/**
 * 主线程侧需要的最小 Worker 面貌（只是为了可注入，测试里能塞假 Worker）。
 * 真实 `Worker` 结构上满足它，所以默认工厂可以直接返回 `new Worker(...)`。
 */
export interface PdfWorkerLike {
  onmessage: ((event: MessageEvent) => void) | null
  onerror: ((event: ErrorEvent) => void) | null
  postMessage(message: unknown, transfer: Transferable[]): void
  terminate(): void
}

export interface ParsePdfSummaryOptions {
  /** 超时上限（毫秒）。超时即放弃等待并终止 Worker。 */
  timeoutMs?: number
  /** 注入 Worker 工厂；默认走 `new Worker(new URL(..., import.meta.url))`。 */
  createWorker?: () => PdfWorkerLike | null
}

/** 4s：大 PDF 也够扫完；再久就是 Worker 出问题了，不如早点回退原路径。 */
export const DEFAULT_PDF_WORKER_TIMEOUT_MS = 4000

/** Vite 原生支持 `new Worker(new URL(...), { type: 'module' })`，无需改打包配置。 */
function createModuleWorker(): PdfWorkerLike | null {
  if (typeof Worker === 'undefined') return null
  return new Worker(new URL('../workers/pdfParse.worker.ts', import.meta.url), { type: 'module' })
}

/**
 * 校验 Worker 回包形状。宁可判死也不放行：
 * 页数必须正整数、尺寸必须是有限数的数对 —— 否则上层会把坏数据当真的用。
 */
function readSummary(data: unknown): PdfSummary | null {
  if (!data || typeof data !== 'object') return null
  const message = data as { ok?: unknown; summary?: unknown }
  if (message.ok !== true) return null
  if (!message.summary || typeof message.summary !== 'object') return null

  const { pages, sizes } = message.summary as { pages?: unknown; sizes?: unknown }
  if (typeof pages !== 'number' || !Number.isInteger(pages) || pages <= 0) return null
  if (!Array.isArray(sizes)) return null

  const parsedSizes: Array<[number, number]> = []
  for (const entry of sizes) {
    if (!Array.isArray(entry) || entry.length !== 2) return null
    const width = entry[0] as unknown
    const height = entry[1] as unknown
    if (typeof width !== 'number' || typeof height !== 'number') return null
    if (!Number.isFinite(width) || !Number.isFinite(height)) return null
    parsedSizes.push([width, height])
  }
  return { pages, sizes: parsedSizes }
}

/**
 * 解析 PDF 基本信息（页数 + 逐页尺寸）。
 *
 * 注意：`bytes` 会被**转移**（transfer）给 Worker，调用后该 ArrayBuffer 即被分离，
 * 不要复用 —— 需要保留原件就自己先 `slice()` 一份。
 */
export function parsePdfSummary(
  bytes: ArrayBuffer,
  options: ParsePdfSummaryOptions = {},
): Promise<PdfSummary | null> {
  if (!(bytes instanceof ArrayBuffer) || bytes.byteLength === 0) return Promise.resolve(null)

  const createWorker = options.createWorker ?? createModuleWorker
  let worker: PdfWorkerLike | null = null
  try {
    worker = createWorker()
  } catch {
    // 构造失败（CSP / 无 worker 支持 / 打包产物缺失）—— 降级，不抛
    return Promise.resolve(null)
  }
  if (!worker) return Promise.resolve(null)

  // 闭包里要用，先收成 const：否则 `let` 的窄化在回调里不成立。
  const target = worker
  const timeoutMs = options.timeoutMs ?? DEFAULT_PDF_WORKER_TIMEOUT_MS

  return new Promise<PdfSummary | null>((resolve) => {
    let settled = false

    const finish = (value: PdfSummary | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try {
        target.terminate()
      } catch {
        // 终止失败不影响结果（线程可能已经自己退出）
      }
      resolve(value)
    }

    // 先登记超时再发消息：`finish` 里引用 `timer`，但真正被调用时它一定已初始化
    // （只有 Worker 回包 / 超时 / postMessage 抛错才会走到 `finish`，都在这一行之后）。
    const timer = setTimeout(() => finish(null), timeoutMs)

    target.onmessage = (event) => finish(readSummary(event.data))
    target.onerror = () => finish(null)

    try {
      // 只转移不拷贝：大文件的 ArrayBuffer 直接换手，主线程内存立刻释放
      target.postMessage({ id: 1, bytes }, [bytes])
    } catch {
      finish(null)
    }
  })
}
