import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// G6：KB 预览截断必须告知。
// 修前：引擎侧把预览截到 1 万字符并回 `preview_truncated=true`，前端从不读它 ——
// 用户拿"预览里没有"当"文档里没有"（合规/制度类文档尤其危险）。
//
// 注意：组件用 ant-design-vue 的 `Modal`，内容经 teleport 渲染到 body，因此断言走
// `document.body.querySelector(...)`；同时**不用文案断言** —— 文案是 i18n 的，会随语言变。

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
  name: 'policy.pdf',
  file_type: 'txt',
  file_size_bytes: 900_000,
  page_count: 3,
}

function mockPreview(extra: Record<string, unknown>) {
  get.mockResolvedValue({ data: { data: { ...baseDoc, ...extra } } })
}

async function mountPanel() {
  const wrapper = mount(KBDocumentPreview, {
    props: { visible: true, kbId: 'kb1', documentId: 'd1' },
    attachTo: document.body,
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

describe('KBDocumentPreview 截断提示', () => {
  beforeEach(() => {
    get.mockReset()
    document.body.innerHTML = ''
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('引擎说截断了，界面必须说出来', async () => {
    mockPreview({ content: 'x'.repeat(10000), preview_truncated: true })
    const wrapper = await mountPanel()
    const notice = document.body.querySelector('.content-truncated')
    expect(notice, '截断时必须渲染提示条').not.toBeNull()
    expect(notice?.textContent?.length ?? 0).toBeGreaterThan(0)
    wrapper.unmount()
  })

  it('没截断时不出提示（避免狼来了）', async () => {
    mockPreview({ content: 'short', preview_truncated: false })
    const wrapper = await mountPanel()
    expect(document.body.querySelector('.content-truncated')).toBeNull()
    wrapper.unmount()
  })

  it('字段缺失时按"未截断"处理但不崩', async () => {
    mockPreview({ content: 'short' })
    const wrapper = await mountPanel()
    expect(document.body.querySelector('.content-truncated')).toBeNull()
    wrapper.unmount()
  })
})
