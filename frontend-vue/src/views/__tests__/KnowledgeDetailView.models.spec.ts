import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 知识库详情页的**模型覆盖**与**检索质量**（P1-6）。
 *
 * 两件事都容易做成"看起来做了、实际没生效"：
 *
 * 1. **模型覆盖**：保存时若省略空字段（而不是显式提交空串），用户就永远无法把覆盖
 *    清回默认 —— 后端把"字段缺席"定义为"不改"。这里钉住提交体里**空串也在**。
 * 2. **检索统计**：`available=false`（Redis 读不到）必须显式说明，否则全 0 会被
 *    用户读成"根本没检索过"，进而去删库重建（方向完全错）。
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

const emptyWindow = { queries: 0, hits: 0, zero_hit: 0, errors: 0, avg_latency_ms: 0, zero_hit_rate: 0 }

const KB = {
  id: 'kb-1',
  name: '产品手册',
  provider: 'selfhosted',
  status: 'ready',
  enabled: true,
  document_count: 1,
  index_model: '',
  chat_model: '',
}

const STATS = {
  kb_id: 'kb-1',
  window_hours_24: { ...emptyWindow, queries: 5, hits: 3, zero_hit: 2, errors: 1, avg_latency_ms: 900 },
  window_days_7: { ...emptyWindow, queries: 12, hits: 8, zero_hit: 3, errors: 1, avg_latency_ms: 1200, zero_hit_rate: 0.25 },
  recent_failures: [{ category: 'provider_unavailable', message: '连接被拒绝', count: 1 }],
  diagnosis: ['近 24 小时检索正常（有命中且无报错）'],
  available: true,
}

function installApiMock(stats: unknown = STATS) {
  apiMocks.get.mockImplementation((url: string) => {
    if (url === '/v1/kb/kb-1') return Promise.resolve({ data: { data: KB } })
    if (url === '/v1/kb/kb-1/documents') return Promise.resolve({ data: { data: { documents: [] } } })
    if (url === '/v1/kb/kb-1/status') {
      return Promise.resolve({
        data: {
          data: {
            kb_id: 'kb-1', status: 'ready', percent: 100, active: false,
            total: 0, indexed: 0, failed: 0, pending: 0, indexing: 0,
            error_message: '', documents: [], failed_documents: [],
          },
        },
      })
    }
    if (url === '/v1/kb/kb-1/stats') return Promise.resolve({ data: { data: stats } })
    return Promise.resolve({ data: { data: {} } })
  })
  apiMocks.put.mockResolvedValue({ data: { data: { updated: true } } })
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

describe('知识库详情：检索质量与模型覆盖（P1-6）', () => {
  it('★ 统计卡片展示 24h 数字与失败原因分类', async () => {
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const panel = wrapper.find('[data-testid="kb-stats"]')
    expect(panel.exists()).toBe(true)
    expect(wrapper.find('[data-testid="kb-stats-queries-24h"]').text()).toBe('5')
    expect(panel.text()).toContain('provider_unavailable')
    expect(panel.text()).toContain('连接被拒绝')
    expect(panel.text()).toContain('近 24 小时检索正常')
  })

  it('★ 统计不可读时说明「暂不可读」，不显示成 0', async () => {
    installApiMock({ ...STATS, available: false, window_hours_24: emptyWindow, window_days_7: emptyWindow })
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    expect(wrapper.find('[data-testid="kb-stats"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('检索观测暂不可读')
  })

  it('★ 保存模型时把空串也提交（否则永远无法清回默认）', async () => {
    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const inputs = wrapper.findAll('.kb-model-field input')
    expect(inputs.length).toBe(2)
    await inputs[0].setValue(' idx-model-v2 ')
    // 第二个留空 = 显式清除覆盖
    await wrapper.find('[data-testid="kb-save-models"]').trigger('click')
    await flushPromises()

    expect(apiMocks.put).toHaveBeenCalledWith('/v1/kb/kb-1', {
      index_model: 'idx-model-v2',
      chat_model: '',
    })
  })

  it('回填已保存的覆盖值（刷新后输入框不该是空的）', async () => {
    installApiMock()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/kb/kb-1') {
        return Promise.resolve({ data: { data: { ...KB, index_model: 'idx-a', chat_model: 'chat-b' } } })
      }
      if (url === '/v1/kb/kb-1/documents') return Promise.resolve({ data: { data: { documents: [] } } })
      if (url === '/v1/kb/kb-1/status') {
        return Promise.resolve({
          data: {
            data: {
              kb_id: 'kb-1', status: 'ready', percent: 100, active: false,
              total: 0, indexed: 0, failed: 0, pending: 0, indexing: 0,
              error_message: '', documents: [], failed_documents: [],
            },
          },
        })
      }
      return Promise.resolve({ data: { data: STATS } })
    })

    wrapper = mount(KnowledgeDetailView)
    await flushPromises()

    const inputs = wrapper.findAll('.kb-model-field input')
    expect((inputs[0].element as HTMLInputElement).value).toBe('idx-a')
    expect((inputs[1].element as HTMLInputElement).value).toBe('chat-b')
    // 索引模型非空 ⇒ 提示需要重建索引（否则用户以为保存即生效）
    expect(wrapper.text()).toContain('需要重建索引')
  })
})
