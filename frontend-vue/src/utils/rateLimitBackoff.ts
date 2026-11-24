/**
 * 429 / 503 限流退避（CDS v1/06 §6.7 的 `useRateLimitBackoff`）。
 *
 * 官方公式：`min(max(base, 5000) * 2^(n-1) * (1 ± J), 600000)`，J = 15% 全抖动，
 * 连续 3 次之后不再自动重试、必须交人工 —— 无限自动重试会把账号打进更深的限流。
 *
 * 为什么要有抖动：多个标签页 / 多个客户端同时被限流时，固定延迟会让它们在
 * 同一毫秒一起回来，形成二次冲击。
 *
 * 纯函数放在这里（便于单测），接进 axios 的那一层见 api/index.ts。
 */

/** 基准延迟；低于 5s 的退避在真实的速率窗口面前没有意义。 */
export const BACKOFF_BASE_MS = 5000
/** 封顶 10 分钟：再长就不如让用户自己决定什么时候重试。 */
export const BACKOFF_CAP_MS = 600_000
/** 自动重试上限，超过即交人工。 */
export const BACKOFF_MAX_RETRIES = 3
/** ±15% 全抖动 */
export const BACKOFF_JITTER = 0.15

/** 只有这两个状态码算"限流/暂时不可用"，其它错误走原有处理，绝不能盲目重试。 */
export function isRateLimitStatus(status?: number | null): boolean {
  return status === 429 || status === 503
}

/**
 * 服务端显式说的"能不能再发一次"。只有**真布尔**才算结论 ——
 * 字符串 `'true'`、数字 `1`、`null` 一律视为"没说"，转交给状态码回退逻辑去判，不猜。
 */
export function readRetryable(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

// ── 重放安全性：哪些请求**允许**被自动重发（PRD 9.10 / H66）────────────────
//
// 旧实现把「429/503 或 retryable=true」当成**充分条件**，直接 `api.request(config)`
// 重放原请求。对读接口没问题；对写接口是一场事故：
//   · `POST /submit` 会在响应回来之前就建 session 并启动引擎；
//   · `POST /v1/agents/{id}/run` 同理，一次派发 = 一个 AgentSession + 一份计费；
//   · `POST /v1/media/upload`（multipart）会把文件再传一遍。
// 用户看到的却只是一句"服务繁忙，正在自动重试"。
//
// 所以判据从「状态码」换成「**请求本身**能不能重放」：
//   1. GET / HEAD / OPTIONS —— 语义上可证明幂等，直接放行（读路径行为不变）；
//   2. 其它方法 —— 必须带 `Idempotency-Key`（服务端按键去重，同键重放不产生第二份副作用）；
//   3. multipart / FormData 上传 —— 一律不自动重放：请求体流可能已被消费，
//      而且再传一遍大文件不是"重试"而是"再打一次后端"；
//   4. 调用方显式 `__noRetry` —— 已经知道这次副作用无法去重。

/** 幂等键请求头名（服务端与调用方共用的字面量）。 */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key'

/** 不区分大小写地读一个请求头；兼容 axios 的 AxiosHeaders 与普通对象。 */
export function readHeader(headers: unknown, name: string): string {
  if (!headers || typeof headers !== 'object') return ''
  const target = name.toLowerCase()
  const holder = headers as Record<string, unknown> & {
    get?: (key: string) => unknown
    toJSON?: () => Record<string, unknown>
  }
  if (typeof holder.get === 'function') {
    const direct = holder.get(name)
    if (typeof direct === 'string' && direct.trim()) return direct.trim()
    const lower = holder.get(target)
    if (typeof lower === 'string' && lower.trim()) return lower.trim()
  }
  const flat = typeof holder.toJSON === 'function' ? holder.toJSON() : holder
  for (const [key, value] of Object.entries(flat || {})) {
    if (key.toLowerCase() !== target) continue
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

/** 语义上可证明幂等的方法（重放不会多出一次效果）。 */
export function isIdempotentMethod(method?: string | null): boolean {
  const m = String(method ?? 'get').trim().toLowerCase()
  return m === 'get' || m === 'head' || m === 'options'
}

/** 这次请求体是不是"上传"（multipart / FormData）—— 这类请求体不可重放。 */
export function isUploadPayload(data: unknown, contentType?: unknown): boolean {
  const ct = typeof contentType === 'string' ? contentType.toLowerCase() : ''
  if (ct.includes('multipart/form-data')) return true
  if (typeof FormData !== 'undefined' && data instanceof FormData) return true
  return false
}

/** 重放判定的依据（全部来自 axios 的 `config`，便于纯函数单测）。 */
export interface ReplayInput {
  method?: string | null
  /** `Idempotency-Key` 头的值（没有就是空串） */
  idempotencyKey?: string | null
  /** 请求体是 multipart / FormData */
  isUpload?: boolean
  /** 调用方显式要求不重试 */
  retryDisabled?: boolean
}

export type ReplayReason =
  | 'idempotent_method'
  | 'idempotency_key'
  | 'retry_disabled'
  | 'upload_not_replayable'
  | 'unsafe_method'

export interface ReplayVerdict {
  /** true = 允许自动重放 */
  safe: boolean
  reason: ReplayReason
}

/**
 * 这次失败请求能不能安全地自动重发一次。
 *
 * 顺序即优先级：调用方的显式否决与上传否决**先于**方法/幂等键，
 * 否则一个"带了幂等键的 POST 上传"会被误判成可重放。
 */
export function replayVerdict(input: ReplayInput): ReplayVerdict {
  if (input.retryDisabled) return { safe: false, reason: 'retry_disabled' }
  if (input.isUpload) return { safe: false, reason: 'upload_not_replayable' }
  if (isIdempotentMethod(input.method)) return { safe: true, reason: 'idempotent_method' }
  if (typeof input.idempotencyKey === 'string' && input.idempotencyKey.trim()) {
    return { safe: true, reason: 'idempotency_key' }
  }
  return { safe: false, reason: 'unsafe_method' }
}

/**
 * 重试决策：**服务端契约字段优先，状态码回退**（后向兼容）。
 *
 * 分层依据：`code` 负责"发生了什么"（i18n 与分支），`retryable` 负责"要不要再发一次"。
 * 此前只有状态码可看，于是 409 `session_busy`（等上一轮跑完就能过，重试是**对的策略**）
 * 被当成不可重试而直接甩给用户；反过来 500 里的 provider 暂时性故障又被当成不可重试。
 * 现在服务端说了就听它的；它没说（字段缺失 = 调用方自判）才用旧的 429/503 判断。
 */
export function shouldRetryResponse(status?: number | null, retryable?: boolean): boolean {
  if (retryable !== undefined) return retryable
  return isRateLimitStatus(status)
}

/**
 * 第 `attempt` 次重试前该等多久（attempt 从 1 开始）。
 *
 * @param attempt 已重试次数（1 = 第一次重试）
 * @param random 注入随机源，便于测试断言确定性
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const n = Math.max(1, Math.floor(attempt))
  const raw = Math.max(BACKOFF_BASE_MS, BACKOFF_BASE_MS) * Math.pow(2, n - 1)
  const jitter = 1 + (random() * 2 - 1) * BACKOFF_JITTER
  return Math.min(Math.round(raw * jitter), BACKOFF_CAP_MS)
}

/** 还允许自动重试吗？用 `attempt` 表示"已经重试过几次"。 */
export function canAutoRetry(attempt: number): boolean {
  return attempt < BACKOFF_MAX_RETRIES
}

/**
 * 解析服务端给的 `Retry-After`。**优先于本地退避计算** ——
 * 服务端知道速率窗口什么时候开，本地只能猜。
 *
 * 支持两种格式：秒数（`Retry-After: 30`）与 HTTP 日期（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`）。
 * 解析不出来或不合法时返回 null，交给本地公式。
 */
export function parseRetryAfter(header: unknown, now: number = Date.now()): number | null {
  if (header == null) return null
  const raw = String(header).trim()
  if (!raw) return null

  // 纯数字 = 秒
  if (/^\d+$/.test(raw)) {
    const seconds = Number(raw)
    return Number.isFinite(seconds) ? Math.max(0, seconds * 1000) : null
  }

  // HTTP 日期
  const at = Date.parse(raw)
  if (Number.isNaN(at)) return null
  return Math.max(0, at - now)
}

/**
 * 最终该等多久：Retry-After 优先，否则本地指数退避；
 * 两者都受 10 分钟封顶约束（避免服务端给出荒唐的大值把页面卡死）。
 */
export function resolveWaitMs(
  attempt: number,
  retryAfterHeader?: unknown,
  random: () => number = Math.random,
  now: number = Date.now(),
): number {
  const fromServer = parseRetryAfter(retryAfterHeader, now)
  const wait = fromServer ?? backoffDelay(attempt, random)
  return Math.min(Math.max(0, wait), BACKOFF_CAP_MS)
}

/**
 * 从错误体里取服务端建议的等待时长（秒）：`params.retry_after_seconds`。
 *
 * 这是错误码 + 结构参数的既有用法（`session_busy` / `session_recover` 都已经在传），
 * 与 `Retry-After` 头同义但走 body —— 有些代理会吃掉响应头，body 不会被改。
 */
export function parseRetryAfterParams(body: unknown): number | null {
  const raw = (body as { params?: { retry_after_seconds?: unknown } } | undefined)?.params
    ?.retry_after_seconds
  const seconds = typeof raw === 'string' ? Number(raw) : raw
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return null
  return seconds > 0 ? seconds * 1000 : null
}

/**
 * 重试前实际等待：`Retry-After` 头 > body 的 `params.retry_after_seconds` > 本地退避，
 * 都受 10 分钟封顶。两道服务端口径共存是因为历史路径只写其一（限流中间件写头，
 * `session_busy` 写参数），客户端该两处都认。
 */
export function resolveResponseWaitMs(
  attempt: number,
  retryAfterHeader: unknown,
  body: unknown,
  random: () => number = Math.random,
  now: number = Date.now(),
): number {
  const fromHeader = parseRetryAfter(retryAfterHeader, now)
  const fromBody = fromHeader == null ? parseRetryAfterParams(body) : null
  const wait = fromHeader ?? fromBody ?? backoffDelay(attempt, random)
  return Math.min(Math.max(0, wait), BACKOFF_CAP_MS)
}
