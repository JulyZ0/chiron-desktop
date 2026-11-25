import { describe, it, expect, vi } from 'vitest'

/**
 * G21 —— PDF 预解析进 Worker 的契约测试（主线程侧）。
 *
 * 修前会怎样：`src/utils/pdfWorker.ts` 与 `src/workers/**` 都还不存在，全仓
 * `new Worker` 为零 —— PDF 走 `<iframe>` / 第三方 FileViewer，页数与页面尺寸这类
 * 纯字节计算全压在主线程上，大文件时滚动掉帧、输入延迟。
 *
 * 这里钉住的不是实现细节，而是三条**降级边界**：Worker 不可用 / 构造失败 / 超时，
 * 都必须给调用方 `null`（回退原路径），绝不把异常抛进 UI。
 */

import { parsePdfSummary, type PdfWorkerLike } from '../pdfWorker'
import { summarizePdf } from '../../workers/pdfStructure'

/** 假 Worker：按用例注入"怎么回包"，用来替代真实线程（jsdom 里没有 Worker）。 */
class FakeWorker implements PdfWorkerLike {
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  readonly received: unknown[] = []
  terminated = false
  postMessageThrows = false

  constructor(private readonly onPost: (message: unknown, worker: FakeWorker) => void = () => {}) {}

  postMessage(message: unknown): void {
    this.received.push(message)
    if (this.postMessageThrows) throw new Error('数据克隆失败')
    this.onPost(message, this)
  }

  terminate(): void {
    this.terminated = true
  }

  /** 模拟 Worker 回包，结构与 `pdfParse.worker.ts` 的真实回包一致 */
  reply(data: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  crash(): void {
    this.onerror?.(new Error('worker crashed'))
  }
}

function requestIdOf(message: unknown): number {
  const id = (message as { id?: unknown }).id
  return typeof id === 'number' ? id : -1
}

describe('parsePdfSummary —— Worker 正常路径', () => {
  it('Worker 回包合法时把页数与页面尺寸原样交回主线程', async () => {
    const sizes: Array<[number, number]> = [[595, 842], [612, 792], [595, 842]]
    const worker = new FakeWorker((message, self) => {
      self.reply({ id: requestIdOf(message), ok: true, summary: { pages: 3, sizes } })
    })

    const summary = await parsePdfSummary(new ArrayBuffer(4096), { createWorker: () => worker })

    expect(summary).toEqual({ pages: 3, sizes })
    // 字节是**转移**给 Worker 的（列表非空即证明用了 transfer list，不是结构化克隆拷贝）
    expect(worker.received).toHaveLength(1)
    expect(worker.terminated).toBe(true)
  })

  it('Worker 报告解析失败（例如对象流压缩）时降级为 null，不假装解析成功', async () => {
    const worker = new FakeWorker((message, self) => {
      self.reply({ id: requestIdOf(message), ok: false, reason: 'no-page-objects' })
    })

    const summary = await parsePdfSummary(new ArrayBuffer(64), { createWorker: () => worker })

    expect(summary).toBeNull()
    expect(worker.terminated).toBe(true)
  })

  it('回包形状不合法（页数非正整数 / 尺寸不是数对）时降级为 null', async () => {
    const badPages = new FakeWorker((message, self) => {
      self.reply({ id: requestIdOf(message), ok: true, summary: { pages: 0, sizes: [] } })
    })
    await expect(
      parsePdfSummary(new ArrayBuffer(64), { createWorker: () => badPages }),
    ).resolves.toBeNull()

    const badSizes = new FakeWorker((message, self) => {
      self.reply({ id: requestIdOf(message), ok: true, summary: { pages: 2, sizes: [['a', 'b']] } })
    })
    await expect(
      parsePdfSummary(new ArrayBuffer(64), { createWorker: () => badSizes }),
    ).resolves.toBeNull()
  })

  it('Worker 抛 error 事件时降级为 null', async () => {
    const worker = new FakeWorker((_message, self) => self.crash())

    await expect(
      parsePdfSummary(new ArrayBuffer(64), { createWorker: () => worker }),
    ).resolves.toBeNull()
  })

  it('重复回包只结算一次（不会二次 resolve 或二次 terminate）', async () => {
    const terminate = vi.fn()
    const worker = new FakeWorker((message, self) => {
      self.reply({ id: requestIdOf(message), ok: true, summary: { pages: 1, sizes: [[595, 842]] } })
      self.reply({ id: requestIdOf(message), ok: true, summary: { pages: 9, sizes: [] } })
    })
    worker.terminate = terminate

    const summary = await parsePdfSummary(new ArrayBuffer(64), { createWorker: () => worker })

    expect(summary).toEqual({ pages: 1, sizes: [[595, 842]] })
    expect(terminate).toHaveBeenCalledTimes(1)
  })
})

describe('parsePdfSummary —— 降级（修前必然失败：Worker 路径根本不存在）', () => {
  it('Worker 构造抛错时返回 null，而不是把异常抛给调用方', async () => {
    const createWorker = () => {
      throw new Error('CSP 拦住了 Worker 构造')
    }

    await expect(parsePdfSummary(new ArrayBuffer(128), { createWorker })).resolves.toBeNull()
  })

  it('环境里没有 Worker（工厂返回 null）时返回 null', async () => {
    await expect(
      parsePdfSummary(new ArrayBuffer(128), { createWorker: () => null }),
    ).resolves.toBeNull()
  })

  it('jsdom 这种没有全局 Worker 的环境走默认工厂，同样返回 null', async () => {
    // 不注入 createWorker：默认工厂必须自己发现 `typeof Worker === 'undefined'` 并降级
    await expect(parsePdfSummary(new ArrayBuffer(128))).resolves.toBeNull()
  })

  it('超时未回包时放弃等待、终止 Worker 并返回 null', async () => {
    const worker = new FakeWorker() // 永不回包
    const startedAt = Date.now()

    const summary = await parsePdfSummary(new ArrayBuffer(128), {
      createWorker: () => worker,
      timeoutMs: 30,
    })

    expect(summary).toBeNull()
    expect(worker.terminated).toBe(true)
    // 真的等过这个超时窗口，而不是立刻失败（否则"超时"这条分支没被覆盖）
    expect(Date.now() - startedAt).toBeGreaterThanOrEqual(20)
  })

  it('postMessage 抛错（字节转移失败）时降级为 null', async () => {
    const worker = new FakeWorker()
    worker.postMessageThrows = true

    await expect(
      parsePdfSummary(new ArrayBuffer(128), { createWorker: () => worker }),
    ).resolves.toBeNull()
  })

  it('空字节直接返回 null，不去构造 Worker', async () => {
    const createWorker = vi.fn(() => null)

    await expect(parsePdfSummary(new ArrayBuffer(0), { createWorker })).resolves.toBeNull()
    expect(createWorker).not.toHaveBeenCalled()
  })
})

/**
 * 纯解析函数（Worker 里跑的就是它）。放在这里一起钉住，是因为"页数"错了比"没解析"
 * 更糟：错误的页数会被上层当成真的用。所以不可解析时必须 `ok: false`。
 */
describe('summarizePdf —— 纯字节层面的结构探测', () => {
  function asciiBytes(text: string): Uint8Array {
    return Uint8Array.from(text, (ch) => ch.charCodeAt(0) & 0xff)
  }

  const MINIMAL_PDF = [
    '%PDF-1.7',
    '1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj',
    '2 0 obj << /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >> endobj',
    '3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >> endobj',
    '5 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj',
    'trailer << /Root 1 0 R >>',
    '%%EOF',
  ].join('\n')

  it('从 /Type /Page 对象数出页数，并按 MediaBox 给出页面尺寸（pt）', () => {
    const result = summarizePdf(asciiBytes(MINIMAL_PDF))

    expect(result).toEqual({
      ok: true,
      summary: { pages: 2, sizes: [[595, 842], [612, 792]] },
    })
  })

  it('/Count 只在数不到页对象时兜底：页数可信、尺寸如实为空', () => {
    const text = '%PDF-1.4\n1 0 obj << /Type /Pages /Count 7 >> endobj\n%%EOF'
    const result = summarizePdf(asciiBytes(text))

    expect(result).toEqual({ ok: true, summary: { pages: 7, sizes: [] } })
  })

  it('文件头不是 %PDF 时返回 ok:false，不假装解析成功', () => {
    const result = summarizePdf(asciiBytes('<html><body>not a pdf</body></html>'))

    expect(result.ok).toBe(false)
  })

  it('对象字典被对象流压缩、页对象与 /Count 都扫不到时返回 ok:false', () => {
    const text = [
      '%PDF-1.5',
      '1 0 obj << /Type /ObjStm /N 12 /First 96 /Filter /FlateDecode >> stream',
      'binary-ish',
      'endstream endobj',
      '%%EOF',
    ].join('\n')
    const result = summarizePdf(asciiBytes(text))

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toBe('no-page-structure')
  })

  it('空字节返回 ok:false', () => {
    expect(summarizePdf(new Uint8Array(0)).ok).toBe(false)
  })

  it('超出扫描上限的巨型文件返回 ok:false，而不是给出被截断的错页数', () => {
    const huge = new Uint8Array(64 * 1024 * 1024 + 1)
    const header = asciiBytes('%PDF-1.7')
    huge.set(header, 0)

    const result = summarizePdf(huge)

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toBe('too-large-to-scan')
  })
})
