import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

/**
 * G21 —— MediaView 的 PDF 预览必须把字节解析交给 Worker。
 *
 * 修前会怎样：`openPreview` 只解析签名 URL 就交给 FileViewer，页数/页面尺寸这类
 * 纯字节计算全在主线程上做（本仓此前根本没有 Worker）—— 4MB 的 PDF 就足以让
 * 点击到首帧之间卡住，滚动掉帧、输入延迟。
 *
 * 这里用假 Worker（`vi.mock('../../utils/pdfWorker')`）钉住两件事：
 *   1) 点击预览的**同步栈**里没有任何重活（不 fetch、不解析），取字节与解析都发生在
 *      第一个 `await` 之后的微任务里 —— 即"主线程只做了 await"；
 *   2) Worker 降级（返回 null）时预览照旧走 FileViewer，UI 行为不变。
 *
 * 注意：详情侧栏（antd Drawer）与预览（antd Modal）都 teleport 到 body，
 * 因此交互与断言都走 `document.body`（与 BillingView / PricesView 的用法一致）。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  resolveMediaUrl: vi.fn(async (a: { id?: string; file_url?: string }) => a?.file_url || ''),
  invalidateMediaUrl: vi.fn(),
  isMediaUrlExpired: vi.fn((url: string) => {
    const exp = Number(new URL(url, 'http://api.test').searchParams.get('exp'))
    return Number.isFinite(exp) && exp * 1000 <= Date.now()
  }),
}))

// 假 Worker 的替身：`parsePdfSummary` 被整体替换，MediaView 只需 await 它。
const pdfMocks = vi.hoisted(() => ({ parsePdfSummary: vi.fn() }))

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../api', () => ({
  api: {
    get: apiMocks.get,
    post: apiMocks.post,
    put: apiMocks.put,
    delete: apiMocks.delete,
  },
  resolveMediaUrl: apiMocks.resolveMediaUrl,
  invalidateMediaUrl: apiMocks.invalidateMediaUrl,
  isMediaUrlExpired: apiMocks.isMediaUrlExpired,
  API_URL: 'http://api.test',
  // `stores/auth.ts` 在 setup 阶段就要读这几项（见 auth.ts:3）；整模块 mock 漏掉
  // 任何一个，store 初始化就会抛 "No export is defined on the mock"。
  currentAuthGeneration: () => 0,
  bumpAuthGeneration: () => 1,
  desktopDeviceToken: () => '',
  loadDesktopToken: async () => null,
  setAuthToken: () => {},
}))

vi.mock('../../utils/uploader', () => ({ createChunkUpload: vi.fn() }))
vi.mock('@file-viewer/vue3', () => ({
  FileViewer: { name: 'FileViewer', template: '<div class="file-viewer-stub" />' },
}))
vi.mock('@file-viewer/preset-all', () => ({ default: {} }))
vi.mock('../../utils/pdfWorker', () => ({ parsePdfSummary: pdfMocks.parsePdfSummary }))

import MediaView from '../MediaView.vue'
import { i18n } from '../../i18n'

const tr = (key: string): string => (i18n.global as unknown as { t: (k: string) => string }).t(key)

const SIGNED_PDF_URL = 'http://api.test/media/s/m2.pdf?exp=4102444800&sig=ok'
const BIG_PDF_BYTES = 4 * 1024 * 1024

const pdfItem = {
  id: 'm2',
  type: 'file',
  name: '年度报告.pdf',
  size: BIG_PDF_BYTES,
  file_url: '/media/年度报告.pdf',
  mime_type: 'application/pdf',
  created_at: '2026-01-03T00:00:00Z',
}

const imageItem = {
  id: 'm1',
  type: 'image',
  name: 'a.png',
  size: 1234,
  file_url: '/media/a.png',
  mime_type: 'image/png',
  created_at: '2026-01-02T00:00:00Z',
}

const mountedWrappers: VueWrapper[] = []

function listResponse(items: unknown[], total = items.length) {
  return { data: { data: { items, total } } }
}

/** 挂载（挂到真实 body 上，antd 弹层才可见）→ 双击卡片打开详情侧栏。 */
async function mountDetail(item: typeof pdfItem | typeof imageItem): Promise<VueWrapper> {
  apiMocks.get.mockResolvedValue(listResponse([item]))
  setActivePinia(createPinia())
  const wrapper = mount(MediaView, { attachTo: document.body })
  mountedWrappers.push(wrapper)
  await flushPromises()
  await wrapper.findAll('.file-card')[0].trigger('dblclick')
  await flushPromises()
  return wrapper
}

/**
 * 详情侧栏里的「预览」按钮（模板里 `@click="openPreview(detailItem)"`）。
 *
 * antd 的 Button 会给两个汉字之间自动插空格（实测渲染成「预 览」），所以比较前
 * 两边都去掉空白；不按 DOM 顺序取，避免以后侧栏加按钮时下标错位。
 */
function previewButton(): HTMLButtonElement {
  const normalize = (text: string | null): string => (text || '').replace(/\s+/g, '')
  const target = normalize(tr('common.preview'))
  const buttons = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button'))
  const button = buttons.find((b) => normalize(b.textContent) === target)
  if (!button) {
    const texts = buttons.map((b) => JSON.stringify((b.textContent || '').trim())).join(', ')
    throw new Error(`详情侧栏里没有找到「预览」按钮；现有按钮文案: [${texts}]`)
  }
  return button
}

/**
 * 同步点击（原生 dispatch，不经过 VTU 的 `trigger`）：
 * 只有这样才能在**同一帧**里断言"点击的同步栈里没有重活"。
 */
function clickPreview(): void {
  previewButton().dispatchEvent(new MouseEvent('click', { bubbles: true }))
}

function previewShell(): Element | null {
  return document.body.querySelector('.preview-shell')
}

function stubFetchBytes(size: number) {
  const fetchMock = vi.fn(async () => new Response(new Uint8Array(size), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.clearAllMocks()
  pdfMocks.parsePdfSummary.mockReset()
  localStorage.clear()
  apiMocks.get.mockResolvedValue(listResponse([]))
  apiMocks.resolveMediaUrl.mockImplementation(async (a: { id?: string; file_url?: string }) => a?.file_url || '')
  vi.unstubAllGlobals()
})

afterEach(() => {
  while (mountedWrappers.length) mountedWrappers.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('MediaView 打开 PDF 预览：解析走 Worker，主线程只 await', () => {
  it('点击预览的同步阶段不取字节、不解析；取字节与解析都发生在微任务里', async () => {
    apiMocks.resolveMediaUrl.mockResolvedValue(SIGNED_PDF_URL)
    const fetchMock = stubFetchBytes(BIG_PDF_BYTES)
    pdfMocks.parsePdfSummary.mockResolvedValue({ pages: 12, sizes: [[595, 842]] })
    await mountDetail(pdfItem)

    clickPreview()

    // 关键断言：同步栈里没有任何重活（修前这条会看到 fetch/解析在点击当帧就跑完）
    expect(fetchMock).not.toHaveBeenCalled()
    expect(pdfMocks.parsePdfSummary).not.toHaveBeenCalled()

    await flushPromises()

    // 4MB 文件被整块交给 Worker 侧解析，主线程只拿到一个 Promise 的结果
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(SIGNED_PDF_URL)
    expect(pdfMocks.parsePdfSummary).toHaveBeenCalledTimes(1)
    const handedToWorker = pdfMocks.parsePdfSummary.mock.calls[0][0]
    expect(handedToWorker).toBeInstanceOf(ArrayBuffer)
    expect((handedToWorker as ArrayBuffer).byteLength).toBe(BIG_PDF_BYTES)
  })

  it('Worker 正常时页数落到预览壳上（其余 UI 不变：仍然是 FileViewer）', async () => {
    apiMocks.resolveMediaUrl.mockResolvedValue(SIGNED_PDF_URL)
    stubFetchBytes(BIG_PDF_BYTES)
    pdfMocks.parsePdfSummary.mockResolvedValue({ pages: 12, sizes: [[595, 842]] })
    await mountDetail(pdfItem)

    clickPreview()
    await flushPromises()

    expect(document.body.querySelector('.file-viewer-stub')).not.toBeNull()
    expect(previewShell()?.getAttribute('data-pdf-pages')).toBe('12')
  })

  it('Worker 降级（null）时预览照旧打开，且不多出任何页数标记', async () => {
    apiMocks.resolveMediaUrl.mockResolvedValue(SIGNED_PDF_URL)
    stubFetchBytes(BIG_PDF_BYTES)
    pdfMocks.parsePdfSummary.mockResolvedValue(null)
    await mountDetail(pdfItem)

    clickPreview()
    await flushPromises()

    expect(document.body.querySelector('.file-viewer-stub')).not.toBeNull()
    expect(previewShell()?.getAttribute('data-pdf-pages')).toBeNull()
  })

  it('取字节失败（网络错误）时降级，预览仍然打开', async () => {
    apiMocks.resolveMediaUrl.mockResolvedValue(SIGNED_PDF_URL)
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down')
    }))
    pdfMocks.parsePdfSummary.mockResolvedValue({ pages: 1, sizes: [] })
    await mountDetail(pdfItem)

    clickPreview()
    await flushPromises()

    expect(document.body.querySelector('.file-viewer-stub')).not.toBeNull()
    expect(pdfMocks.parsePdfSummary).not.toHaveBeenCalled()
    expect(previewShell()?.getAttribute('data-pdf-pages')).toBeNull()
  })

  it('非 PDF 不触发预解析（不为图片白下一遍文件）', async () => {
    apiMocks.resolveMediaUrl.mockResolvedValue('http://api.test/media/s/m1.png?exp=4102444800&sig=ok')
    const fetchMock = stubFetchBytes(1024)
    await mountDetail(imageItem)

    clickPreview()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(pdfMocks.parsePdfSummary).not.toHaveBeenCalled()
    expect(previewShell()?.getAttribute('data-pdf-pages')).toBeNull()
  })
})
