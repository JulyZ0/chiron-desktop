/**
 * 首次运行引导（P1-10）的状态来源与本地进度。
 *
 * 这一层**不新增任何后端端点**，只把四条既有链路收拢成引导需要的形状：
 *   1. 模型   —— `GET /v1/models`（`api/index.ts:listModels`，只含 enabled 行）；
 *   2. 连通性 —— `GET /v1/system/components`（`api/systemHealth.ts`，逐组件健康态）；
 *   3. 可选能力 —— `GET /v1/kb`（`api/knowledge.ts`）+ `GET /v1/system/isolation`（只读展示）；
 *   4. 第一条消息 —— 纯前端跳转 `/chat`，没有探测。
 *
 * 进度存 localStorage 且**按 userId 隔离**：同一台机器换个账号登录，不该继承上一个
 * 账号"已经看过引导"的状态；反之亦然（否则第二个账号会被永久跳过引导）。
 *
 * 为什么不复用 `api/onboarding.ts`：那是**云端**的试用额度引导（`/v1/onboarding`，
 * 步骤是 trial_credit / default_model / first_message），后端在本地模式下没有这套表；
 * 本文件面对的是「新装机第一次能不能用起来」，判定依据全是本机真实状态。
 */
import {
  listModels,
  getIsolationStatus,
  type IsolationLevel,
  type IsolationStatus,
  type LlmModel,
} from './index'
import { fetchComponentsHealth, isComponentUp, type ComponentsHealth } from './systemHealth'
import { listKnowledgeBases } from './knowledge'

// ── 步骤与进度 ───────────────────────────────────────────────────────────────

/** 四个步骤的稳定 id（也是 localStorage 里记录的键，改名会导致老进度被丢弃） */
export type WelcomeStepId = 'models' | 'connectivity' | 'capabilities' | 'firstMessage'

/** 一步的结局：完成或跳过。跳过**不等于**完成，但两者都算"这一步走过了"。 */
export type WelcomeStepStatus = 'completed' | 'skipped'

export const WELCOME_STEPS: readonly WelcomeStepId[] = [
  'models',
  'connectivity',
  'capabilities',
  'firstMessage',
]

export interface WelcomeProgress {
  /** 结构版本：将来若改字段，靠它丢弃老数据而不是渲染出半截状态 */
  version: 1
  /** 当前停留的步骤 */
  step: WelcomeStepId
  /** 已走过（完成或跳过）的步骤 */
  steps: Partial<Record<WelcomeStepId, WelcomeStepStatus>>
  /** 四步都走过了 —— 此后不再自动跳转（"重新运行引导"入口仍可手动进入） */
  done: boolean
  updatedAt: number
}

const STORAGE_PREFIX = 'chiron.welcome.v1.'

/**
 * 存储键按 userId 隔离。
 *
 * 拿不到 userId（未登录 / 身份还没拉回来）时退到 `anonymous`：宁可多存一份，
 * 也不要把"别人的引导已看完"读成自己的 —— 那会让新用户**永远看不到**引导。
 */
export function welcomeStorageKey(userId?: string | null): string {
  const id = (userId ?? '').trim()
  return `${STORAGE_PREFIX}${id || 'anonymous'}`
}

function getStorage(): Storage | null {
  try {
    return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage
  } catch {
    // 隐私模式 / 存储被禁用：引导退化成"本次会话内有效"，不影响功能
    return null
  }
}

export function createEmptyProgress(): WelcomeProgress {
  return { version: 1, step: WELCOME_STEPS[0], steps: {}, done: false, updatedAt: 0 }
}

/**
 * 读进度。
 *
 * 存储里的任何异常形态（手改、旧版本、JSON 坏掉）都退化为空进度而不是抛错 ——
 * 引导页在启动路径上，这里抛错等于白屏。`done` 同时用"四步齐全"兜底判定，
 * 避免只写坏了 `done` 字段就被反复弹回引导。
 */
export function loadWelcomeProgress(userId?: string | null): WelcomeProgress {
  const raw = getStorage()?.getItem(welcomeStorageKey(userId))
  if (!raw) return createEmptyProgress()
  try {
    const parsed = JSON.parse(raw) as Partial<WelcomeProgress> | null
    if (!parsed || typeof parsed !== 'object') return createEmptyProgress()
    const step = WELCOME_STEPS.includes(parsed.step as WelcomeStepId)
      ? (parsed.step as WelcomeStepId)
      : WELCOME_STEPS[0]
    const steps: Partial<Record<WelcomeStepId, WelcomeStepStatus>> = {}
    const rawSteps = parsed.steps
    if (rawSteps && typeof rawSteps === 'object') {
      for (const key of WELCOME_STEPS) {
        const value = (rawSteps as Record<string, unknown>)[key]
        if (value === 'completed' || value === 'skipped') steps[key] = value
      }
    }
    const done = parsed.done === true || WELCOME_STEPS.every(s => !!steps[s])
    return {
      version: 1,
      step,
      steps,
      done,
      updatedAt: typeof parsed.updatedAt === 'number' ? parsed.updatedAt : 0,
    }
  } catch {
    return createEmptyProgress()
  }
}

export function saveWelcomeProgress(
  userId: string | null | undefined,
  progress: WelcomeProgress,
): WelcomeProgress {
  const steps: Partial<Record<WelcomeStepId, WelcomeStepStatus>> = {}
  for (const key of WELCOME_STEPS) {
    const value = progress.steps?.[key]
    if (value === 'completed' || value === 'skipped') steps[key] = value
  }
  const normalized: WelcomeProgress = {
    version: 1,
    step: WELCOME_STEPS.includes(progress.step) ? progress.step : WELCOME_STEPS[0],
    steps,
    done: progress.done === true || WELCOME_STEPS.every(s => !!steps[s]),
    updatedAt: Date.now(),
  }
  try {
    getStorage()?.setItem(welcomeStorageKey(userId), JSON.stringify(normalized))
  } catch {
    /* 存储不可用：本次会话内仍然按内存态推进 */
  }
  return normalized
}

/**
 * 标记一步完成/跳过，并前进到下一步。
 *
 * 最后一步标记后 `done` 自动为 true；中途跳过的步骤**保留**在 `steps` 里，
 * 这样用户点「上一步」回去仍能看到"这步当时跳过了"，而不是一个空状态。
 */
export function markWelcomeStep(
  userId: string | null | undefined,
  step: WelcomeStepId,
  status: WelcomeStepStatus,
): WelcomeProgress {
  const current = loadWelcomeProgress(userId)
  const steps: Partial<Record<WelcomeStepId, WelcomeStepStatus>> = { ...current.steps, [step]: status }
  const index = WELCOME_STEPS.indexOf(step)
  const nextStep = index >= 0 && index < WELCOME_STEPS.length - 1 ? WELCOME_STEPS[index + 1] : step
  return saveWelcomeProgress(userId, { ...current, steps, step: nextStep, done: false })
}

/** 跳到指定步骤（"上一步" / 步骤导航用），不改动已完成记录。 */
export function setWelcomeStep(
  userId: string | null | undefined,
  step: WelcomeStepId,
): WelcomeProgress {
  const current = loadWelcomeProgress(userId)
  return saveWelcomeProgress(userId, { ...current, step })
}

/** 清空进度（"重新运行引导"用）：下一次进入从第一步开始。 */
export function resetWelcomeProgress(userId?: string | null): WelcomeProgress {
  try {
    getStorage()?.removeItem(welcomeStorageKey(userId))
  } catch {
    /* 同上：清不掉也不该阻断重新运行 */
  }
  return createEmptyProgress()
}

export function isWelcomeDone(userId?: string | null): boolean {
  return loadWelcomeProgress(userId).done
}

// ── 探测：模型 ───────────────────────────────────────────────────────────────

export interface ModelsProbe {
  /** 请求是否成功（失败 ≠ 没有模型：前者要提示"检查网关"，后者要提示"去配置"） */
  ok: boolean
  models: LlmModel[]
  error?: string
}

/**
 * 模型列表缓存。
 *
 * 路由守卫要在每次进入首页时判断"有没有模型"，每次真打一次 `/v1/models` 会给
 * 启动路径加一次往返。缓存 30s：足够覆盖启动时的连续导航，又不会让"刚配好模型
 * 回到首页"在长时间里被旧结果弹回引导。
 */
const MODELS_CACHE_TTL_MS = 30_000
let modelsCache: { count: number; at: number } | null = null

export function invalidateWelcomeModelCache(): void {
  modelsCache = null
}

function errorText(err: unknown): string {
  const e = err as { response?: { data?: { error?: unknown } }; message?: string }
  const server = e?.response?.data?.error
  if (typeof server === 'string' && server) return server
  return typeof e?.message === 'string' ? e.message : ''
}

export async function probeModels(): Promise<ModelsProbe> {
  try {
    const models = await listModels()
    modelsCache = { count: models.length, at: Date.now() }
    return { ok: true, models }
  } catch (err) {
    return { ok: false, models: [], error: errorText(err) }
  }
}

export async function hasUsableModels(now = Date.now()): Promise<boolean> {
  const count = await resolveModelCount(now)
  // 探测失败按"没有可用模型"处理：本函数只回答"现在能不能选模型"，
  // 而"要不要因此打断用户"是 shouldAutoEnterWelcome 的判断，那里对 null 会放行。
  return count !== null && count > 0
}

/**
 * 模型数量：null = **没查到**（接口失败），0 = 查到了但没有模型。
 *
 * 两者必须分开：把失败当 0 会让网关抖动时把老用户从首页拽进引导，
 * 而把 0 当失败会让新装机的人永远看不到引导。
 */
async function resolveModelCount(now: number): Promise<number | null> {
  if (modelsCache && now - modelsCache.at < MODELS_CACHE_TTL_MS) {
    return modelsCache.count
  }
  const probe = await probeModels()
  return probe.ok ? probe.models.length : null
}

/**
 * 是否应该把用户送进引导。
 *
 * 只有**同时**满足三条才进：有登录用户、本账号没走完引导、当前没有任何可用模型。
 * 已有模型的用户一律不打扰（哪怕他从没看过引导页 —— 能对话就说明不缺这一步）。
 * 探测失败也**不**跳：后端/网关抖动时把人从首页拽走，比漏掉一次引导伤害更大。
 */
export async function shouldAutoEnterWelcome(
  userId?: string | null,
  now = Date.now(),
): Promise<boolean> {
  const id = (userId ?? '').trim()
  if (!id) return false
  if (isWelcomeDone(id)) return false
  try {
    return (await resolveModelCount(now)) === 0
  } catch {
    return false
  }
}

/** 只有这两个入口会触发自动跳转 —— 其它路径（/models、/knowledge…）跳过去就不再纠缠。 */
export const WELCOME_ENTRY_PATHS: readonly string[] = ['/', '/chat']

// ── 探测：连通性 ─────────────────────────────────────────────────────────────

export type WelcomeComponentKey = 'postgres' | 'redis' | 'engine' | 'gateway'

/** 展示顺序：与状态栏一致（先存储层，再执行层） */
export const WELCOME_COMPONENT_KEYS: readonly WelcomeComponentKey[] = [
  'postgres',
  'redis',
  'engine',
  'gateway',
]

/**
 * 判定"连通性通过"所必需的组件。
 *
 * `gateway` 不在列：能拿到这份健康态本身就说明网关活着；把它算进必需项，会在
 * 某些构建里（不回报 gateway 字段）让这一步永远无法完成。
 */
export const WELCOME_REQUIRED_COMPONENT_KEYS: readonly WelcomeComponentKey[] = [
  'postgres',
  'redis',
  'engine',
]

export interface WelcomeComponentRow {
  key: WelcomeComponentKey
  /** true 可用 / false 不可用 / null 未上报（缺字段，不是 0） */
  up: boolean | null
}

export function welcomeComponentRows(health: ComponentsHealth | null): WelcomeComponentRow[] {
  return WELCOME_COMPONENT_KEYS.map(key => {
    const component = health?.[key]
    return { key, up: component ? isComponentUp(component) : null }
  })
}

export function welcomeConnectivityOk(health: ComponentsHealth | null): boolean {
  if (!health) return false
  return WELCOME_REQUIRED_COMPONENT_KEYS.every(key => isComponentUp(health[key]))
}

export interface ConnectivityProbe {
  /** 能否采到健康态（false = 网关不可达，与"组件挂了"是两件事） */
  ok: boolean
  /** 必需组件全部明确健康 */
  allUp: boolean
  health: ComponentsHealth | null
  rows: WelcomeComponentRow[]
}

export async function probeConnectivity(): Promise<ConnectivityProbe> {
  const health = await fetchComponentsHealth()
  return {
    ok: health !== null,
    allUp: welcomeConnectivityOk(health),
    health,
    rows: welcomeComponentRows(health),
  }
}

// ── 探测：可选能力（知识库 + 隔离档位）──────────────────────────────────────

export interface KnowledgeProbe {
  ok: boolean
  total: number
  /** `status === 'ready'` 且未被停用的库数 —— 只有这种才真的参与检索 */
  ready: number
  indexing: number
  error?: string
}

export interface IsolationProbe {
  ok: boolean
  effective: IsolationLevel | null
  belowFloor: boolean
  reasons: string[]
  error?: string
}

export interface CapabilitiesProbe {
  knowledge: KnowledgeProbe
  isolation: IsolationProbe
}

async function probeKnowledge(): Promise<KnowledgeProbe> {
  try {
    const bases = await listKnowledgeBases()
    return {
      ok: true,
      total: bases.length,
      ready: bases.filter(kb => kb.status === 'ready' && kb.enabled !== false).length,
      indexing: bases.filter(kb => kb.status === 'indexing').length,
    }
  } catch (err) {
    return { ok: false, total: 0, ready: 0, indexing: 0, error: errorText(err) }
  }
}

async function probeIsolation(): Promise<IsolationProbe> {
  try {
    const status: IsolationStatus = await getIsolationStatus()
    return {
      ok: true,
      effective: status?.effective ?? null,
      belowFloor: status?.below_floor === true,
      reasons: Array.isArray(status?.reasons) ? status.reasons : [],
    }
  } catch (err) {
    return { ok: false, effective: null, belowFloor: false, reasons: [], error: errorText(err) }
  }
}

/** 两步并行：知识库与隔离互不依赖，串行只是白等一个往返。 */
export async function probeCapabilities(): Promise<CapabilitiesProbe> {
  const [knowledge, isolation] = await Promise.all([probeKnowledge(), probeIsolation()])
  return { knowledge, isolation }
}
