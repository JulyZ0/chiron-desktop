import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { Modal } from 'ant-design-vue'

/**
 * 知识库详情页的**索引进度闭环**（P1-8）。
 *
 * 钉住三件"看起来做了、实际会误导用户"的事：
 *
 * 1. 整体进度用服务端聚合的 `ready/total`（前端不算），失败文档逐条列出原因；
 * 2. 「重试本文件」走 `scope='doc'`（只重跑这一篇），而不是悄悄重跑整库；
 * 3. 「重建全部索引」必须先确认，且确认文案写明**会消耗模型额度** ——
 *    `scope=all` 是整库重新提交给 PageIndex，点一下就烧一遍索引费用。
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
  resolveMediaUrl: vi.fn(async () => ''),
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

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'kb-1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

import KnowledgeDetailView from '../KnowledgeDetailView.vue'

const KB = {
  id: 'kb-1',
  name: '产品手册',
  provider: 'selfhosted',
  status: 'error',
  enabled: true,
  document_count: 3,
  index_model: '',
  chat_model: '',
}

const FAILED_DOC = {
  id: 'doc-9',
  name: '坏文件.pdf',
  status: 'error',
  page_count: 0,
  error_message: 'PDF 解析失败',
  file_size_bytes: 1024,
}

const INDEX_STATUS = {
  kb_id: 'kb-1',
  status: 'error',
  percent: 100,
  active: false,
  total: 3,
  indexed: 2,
  failed: 1,
  pending: 0,
  indexing: 0,
  error_message: 'PDF 解析失败',
  documents: [FAILED_DOC],
  failed_documents: [FAILED_DOC],
}

const EMPTY_WINDOW = { queries: 0, hits: 0, zero_hit: 0, errors: 0, avg_latency_ms: 0, zero_hit_rate: 0 }

function installApiMock(overrides: { status?: Record<string, unknown> } = {}) {
  const status = { ...INDEX_STATUS, ...(overrides.status ?? {}) }
  apiMocks.get.mockImplementation((url: string) => {
    if (url === '/v1/kb/kb-1') return Promise.resolve({ data: { data: KB } })
    if (url === '/v1/kb/kb-1/documents') return Promise.resolve({ data: { data: { documents: [] } } })
    if (url === '/v1/kb/kb-1/status') return Promise.resolve({ data: { data: status } })
    if (url === '/v1/kb/kb-1/stats') {
      return Promise.resolve({
        data: { data: { kb_id: 'kb-1', window_hours_24: EMPTY_WINDOW, window_days_7: EMPTY_WINDOW, recent_failures: [], diagnosis: [], available: true } },
      })
    }
    return Promise.resolve({ data: { data: {} } })
  })
  apiMocks.post.mockResolvedValue({
    data: { data: { status: 'indexing', enqueued: true, scope: 'doc', count: 1, task_id: 't1' } },
  })
}

let wrapper: ReturnType<typeof mount> | null = null

beforeEach(() => {
  vi.clearAllMocks()
  installApiMock()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
})

describe('知识库详情：索引进度与重建', () => {
  it('★ 进度卡片显示服务端聚合的 ready/total 与失败原因', async () => {
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const card = wrapper.find('[data-testid="kb-index-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('2/3')
    expect(card.text()).toContain('坏文件.pdf')
    expect(card.text()).toContain('PDF 解析失败')
  })

  it('★ 「重试本文件」只重跑这一篇（scope=doc + doc_id），不重跑整库', async () => {
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    await wrapper.find('[data-testid="retry-document"]').trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/v1/kb/kb-1/reindex', {
      scope: 'doc',
      doc_id: 'doc-9',
    })
  })

  it('★ 「重建全部索引」先弹确认，文案写明消耗模型额度；确认后才提交 scope=all', async () => {
    const confirmSpy = vi
      .spyOn(Modal, 'confirm')
      .mockReturnValue({ destroy: vi.fn(), update: vi.fn() } as never)

    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    await wrapper.find('[data-testid="kb-reindex-all"]').trigger('click')
    await flushPromises()

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    // 只弹窗、不提交：确认之前一次重建都不该发生
    expect(apiMocks.post).not.toHaveBeenCalled()

    const options = confirmSpy.mock.calls[0][0] as unknown as {
      content: string
      onOk: () => Promise<void> | void
    }
    expect(String(options.content)).toContain('模型额度')
    expect(String(options.content)).toContain('3')

    await options.onOk()
    await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith('/v1/kb/kb-1/reindex', { scope: 'all' })

    confirmSpy.mockRestore()
  })

  it('★ 库空闲时不轮询状态（只有 active 才继续拉接口）', async () => {
    vi.useFakeTimers()
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const statusCalls = () => apiMocks.get.mock.calls.filter(([url]) => url === '/v1/kb/kb-1/status').length
    expect(statusCalls()).toBe(1)

    await vi.advanceTimersByTimeAsync(10_000)
    // 终态（active=false）：一次都不该再拉
    expect(statusCalls()).toBe(1)
  })

  it('索引进行中时才轮询', async () => {
    vi.useFakeTimers()
    installApiMock({ status: { status: 'indexing', active: true, failed: 0, indexed: 1, failed_documents: [] } })
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const statusCalls = () => apiMocks.get.mock.calls.filter(([url]) => url === '/v1/kb/kb-1/status').length
    expect(statusCalls()).toBe(1)

    await vi.advanceTimersByTimeAsync(3_100)
    expect(statusCalls()).toBe(2)
  })
})
