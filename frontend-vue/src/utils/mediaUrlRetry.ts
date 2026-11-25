/**
 * 媒体签名 URL 的失败分类与重签策略（PRD 9.10 / H70）。
 *
 * 缺陷形态（旧 `MediaView.vue`）：
 *   1. `failedSignedUrls` 是**永久黑名单**：同一 URL 失败一次以后再也不重签。
 *      于是"切换网络/休眠唤醒/服务端短暂 502"这种一次性失败，会让那张图在本会话里
 *      **永远**不显示 —— 用户唯一的出路是刷新整页。
 *   2. 404（真的没了）与 403（过期）走同一分支：前者被反复重签（白费请求），
 *      后者在重签次数用尽后掉进黑名单，两种结局都是"黑洞"。
 *   3. 没有可观测状态：图片消失，界面没有任何"为什么"。
 *
 * 这里把策略抽成纯状态机，渲染层只负责"分类 → 问策略 → 执行"：
 *   - `expired`：URL 自身的 `exp` 已过，或服务端明确说 `link expired` ⇒ 立刻重签；
 *   - `unauthorized`（401/403）：可能是签名过期/权限变更 ⇒ 重签，但次数有上限；
 *   - `gone`（404/410）：资源真的没了 ⇒ **不再重签**，进入不可用态（可展示占位）；
 *   - `transient`（网络错误/超时/5xx/429）：等一会儿再试，不算永久失败。
 */

export type MediaFailureKind = 'expired' | 'unauthorized' | 'gone' | 'transient' | 'unknown'

export interface MediaFailureInput {
  /** HTTP 状态码；网络错误时为 0/undefined。 */
  status?: number
  /** 响应体片段（服务端会写 `link expired` 之类的说明）。 */
  body?: string
  /** 出错的 URL（可能是签名 URL）。 */
  url?: string
  /** 当前时间（毫秒），测试注入。 */
  now?: number
}

export interface MediaRetryState {
  /** 已重签次数。 */
  attempts: number
  /** 下次允许重试的时间戳（毫秒）；0 表示可以立即。 */
  nextAt: number
  /** 最近一次的分类。 */
  lastKind: MediaFailureKind
}

export type MediaRetryAction =
  | { action: 'resign'; reason: MediaFailureKind }
  | { action: 'wait'; until: number; reason: MediaFailureKind }
  | { action: 'unavailable'; reason: MediaFailureKind }

/** 同一资源最多重签次数（防打爆服务端，也防"永久失败"变成无限循环）。 */
export const MEDIA_RESIGN_LIMIT = 3
/** `transient` 的退避基数：1s、2s、4s（上限 8s）。 */
export const MEDIA_TRANSIENT_BACKOFF_MS = 1000
export const MEDIA_TRANSIENT_BACKOFF_MAX_MS = 8000

/** 解析 URL 上的 `exp`（秒或毫秒都接受）；解析不出返回 null。 */
export function mediaUrlExpiry(url: string): number | null {
  const text = String(url || '')
  if (!text) return null
  let parsed: URL
  try {
    parsed = new URL(text, 'http://placeholder.local')
  } catch {
    return null
  }
  const raw = parsed.searchParams.get('exp') || parsed.searchParams.get('expires')
  if (!raw) return null
  const value = Number(raw)
  if (!Number.isFinite(value) || value <= 0) return null
  // 10 位是秒，13 位是毫秒：两种服务端写法都见过，别把秒当毫秒（会永远"未过期"）。
  return value < 1e12 ? value * 1000 : value
}

/** 给一次失败分类。**顺序重要**：先看明确的资源不存在，再看鉴权与过期。 */
export function classifyMediaFailure(input: MediaFailureInput): MediaFailureKind {
  const now = input.now ?? Date.now()
  const status = Number(input.status || 0)
  const body = String(input.body || '').toLowerCase()

  if (status === 404 || status === 410) return 'gone'
  if (status === 401 || status === 403) {
    return body.includes('expired') ? 'expired' : 'unauthorized'
  }

  // URL 自身的 exp 已过：**不需要**先发一次探测请求就能确定"该换一张"。
  // 顺序很重要 —— 这个判断必须早于下面的 transient 分支，否则网络层的
  // 一次失败会把"明确过期"淹没成"稍后重试"，用户要多等好几轮才看到图。
  const exp = mediaUrlExpiry(String(input.url || ''))
  if (exp !== null && exp <= now) return 'expired'

  if (status === 0 || status === 408 || status === 429 || status >= 500) return 'transient'
  if (body.includes('link expired')) return 'expired'
  return 'unknown'
}

/**
 * 决定下一步。规则：
 *   - `gone` → 不可用（不重签）；
 *   - `expired` → 立刻重签（到期是最确定的"该换一张"信号），但计入 attempts；
 *   - `unauthorized` → 重签，直到上限，之后不可用；
 *   - `transient`/`unknown` → 退避等待后重试；永不进入"永久不可用"。
 */
export function nextMediaAction(
  state: MediaRetryState | undefined,
  kind: MediaFailureKind,
  now = Date.now(),
): MediaRetryAction {
  const attempts = state?.attempts ?? 0

  if (kind === 'gone') return { action: 'unavailable', reason: kind }

  if (kind === 'expired') {
    return attempts >= MEDIA_RESIGN_LIMIT
      ? { action: 'unavailable', reason: kind }
      : { action: 'resign', reason: kind }
  }

  if (kind === 'unauthorized') {
    return attempts >= MEDIA_RESIGN_LIMIT
      ? { action: 'unavailable', reason: kind }
      : { action: 'resign', reason: kind }
  }

  // transient / unknown：退避重试，不算永久失败。
  // 以 now 为基准：连续失败时 nextAt 只会被覆盖成"更晚的那次"，
  // 不会因为"上一次已经排了下一次"而层层叠加（那会让重试间隔失控）。
  const backoff = Math.min(MEDIA_TRANSIENT_BACKOFF_MS * 2 ** attempts, MEDIA_TRANSIENT_BACKOFF_MAX_MS)
  const until = now + backoff
  return { action: 'wait', until, reason: kind }
}

/** 应用一次失败：返回新的状态（纯函数，不修改入参）。 */
export function recordMediaFailure(
  state: MediaRetryState | undefined,
  kind: MediaFailureKind,
  now = Date.now(),
): MediaRetryState {
  const attempts = (state?.attempts ?? 0) + 1
  const next = nextMediaAction(state, kind, now)
  return {
    attempts,
    nextAt: next.action === 'wait' ? next.until : 0,
    lastKind: kind,
  }
}

/** 界面上该显示什么（空串 = 正常渲染）。 */
export function mediaUnavailableLabel(kind: MediaFailureKind | undefined): string {
  switch (kind) {
    case 'gone':
      return '文件已不存在'
    case 'unauthorized':
      return '无权访问该文件'
    case 'expired':
      return '链接已过期'
    case 'transient':
    case 'unknown':
      return '暂时无法加载'
    default:
      return ''
  }
}
