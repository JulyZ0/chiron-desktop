import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 知识库详情页「从媒体库选取」弹窗的**目录导航**（2026-11-17）。
 *
 * 钉住的是两个"点下去看起来没反应"的缺陷：
 *
 * 1. **文件夹曾被当成可选条目**。媒体库把文件夹与文件**同列返回**（`type === 'folder'`），
 *    旧弹窗把它们与文件一样渲染成可勾选的行；`importFromMedia` 取 `file_url` 时取不到、
 *    静默 `continue`，用户看到的是"成功导入 0 个文件"，没有任何解释。
 * 2. **选中 id 只在当前目录里找得回条目**。原先按 id 到 `mediaFiles`（当前目录）里
 *    `find`，一旦跨目录选择，另一目录的文件就凭空消失。
 *
 * 这两条都不报错、只是少做一点事 —— 正是最容易在重构里悄悄回归的那类行为。
 *
 * ⚠ 断言查的是 `document.body` 而不是 `wrapper`：antd 的 Modal 经 Teleport 挂到 body，
 * `wrapper.findAll('.media-item')` 会**恒为 0**（写这个 spec 时第一版就这么假红过）。
 */

// antd 的 Table / Modal 走 responsiveObserve，jsdom 没有 matchMedia。
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

vi.mock('../../api', () => ({
  api: {
    get: apiMocks.get,
    post: apiMocks.post,
    put: apiMocks.put,
    delete: apiMocks.delete,
  },
  resolveMediaUrl: apiMocks.resolveMediaUrl,
}))

vi.mock('../../utils/uploader', () => ({ createChunkUpload: vi.fn() }))

// 路由：详情页从 `route.params.id` 取库 id，失败分支会 `router.push('/knowledge')`
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'kb-1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

import KnowledgeDetailView from '../KnowledgeDetailView.vue'

const KB = {
  id: 'kb-1',
  name: '产品手册',
  provider: 'selfhosted',
  status: 'ready',
  enabled: true,
  document_count: 0,
}

const folder = { id: 'f1', type: 'folder', name: '归档', size: 0, file_url: '' }
const rootFile = { id: 'm1', type: 'file', name: 'a.pdf', size: 100, file_url: '/media/a.pdf' }
const nestedFile = { id: 'm2', type: 'file', name: 'b.pdf', size: 200, file_url: '/media/b.pdf' }

/** 按 `parent_id` 返回不同目录的内容，模拟服务端的目录过滤。 */
function installApiMock() {
  apiMocks.get.mockImplementation((url: string, config?: { params?: { parent_id?: string } }) => {
    if (url === '/v1/kb/kb-1') return Promise.resolve({ data: { data: KB } })
    if (url === '/v1/kb/kb-1/documents') return Promise.resolve({ data: { data: { documents: [] } } })
    if (url === '/v1/media') {
      const parent = config?.params?.parent_id ?? ''
      const items = parent === '' ? [folder, rootFile] : [nestedFile]
      return Promise.resolve({ data: { data: { items, total: items.length } } })
    }
    return Promise.resolve({ data: { data: {} } })
  })
}

// —— Modal 渲染在 body 里，用原生查询更直观 ——
const rows = (): HTMLElement[] => Array.from(document.body.querySelectorAll<HTMLElement>('.media-item'))
const crumbs = (): HTMLElement[] => Array.from(document.body.querySelectorAll<HTMLElement>('.media-breadcrumb .crumb'))
const importButton = (): HTMLButtonElement | null =>
  document.body.querySelector<HTMLButtonElement>('.modal-footer button:last-child')

async function mountAndOpenPicker() {
  const wrapper = mount(KnowledgeDetailView, { attachTo: document.body })
  await flushPromises()

  const pickerBtn = wrapper
    .findAll('button')
    .find((b) => b.text().includes('从媒体库选取'))
  expect(pickerBtn, '详情页应有「从媒体库选取」入口').toBeTruthy()
  await pickerBtn!.trigger('click')
  await flushPromises()

  return wrapper
}

/** 媒体列表的调用参数（含 parent_id）。 */
function mediaCalls() {
  return apiMocks.get.mock.calls
    .filter((c) => c[0] === '/v1/media')
    .map((c) => (c[1] as { params?: { parent_id?: string } })?.params)
}

let wrapper: ReturnType<typeof mount> | null = null

beforeEach(() => {
  vi.clearAllMocks()
  installApiMock()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('知识库详情：媒体库选取弹窗的目录导航', () => {
  it('打开弹窗时按根目录（parent_id 为空串）拉取', async () => {
    wrapper = await mountAndOpenPicker()

    expect(mediaCalls()).toEqual([expect.objectContaining({ parent_id: '' })])
  })

  it('★ 点文件夹是「进入目录」，不是「选中它」', async () => {
    wrapper = await mountAndOpenPicker()

    expect(rows()).toHaveLength(2)
    expect(rows()[0].textContent).toContain('归档')

    rows()[0].click()
    await flushPromises()

    // 进入目录 ⇒ 用该文件夹 id 重新拉取
    expect(mediaCalls().at(-1)).toEqual(expect.objectContaining({ parent_id: 'f1' }))
    // 且新目录的内容渲染出来了
    expect(rows()[0].textContent).toContain('b.pdf')

    // 文件夹**没有**被计入选中：它不是可导入对象，计入会让「全选」永远差一截
    expect(importButton()!.textContent).toContain('0')
    expect(importButton()!.disabled).toBe(true)
  })

  it('★ 跨目录选择的文件在下一次选择时仍保留（选中不随目录切换丢失）', async () => {
    wrapper = await mountAndOpenPicker()

    // 根目录选 a.pdf
    rows()[1].click()
    await flushPromises()
    expect(importButton()!.textContent).toContain('1')

    // 进入 f1，再选 b.pdf
    rows()[0].click()
    await flushPromises()
    rows()[0].click()
    await flushPromises()

    // 两次选择都保留：导入 2 个（旧实现按当前目录 find，另一个目录的会落空）
    expect(importButton()!.textContent).toContain('2')
    expect(importButton()!.disabled).toBe(false)
  })

  it('面包屑能回到根目录（parent_id 回到空串）', async () => {
    wrapper = await mountAndOpenPicker()

    rows()[0].click()                       // 进入 归档
    await flushPromises()
    expect(crumbs()).toHaveLength(2)

    crumbs()[0].click()
    await flushPromises()

    expect(mediaCalls().at(-1)).toEqual(expect.objectContaining({ parent_id: '' }))
    expect(crumbs()).toHaveLength(1)
    expect(rows()[0].textContent).toContain('归档')
  })

  it('全选只勾文件，不把文件夹算进去', async () => {
    wrapper = await mountAndOpenPicker()

    // antd 会在两个汉字的按钮文案里插空格（渲染成「全 选」），所以比较前先去掉空白
    const compact = (s: string | null | undefined) => (s || '').replace(/\s+/g, '')
    const selectAll = Array.from(document.body.querySelectorAll<HTMLButtonElement>('.media-actions button'))
      .find((b) => compact(b.textContent) === compact('全选'))
    expect(selectAll, '弹窗应有「全选」按钮').toBeTruthy()
    selectAll!.click()
    await flushPromises()

    // 根目录共 2 项：1 个文件夹 + 1 个文件 ⇒ 只应选中 1 个
    expect(importButton()!.textContent).toContain('1')
  })
})
