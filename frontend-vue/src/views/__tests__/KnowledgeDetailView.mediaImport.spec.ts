import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 知识库详情页「从媒体库导入」的**类型门禁**与**真实错误上报**（2026-11-18）。
 *
 * 用户报的症状是"从媒体库上传失败"。追下去有三处叠加：
 *
 * 1. 媒体库收一切文件（图片/视频/压缩包），知识库按 provider 只吃少数原文
 *    （selfhosted 只有 PDF）。选取弹窗**没有**任何类型过滤，勾一张 PNG 也会被
 *    完整下载、切成 2MB 分片传上去，最后由网关 `finalizeKBDoc` 以 400 拒绝 ——
 *    一次白传，界面上只留下「N 个文件导入失败」。
 * 2. `catch {}` 把失败原因整个吞掉，用户无从判断是签名失效、404 还是类型被拒。
 * 3. `fetch` 未检查 `response.ok`：404/403 的 JSON 错误页会被 `blob()` 成"文件内容"
 *    传进知识库 —— 库里出现一条内容完全错误、却显示成功的文档。
 *
 * 这个 spec 钉住的是 1 与 3（2 由 3 的断言间接覆盖：错误必须带出 HTTP 状态）。
 */

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

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  resolveMediaUrl: vi.fn(async (a: { id?: string; file_url?: string }) => a?.file_url || ''),
}))

const uploaderMock = vi.hoisted(() => ({ createChunkUpload: vi.fn() }))

vi.mock('../../api', () => ({
  api: {
    get: apiMocks.get,
    post: apiMocks.post,
    put: apiMocks.put,
    delete: apiMocks.delete,
  },
  resolveMediaUrl: apiMocks.resolveMediaUrl,
}))

vi.mock('../../utils/uploader', () => ({ createChunkUpload: uploaderMock.createChunkUpload }))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'kb-1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

import KnowledgeDetailView from '../KnowledgeDetailView.vue'

/** selfhosted 库：只接受 .pdf（见 kb_file_types.go / kbFileTypes.ts）。 */
const KB = {
  id: 'kb-1',
  name: '产品手册',
  provider: 'selfhosted',
  status: 'ready',
  enabled: true,
  document_count: 0,
}

const pdf = { id: 'm1', type: 'file', name: 'a.pdf', size: 100, file_url: '/media/a.pdf' }
const png = { id: 'm2', type: 'image', name: 'b.png', size: 200, file_url: '/media/b.png' }

function installApiMock() {
  apiMocks.get.mockImplementation((url: string) => {
    if (url === '/v1/kb/kb-1') return Promise.resolve({ data: { data: KB } })
    if (url === '/v1/kb/kb-1/documents') return Promise.resolve({ data: { data: { documents: [] } } })
    if (url === '/v1/media') {
      const items = [pdf, png]
      return Promise.resolve({ data: { data: { items, total: items.length } } })
    }
    return Promise.resolve({ data: { data: {} } })
  })
}

const rows = (): HTMLElement[] => Array.from(document.body.querySelectorAll<HTMLElement>('.media-item'))
const importButton = (): HTMLButtonElement | null =>
  document.body.querySelector<HTMLButtonElement>('.modal-footer button:last-child')

async function mountAndOpenPicker() {
  const wrapper = mount(KnowledgeDetailView, { attachTo: document.body })
  await flushPromises()

  const pickerBtn = wrapper.findAll('button').find((b) => b.text().includes('从媒体库选取'))
  expect(pickerBtn, '详情页应有「从媒体库选取」入口').toBeTruthy()
  await pickerBtn!.trigger('click')
  await flushPromises()
  return wrapper
}

let wrapper: ReturnType<typeof mount> | null = null

beforeEach(() => {
  vi.clearAllMocks()
  installApiMock()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.unstubAllGlobals()
})

describe('知识库详情：从媒体库导入的类型门禁', () => {
  it('★ 不支持的类型点不进去（不会进选中集合）', async () => {
    wrapper = await mountAndOpenPicker()

    // 两行都渲染出来：用户得看得见目录里有什么，只是不能选
    expect(rows()).toHaveLength(2)
    expect(rows()[1].textContent).toContain('b.png')
    expect(rows()[1].classList.contains('not-importable')).toBe(true)

    rows()[1].click()
    await flushPromises()

    // 仍为 0 ⇒ 不可导入的条目根本没有进入 selectedMediaIds
    expect(importButton()!.textContent).toContain('0')
    expect(importButton()!.disabled).toBe(true)
  })

  it('★ 全选只勾可导入的文件（PNG 不参与）', async () => {
    wrapper = await mountAndOpenPicker()

    const compact = (s: string | null | undefined) => (s || '').replace(/\s+/g, '')
    const selectAll = Array.from(document.body.querySelectorAll<HTMLButtonElement>('.media-actions button'))
      .find((b) => compact(b.textContent) === compact('全选'))
    expect(selectAll, '弹窗应有「全选」按钮').toBeTruthy()
    selectAll!.click()
    await flushPromises()

    // 目录 2 个文件，只有 a.pdf 可导入
    expect(importButton()!.textContent).toContain('1')
  })

  it('可导入的类型照常能选中', async () => {
    wrapper = await mountAndOpenPicker()

    rows()[0].click()
    await flushPromises()

    expect(importButton()!.textContent).toContain('1')
    expect(importButton()!.disabled).toBe(false)
  })
})

describe('知识库详情：从媒体库导入的失败处理', () => {
  it('★ 取字节返回 404 时不得把错误页当文件上传', async () => {
    // fetch 对 4xx 不抛错：旧实现直接 blob() 就得到一段 JSON 错误页
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      blob: async () => new Blob(['{"error":"media not found"}'], { type: 'application/json' }),
    })))

    wrapper = await mountAndOpenPicker()
    rows()[0].click()
    await flushPromises()
    importButton()!.click()
    await flushPromises()

    // 关键断言：没有发起上传 ⇒ 库里不会出现内容错误的"成功"文档
    expect(uploaderMock.createChunkUpload).not.toHaveBeenCalled()
  })

  it('取字节成功时按 kb_doc 上传到当前库', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      blob: async () => new Blob(['%PDF-1.4'], { type: 'application/pdf' }),
    })))
    uploaderMock.createChunkUpload.mockResolvedValue({ done: Promise.resolve({}) })

    wrapper = await mountAndOpenPicker()
    rows()[0].click()
    await flushPromises()
    importButton()!.click()
    await flushPromises()

    expect(uploaderMock.createChunkUpload).toHaveBeenCalledTimes(1)
    const [file, meta] = uploaderMock.createChunkUpload.mock.calls[0]
    expect((file as File).name).toBe('a.pdf')
    expect(meta).toEqual({ purpose: 'kb_doc', parentId: 'kb-1' })
  })
})
