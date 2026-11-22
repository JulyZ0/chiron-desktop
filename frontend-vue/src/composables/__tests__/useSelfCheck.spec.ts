import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 系统自检单例（P1-9）的接线网。
 *
 * 钉住四件**改坏了不会报错、只会静默出错**的事：
 *  ① 非 admin 一个请求都不发（面板隐藏是产品决定，不是把 403 当"自检失败"）；
 *  ② 5 分钟缓存真的生效（三个入口各挂一次不该打三组请求）；
 *  ③ 并发去重（首页抽屉 + 设置页同时挂载只发一组）；
 *  ④ **失败不写缓存时间戳**（否则一次抖动会把"未知"钉在界面上 5 分钟），
 *     且坏掉的一项只影响它自己，别的行照常出状态。
 */

const authState = vi.hoisted(() => ({ isPlatformOwner: true }))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => authState,
}))

const mocks = vi.hoisted(() => ({
  get: vi.fn<(url: string) => Promise<unknown>>(),
  listModels: vi.fn<() => Promise<unknown>>(),
  summary: vi.fn<() => Promise<unknown>>(),
}))

vi.mock('../../api', () => ({
  api: { get: mocks.get },
  listModels: mocks.listModels,
}))

vi.mock('../../api/admin', () => ({
  getDiagnosticsSummary: mocks.summary,
}))

import {
  SELF_CHECK_TTL_MS,
  buildSelfCheckItems,
  resetSelfCheckForTest,
  useSelfCheck,
  type SelfCheckItem,
} from '../useSelfCheck'
import type { DiagSummaryCheck } from '../../api/admin'

/** 五项聚合清单的典型回包（schema/knowledge 各一种问题，覆盖四种状态） */
const CHECKS: DiagSummaryCheck[] = [
  { id: 'schema', title: 'Schema', status: 'ok', detail: 'v 12', fix: '' },
  { id: 'tables', title: '必需表', status: 'warn', detail: '缺 1 张表', fix: '跑迁移' },
  { id: 'knowledge', title: '知识库', status: 'error', detail: '服务未启动', fix: '' },
  { id: 'checkpoints', title: '检查点', status: 'ok', detail: '已保护', fix: '' },
  { id: 'deps', title: '依赖', status: 'unknown', detail: '读不到', fix: '' },
]

const MODELS = [
  { provider: 'p', name: 'm1', display_name: 'M1', context_window: 1000 },
  { provider: 'p', name: 'm2', display_name: 'M2', context_window: 1000 },
]

const COVERAGE = { period_days: 90, total_models: 4, priced_models: 3, unpriced_models: 1 }

function routeAll(overrides: { summary?: 'reject'; models?: 'reject'; coverage?: 'reject' } = {}) {
  mocks.summary.mockImplementation(() =>
    overrides.summary === 'reject'
      ? Promise.reject(new Error('summary down'))
      : Promise.resolve({ generated_at: 1, status: 'warn', counts: {}, checks: CHECKS }),
  )
  mocks.listModels.mockImplementation(() =>
    overrides.models === 'reject' ? Promise.reject(new Error('models down')) : Promise.resolve(MODELS),
  )
  mocks.get.mockImplementation(() =>
    overrides.coverage === 'reject'
      ? Promise.reject(new Error('403'))
      : Promise.resolve({ data: { data: COVERAGE } }),
  )
}

function byKey(items: SelfCheckItem[]) {
  return Object.fromEntries(items.map(i => [i.key, i]))
}

describe('useSelfCheck', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetSelfCheckForTest()
    authState.isPlatformOwner = true
    routeAll()
  })

  it('admin：五项清单 + 模型 + 价格覆盖率共七行，状态与跳转齐全', async () => {
    const selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()

    const keys = selfCheck.items.value.map(i => i.key)
    expect(keys).toEqual(['schema', 'tables', 'knowledge', 'checkpoints', 'deps', 'models', 'pricing'])

    const map = byKey(selfCheck.items.value)
    expect(map.schema.status).toBe('ok')
    expect(map.tables.status).toBe('warn')
    expect(map.knowledge.status).toBe('error')
    expect(map.deps.status).toBe('unknown')
    expect(map.models.status).toBe('ok')
    expect(map.models.extra?.count).toBe(2)
    // 3/4 有价 → 有缺价项 → warn（不是 ok）
    expect(map.pricing.status).toBe('warn')
    expect(map.pricing.extra).toMatchObject({ total: 4, priced: 3, unpriced: 1, days: 90 })

    // 「去修复」落点：只有真的能修的项才有路径
    expect(map.schema.fixPath).toBe('/schema-mismatch')
    expect(map.knowledge.fixPath).toBe('/knowledge/settings')
    expect(map.deps.fixPath).toBe('/admin/deps')
    expect(map.models.fixPath).toBe('/models')
    expect(map.pricing.fixPath).toBe('/billing')
    // tables / checkpoints 没有可操作页面 —— 不出跳转
    expect(map.tables.fixPath).toBeNull()
    expect(map.checkpoints.fixPath).toBeNull()

    expect(selfCheck.error.value).toBe('')
    // 有 error 行 → 整体 error；需要处理的行 = tables(warn) + knowledge(error) + pricing(warn)
    expect(selfCheck.overall.value).toBe('error')
    expect(selfCheck.issueCount.value).toBe(3)
  })

  it('非 admin：不发任何请求，可用性为 false', async () => {
    authState.isPlatformOwner = false
    const selfCheck = useSelfCheck()

    expect(selfCheck.available.value).toBe(false)
    await selfCheck.ensureLoaded()
    await selfCheck.refresh()

    expect(mocks.summary).not.toHaveBeenCalled()
    expect(mocks.listModels).not.toHaveBeenCalled()
    expect(mocks.get).not.toHaveBeenCalled()
    expect(selfCheck.items.value).toEqual([])
    expect(selfCheck.error.value).toBe('')
  })

  it('5 分钟缓存：TTL 内复用，过期后重新拉取', async () => {
    const now = 1_000_000
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(now)

    const selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()
    await selfCheck.ensureLoaded()
    expect(mocks.summary).toHaveBeenCalledTimes(1)

    // TTL 边界内（少 1ms）仍然是缓存
    nowSpy.mockReturnValue(now + SELF_CHECK_TTL_MS - 1)
    await selfCheck.ensureLoaded()
    expect(mocks.summary).toHaveBeenCalledTimes(1)

    // 过期后重取
    nowSpy.mockReturnValue(now + SELF_CHECK_TTL_MS)
    await selfCheck.ensureLoaded()
    expect(mocks.summary).toHaveBeenCalledTimes(2)

    nowSpy.mockRestore()
  })

  it('手动 refresh 无视缓存', async () => {
    const selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()
    expect(mocks.summary).toHaveBeenCalledTimes(1)

    await selfCheck.refresh()
    expect(mocks.summary).toHaveBeenCalledTimes(2)
  })

  it('并发去重：同时挂载的多处只发一组请求（单例）', async () => {
    const a = useSelfCheck()
    const b = useSelfCheck()
    await Promise.all([a.ensureLoaded(), b.ensureLoaded()])

    expect(mocks.summary).toHaveBeenCalledTimes(1)
    expect(mocks.listModels).toHaveBeenCalledTimes(1)
    expect(mocks.get).toHaveBeenCalledTimes(1)
    // 两个入口看到的是同一份读数
    expect(a.items.value).toBe(b.items.value)
  })

  it('清单拉取失败：五项退化为 unknown 并记下原因，但**不缓存**失败', async () => {
    routeAll({ summary: 'reject' })
    const selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()

    const map = byKey(selfCheck.items.value)
    expect(map.schema.status).toBe('unknown')
    expect(map.deps.status).toBe('unknown')
    expect(selfCheck.error.value).toBe('summary down')
    expect(selfCheck.fetchedAt.value).toBe(0)

    // 失败不写时间戳 → 下一次挂载会重试
    await selfCheck.ensureLoaded()
    expect(mocks.summary).toHaveBeenCalledTimes(2)
  })

  it('模型 0 个是 error（明确的问题），读不到是 unknown（看不见）', async () => {
    mocks.listModels.mockResolvedValue([])
    let selfCheck = useSelfCheck()
    await selfCheck.refresh()
    expect(byKey(selfCheck.items.value).models.status).toBe('error')

    resetSelfCheckForTest()
    routeAll({ models: 'reject' })
    selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()
    expect(byKey(selfCheck.items.value).models.status).toBe('unknown')
    expect(selfCheck.error.value).toBe('models down')
  })

  it('价格覆盖率不可读：整项不显示（不渲染成 0%）', async () => {
    routeAll({ coverage: 'reject' })
    const selfCheck = useSelfCheck()
    await selfCheck.ensureLoaded()

    expect(selfCheck.items.value.some(i => i.key === 'pricing')).toBe(false)
    // 其它项照常
    expect(byKey(selfCheck.items.value).schema.status).toBe('ok')
  })

  it('区间内没有可计价模型：价格项是 unknown（没样本 ≠ 全绿）', () => {
    const items = buildSelfCheckItems(CHECKS, { ok: true, count: 1 }, {
      period_days: 90,
      total_models: 0,
      priced_models: 0,
      unpriced_models: 0,
    })
    expect(byKey(items).pricing.status).toBe('unknown')
    expect(byKey(items).pricing.extra?.total).toBe(0)
  })
})
