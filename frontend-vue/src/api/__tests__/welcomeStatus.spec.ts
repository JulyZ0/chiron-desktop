import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 首次运行引导的状态层（P1-10）。
 *
 * 这里钉住的都是"错了会让用户永远进不去 / 永远被弹出来"的判定：
 *  · 进度按 userId 隔离 —— 混用会让第二个账号继承"已看过引导"；
 *  · 四步全记录（完成或跳过）才算 done —— 跳过也是走完，否则跳过的人会被反复弹回；
 *  · 已有模型 / 探测失败都不自动跳 —— 不打扰老用户，也不在网关抖动时把人从首页拽走。
 */

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(),
  getIsolationStatus: vi.fn(),
  fetchComponentsHealth: vi.fn(),
  listKnowledgeBases: vi.fn(),
}))

vi.mock('../index', () => ({
  listModels: () => mocks.listModels(),
  getIsolationStatus: () => mocks.getIsolationStatus(),
}))

vi.mock('../systemHealth', async importOriginal => {
  const actual = await importOriginal<typeof import('../systemHealth')>()
  return { ...actual, fetchComponentsHealth: () => mocks.fetchComponentsHealth() }
})

vi.mock('../knowledge', () => ({
  listKnowledgeBases: () => mocks.listKnowledgeBases(),
}))

import {
  WELCOME_STEPS,
  isWelcomeDone,
  loadWelcomeProgress,
  markWelcomeStep,
  probeCapabilities,
  probeConnectivity,
  resetWelcomeProgress,
  setWelcomeStep,
  shouldAutoEnterWelcome,
  invalidateWelcomeModelCache,
  welcomeComponentRows,
  welcomeConnectivityOk,
  welcomeStorageKey,
} from '../welcomeStatus'

function healthyComponents() {
  return {
    postgres: { available: true },
    redis: { available: true, ping_ok: true },
    engine: { healthy: true },
    gateway: { available: true },
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  invalidateWelcomeModelCache()
})

describe('引导进度（localStorage，按 userId 隔离）', () => {
  it('存储键带 userId；不同账号互不影响', () => {
    expect(welcomeStorageKey('u1')).not.toBe(welcomeStorageKey('u2'))
    expect(welcomeStorageKey('')).toBe(welcomeStorageKey(null))

    markWelcomeStep('u1', 'models', 'completed')
    expect(loadWelcomeProgress('u1').steps.models).toBe('completed')
    expect(loadWelcomeProgress('u2').steps.models).toBeUndefined()
    expect(loadWelcomeProgress('u2').done).toBe(false)
  })

  it('逐步标记完成：最后一步后 done，且中途不算完成', () => {
    let progress = loadWelcomeProgress('u1')
    expect(progress.step).toBe('models')
    expect(progress.done).toBe(false)

    for (const step of WELCOME_STEPS) {
      progress = markWelcomeStep('u1', step, 'completed')
      expect(progress.steps[step]).toBe('completed')
    }
    expect(progress.done).toBe(true)
    expect(isWelcomeDone('u1')).toBe(true)
  })

  it('跳过也算走完（否则跳过的人会被永远弹回引导）', () => {
    let progress = loadWelcomeProgress('u1')
    for (const step of WELCOME_STEPS) {
      progress = markWelcomeStep('u1', step, 'skipped')
    }
    expect(progress.done).toBe(true)
    // 跳过与完成要能区分开：完成率统计不能把跳过算成完成
    expect(progress.steps.firstMessage).toBe('skipped')
    expect(progress.steps.models).toBe('skipped')
  })

  it('"上一步"只移动当前步，不清已完成记录', () => {
    markWelcomeStep('u1', 'models', 'completed')
    const progress = setWelcomeStep('u1', 'models')
    expect(progress.step).toBe('models')
    expect(progress.steps.models).toBe('completed')
    expect(progress.done).toBe(false)
  })

  it('重新运行：清空后回到第一步', () => {
    for (const step of WELCOME_STEPS) markWelcomeStep('u1', step, 'completed')
    expect(isWelcomeDone('u1')).toBe(true)

    const progress = resetWelcomeProgress('u1')
    expect(progress.done).toBe(false)
    expect(progress.step).toBe('models')
    expect(progress.steps).toEqual({})
    expect(isWelcomeDone('u1')).toBe(false)
  })

  it('存储被手改成坏数据时退化为空进度而不是抛错', () => {
    localStorage.setItem(welcomeStorageKey('u1'), '{not json')
    const progress = loadWelcomeProgress('u1')
    expect(progress.done).toBe(false)
    expect(progress.step).toBe('models')
  })
})

describe('shouldAutoEnterWelcome（自动跳转判定）', () => {
  it('未登录不跳', async () => {
    await expect(shouldAutoEnterWelcome('')).resolves.toBe(false)
    await expect(shouldAutoEnterWelcome(null)).resolves.toBe(false)
    expect(mocks.listModels).not.toHaveBeenCalled()
  })

  it('已完成引导不跳（哪怕当前没有模型）', async () => {
    for (const step of WELCOME_STEPS) markWelcomeStep('u1', step, 'completed')
    mocks.listModels.mockResolvedValue([])
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(false)
    expect(mocks.listModels).not.toHaveBeenCalled()
  })

  it('已有模型不打扰', async () => {
    mocks.listModels.mockResolvedValue([{ provider: 'p', name: 'm', display_name: 'M', context_window: 1000 }])
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(false)
  })

  it('无模型且未完成 → 进入引导', async () => {
    mocks.listModels.mockResolvedValue([])
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(true)
  })

  it('探测失败不跳（网关抖动不该把人从首页拽走）', async () => {
    mocks.listModels.mockRejectedValue(new Error('network down'))
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(false)
  })

  it('30s 内复用模型结果，不重复打 /v1/models', async () => {
    mocks.listModels.mockResolvedValue([])
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(true)
    await expect(shouldAutoEnterWelcome('u1')).resolves.toBe(true)
    expect(mocks.listModels).toHaveBeenCalledTimes(1)
  })
})

describe('探测：连通性', () => {
  it('必需组件全部明确健康才算通过', () => {
    expect(welcomeConnectivityOk(healthyComponents())).toBe(true)
    // ping_ok=false 比 available=true 更严格
    expect(
      welcomeConnectivityOk({ ...healthyComponents(), redis: { available: true, ping_ok: false } }),
    ).toBe(false)
    expect(welcomeConnectivityOk({ postgres: { available: true } })).toBe(false)
    expect(welcomeConnectivityOk(null)).toBe(false)
  })

  it('gateway 未上报也允许通过（能拿到健康态就说明网关活着）', () => {
    const health = { postgres: { available: true }, redis: { available: true }, engine: { healthy: true } }
    expect(welcomeConnectivityOk(health)).toBe(true)
    const rows = welcomeComponentRows(health)
    expect(rows.find(r => r.key === 'gateway')?.up).toBeNull()
    expect(rows.filter(r => r.up === true)).toHaveLength(3)
  })

  it('网关不可达时 ok=false 且 rows 全为未上报', async () => {
    mocks.fetchComponentsHealth.mockResolvedValue(null)
    const probe = await probeConnectivity()
    expect(probe.ok).toBe(false)
    expect(probe.allUp).toBe(false)
    expect(probe.rows.every(r => r.up === null)).toBe(true)
  })
})

describe('探测：可选能力', () => {
  it('只有 ready 且未被停用的知识库才算就绪；隔离档位只读读取', async () => {
    mocks.listKnowledgeBases.mockResolvedValue([
      { id: 'kb1', status: 'ready', enabled: true },
      { id: 'kb2', status: 'ready', enabled: false },
      { id: 'kb3', status: 'indexing', enabled: true },
    ])
    mocks.getIsolationStatus.mockResolvedValue({ effective: 'job', below_floor: false, reasons: [] })

    const probe = await probeCapabilities()
    expect(probe.knowledge).toMatchObject({ ok: true, total: 3, ready: 1, indexing: 1 })
    expect(probe.isolation).toMatchObject({ ok: true, effective: 'job', belowFloor: false })
  })

  it('接口失败不抛错（这一步本来就是可跳过的）', async () => {
    mocks.listKnowledgeBases.mockRejectedValue(new Error('500'))
    mocks.getIsolationStatus.mockRejectedValue(new Error('403'))
    const probe = await probeCapabilities()
    expect(probe.knowledge.ok).toBe(false)
    expect(probe.isolation.ok).toBe(false)
    expect(probe.isolation.effective).toBeNull()
  })
})
