import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// G17：KB 文档预览按 offset 分页。
// 修前会怎样：组件一次 `GET …/preview` 拿整块 content，引擎侧硬截在 1 万字符 ——
// 第 10000 个字符之后的内容在界面上**没有任何入口**；"加载更多""换文档重置"
// 这些行为连断言的地方都没有。
//
// 断言口径与 `KBDocumentPreview.truncation.spec.ts` 一致：Modal 的内容经 teleport
// 渲染到 body，所以走 `document.body.querySelector`；文案是 i18n 的，不按文案断言，
// 只按类名与"内容长度"断言。

const get = vi.fn()

vi.mock('../../../src/api', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string, params?: Record<string, unknown>) =>
      params?.n === undefined ? key : `${key}:${params.n}`,
  }),
}))

import KBDocumentPreview from '../KBDocumentPreview.vue'

const baseDoc = {
  id: 'd1',
  name: 'policy.txt',
  file_type: 'txt',
  file_size_bytes: 900_000,
  page_count: 3,
  status: 'ready',
  preview_truncated: false,
}

/** 引擎侧单页上限是 5000；前端必须按同一口径请求，否则每页都会比预期短一截 */
const PAGE = 5000

function page(content: string, extra: Record<string, unknown> = {}) {
  return { data: { data: { ...baseDoc, content, ...extra } } }
}

/** 分页响应：offset 表示这一页从哪开始 */
function pageAt(full: string, offset: number, limit = PAGE) {
  const chunk = full.slice(offset, offset + limit)
  const nextOffset = offset + chunk.length
  return page(chunk, {
    offset,
    next_offset: nextOffset,
    eof: nextOffset >= full.length,
    total_chars: full.length,
  })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

async function mountPanel(documentId = 'd1') {
  const wrapper = mount(KBDocumentPreview, {
    props: { visible: true, kbId: 'kb1', documentId },
    attachTo: document.body,
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

function bodyText(): string {
  return document.body.querySelector('.content-body')?.textContent ?? ''
}

function moreButton(): HTMLButtonElement | null {
  return document.body.querySelector<HTMLButtonElement>('.content-more-btn')
}

/** 第 n 次请求（0 起）带的查询参数 */
function requestParams(index: number): Record<string, unknown> {
  return (get.mock.calls[index]?.[1] as { params?: Record<string, unknown> } | undefined)?.params ?? {}
}

describe('KBDocumentPreview 预览分页', () => {
  beforeEach(() => {
    get.mockReset()
    document.body.innerHTML = ''
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('点「加载更多」把下一页**追加**在已读内容后面（不重头渲染）', async () => {
    const full = 'A'.repeat(PAGE) + 'B'.repeat(PAGE)
    get.mockResolvedValueOnce(pageAt(full, 0))
    const wrapper = await mountPanel()

    // 第一页：只请求第一段，按钮存在（eof=false）
    expect(requestParams(0).offset).toBe(0)
    expect(requestParams(0).limit).toBe(PAGE)
    expect(bodyText()).toHaveLength(PAGE)
    const btn = moreButton()
    expect(btn, '还有下一页时必须给出「加载更多」入口').not.toBeNull()

    // 第二页：慢一点返回，期间已加载内容**不能**消失（累积渲染、不闪烁）
    const slow = deferred<unknown>()
    get.mockReturnValueOnce(slow.promise)
    btn?.click()
    await flushPromises()
    expect(bodyText(), '加载下一页的等待期间，已读内容不能被清空').toHaveLength(PAGE)

    slow.resolve(pageAt(full, PAGE))
    await flushPromises()

    expect(requestParams(1).offset).toBe(PAGE)
    expect(bodyText()).toBe(full)
    expect(moreButton(), 'eof=true 时不该再提示还有下一页').toBeNull()
    wrapper.unmount()
  })

  it('换 documentId 时重置到第一页（不把上一篇的内容带过来）', async () => {
    get.mockResolvedValueOnce(pageAt('A'.repeat(PAGE) + 'A'.repeat(PAGE), 0))
    const wrapper = await mountPanel('d1')
    expect(bodyText()).toHaveLength(PAGE)

    // 第二篇只有 300 字符：换文档后界面上只能有它，一个 'A' 都不许剩
    const other = 'C'.repeat(300)
    get.mockResolvedValueOnce(pageAt(other, 0))
    await wrapper.setProps({ documentId: 'd2' })
    await flushPromises()
    await flushPromises()

    expect(requestParams(1).offset, '换文档必须从第一页重新请求').toBe(0)
    expect(bodyText()).toBe(other)
    expect(moreButton(), '新文档只有一页时不该出现「加载更多」').toBeNull()
    wrapper.unmount()
  })

  it('加载下一页失败时保留已加载内容，并给出可重试的入口', async () => {
    const full = 'A'.repeat(PAGE) + 'B'.repeat(PAGE)
    get.mockResolvedValueOnce(pageAt(full, 0))
    const wrapper = await mountPanel()

    get.mockRejectedValueOnce({ response: { data: { detail: 'boom' } } })
    moreButton()?.click()
    await flushPromises()

    expect(bodyText(), '一段加载失败不该把用户已经读到的正文也清掉').toHaveLength(PAGE)
    const retry = moreButton()
    expect(retry, '失败后必须留在原地并给出重试入口').not.toBeNull()
    expect(document.body.querySelector('.content-more-error')?.textContent).toContain('boom')

    // 重试成功：从同一 offset 续上，内容拼成全文
    get.mockResolvedValueOnce(pageAt(full, PAGE))
    retry?.click()
    await flushPromises()

    expect(requestParams(2).offset).toBe(PAGE)
    expect(bodyText()).toBe(full)
    expect(document.body.querySelector('.content-more-error')).toBeNull()
    wrapper.unmount()
  })

  it('关闭再打开同一篇文档时从第一页重新开始（分页游标不跨次残留）', async () => {
    const full = 'A'.repeat(PAGE) + 'B'.repeat(PAGE)
    get.mockResolvedValueOnce(pageAt(full, 0))
    get.mockResolvedValueOnce(pageAt(full, PAGE))
    const wrapper = await mountPanel()
    moreButton()?.click()
    await flushPromises()
    expect(bodyText()).toBe(full)

    // 关闭：Modal 的关闭按钮走 close()
    await wrapper.setProps({ visible: false })
    await flushPromises()

    get.mockResolvedValueOnce(pageAt(full, 0))
    await wrapper.setProps({ visible: true })
    await flushPromises()
    await flushPromises()

    const lastParams = requestParams(get.mock.calls.length - 1)
    expect(lastParams.offset, '重新打开必须从 offset=0 起').toBe(0)
    expect(bodyText()).toHaveLength(PAGE)
    wrapper.unmount()
  })

  it('老后端（响应里没有 eof/next_offset）不显示「加载更多」，也不会崩', async () => {
    get.mockResolvedValueOnce(page('legacy content'))
    const wrapper = await mountPanel()

    expect(bodyText()).toBe('legacy content')
    expect(moreButton(), '拿不到分页字段时按"没有下一页"处理').toBeNull()
    wrapper.unmount()
  })
})
