/**
 * PDF 结构探测（纯函数：无 DOM、无依赖、可在 Worker 里跑）。
 *
 * 为什么不用 pdfjs：本任务不允许新增依赖，而且要的只是「页数 + 页面尺寸」这种
 * **字典层面**的信息 —— 不需要解码内容流、不需要字体/cmap、不需要渲染。
 *
 * 做法（完全字节层面，不做任何猜测）：
 *   - 文件头里有 `%PDF-` 才算 PDF，否则 `ok: false`；
 *   - 页对象 `/Type /Page` 的个数 = 页数（`/Type /Pages` 是页树节点，不算）；
 *   - `/MediaBox [x0 y0 x1 y1]` → 页面尺寸 pt：宽 = x1-x0、高 = y1-y0；
 *   - 页对象一个都扫不到时，用页树字典里的 `/Count n` 兜底（页数可信、尺寸未知）；
 *   - 两者都扫不到 → `ok: false`，**绝不编一个页数出来**（错的页数比没有更糟）。
 *
 * 已知边界（诚实降级，不假装解析成功）：
 *   - PDF 1.5+ 可以把对象字典压进**对象流**（`/ObjStm`）。这类文件在字节层面
 *     根本看不到 `/Type /Page`，于是走 `ok: false`，主线程回退到原预览路径。
 *   - 超过 `MAX_SCAN_BYTES` 的文件不扫：截断扫描会把页数数少，而少算的页数会被
 *     上层当成事实用 —— 宁可返回 `too-large-to-scan`。
 *   - 加密 PDF（`/Encrypt`）的字典通常仍是明文，能读到就照读。
 */

/** 主线程只需要这两样：页数与逐页尺寸（pt）。 */
export interface PdfSummary {
  pages: number
  sizes: Array<[number, number]>
}

export type PdfParseResult =
  | { ok: true; summary: PdfSummary }
  | { ok: false; reason: string }

/** 只在开头这么多字节里找 `%PDF-`：规范要求头必须在文件最前，留点余量给 BOM/垃圾前缀。 */
const HEADER_PROBE_BYTES = 1024

/** 扫描上限：再大就不扫（见文件头注释里的取舍）。 */
const MAX_SCAN_BYTES = 48 * 1024 * 1024

/** 尺寸表上限：几千页的 PDF 也没人会把每一页尺寸都塞进 UI，白占内存。 */
const MAX_SIZES = 512

/** 页数上限：离谱的值直接判为解析失败，避免把坏数据当真。 */
const MAX_PAGES = 200_000

const PAGE_OBJECT_RE = /\/Type\s*\/Page(?![a-zA-Z])/g
const MEDIA_BOX_RE = /\/MediaBox\s*\[\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\]/g
const PAGE_TREE_COUNT_RE = /\/Count\s+(\d+)/g

/**
 * 字节 → 单字节字符串（latin1）。
 *
 * 不用 `TextDecoder`：worker 与 node 下的编码别名不完全一致（`latin1` 在部分实现里
 * 落到 windows-1252）。这里只是做**标记扫描**，逐字节映射就够，且不产生编码歧义。
 * 分块拼接是为了避开 `fromCharCode` 的参数上限。
 */
function toByteString(bytes: Uint8Array): string {
  const CHUNK = 0x4000
  let out = ''
  for (let offset = 0; offset < bytes.length; offset += CHUNK) {
    const slice = bytes.subarray(offset, Math.min(offset + CHUNK, bytes.length))
    out += String.fromCharCode(...slice)
  }
  return out
}

function round2(value: number): number {
  return Math.round(value * 100) / 100
}

export function summarizePdf(bytes: Uint8Array): PdfParseResult {
  if (!bytes || bytes.byteLength === 0) return { ok: false, reason: 'empty' }
  if (bytes.byteLength > MAX_SCAN_BYTES) return { ok: false, reason: 'too-large-to-scan' }

  const header = toByteString(bytes.subarray(0, Math.min(HEADER_PROBE_BYTES, bytes.byteLength)))
  if (!header.includes('%PDF-')) return { ok: false, reason: 'not-pdf' }

  const text = toByteString(bytes)

  const sizes: Array<[number, number]> = []
  for (const match of text.matchAll(MEDIA_BOX_RE)) {
    const x0 = Number(match[1])
    const y0 = Number(match[2])
    const x1 = Number(match[3])
    const y1 = Number(match[4])
    if (![x0, y0, x1, y1].every(Number.isFinite)) continue
    const width = round2(Math.abs(x1 - x0))
    const height = round2(Math.abs(y1 - y0))
    if (width <= 0 || height <= 0) continue
    sizes.push([width, height])
    if (sizes.length >= MAX_SIZES) break
  }

  const pageObjects = (text.match(PAGE_OBJECT_RE) ?? []).length
  if (pageObjects > 0 && pageObjects <= MAX_PAGES) {
    // sizes 可能比 pages 少：`/MediaBox` 走间接引用（`/MediaBox 12 0 R`）时字节层面
    // 读不到数，宁可少给几页尺寸，也不拿默认纸张去填（那等于编数据）。
    return { ok: true, summary: { pages: pageObjects, sizes } }
  }

  // 兜底：页树字典里的 /Count（页对象被对象流吞掉时唯一还能信的数）
  let maxCount = 0
  for (const match of text.matchAll(PAGE_TREE_COUNT_RE)) {
    const count = Number(match[1])
    if (Number.isFinite(count) && count > maxCount) maxCount = count
  }
  if (maxCount > 0 && maxCount <= MAX_PAGES) {
    return { ok: true, summary: { pages: maxCount, sizes: [] } }
  }

  return { ok: false, reason: 'no-page-structure' }
}
