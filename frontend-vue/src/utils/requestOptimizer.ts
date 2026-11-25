import axios from 'axios'
import type { AxiosRequestConfig, Canceler, AxiosInstance, CancelToken } from 'axios'

/**
 * 请求优化管理器
 * 
 * 功能:
 * 1. 路由切换时自动取消未完成的请求
 * 2. 相同请求短时间内的去重 (请求合并)
 * 3. 弱网模式检测
 */

declare module 'axios' {
  export interface AxiosRequestConfig {
    /**
     * 置 true 时 `cancelAll()` 不取消它（见 `cancelAll` 的注释）。
     *
     * 只给"本身就是会话/身份"的请求用（`POST /v1/auth/session`）：
     * 它的结果与页面无关，被导航取消等于把登录态丢掉。
     */
    __retainAcrossNavigation?: boolean
    /** 由请求管理器写入；同 URL 的并发请求也各自拥有独立身份。 */
    __requestId?: string
  }
}

interface PendingRequest {
  cancel: Canceler
  config: AxiosRequestConfig
  unlinkCaller?: () => void
}

let requestSequence = 0

/**
 * 稳定序列化：键排序后拼接（H71）。
 *
 * `JSON.stringify` 直接用会受**键顺序**影响，于是"同样的参数、不同的构造顺序"
 * 会被当成两个请求 —— 既破坏去重，也让"按签名取消"变得不可预测。
 */
function stableStringify(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value !== 'object') return String(value)
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return `{${entries.map(([k, v]) => `${k}:${stableStringify(v)}`).join(',')}}`
}

/**
 * 请求签名（H71）：method + url + 参数指纹。
 *
 * 为什么需要它：`cancelRequest(url)` 只比 url。用户在列表页连续切筛选时，
 * 两个请求的 url 完全相同、只有 params 不同 —— 后发的那个会把先发的取消掉，
 * 而且是**静默**的（axios cancel 不是错误提示），表现为"筛选项点了没反应"。
 */
export function requestSignature(config?: AxiosRequestConfig): string {
  if (!config) return ''
  const method = (config.method || 'GET').toUpperCase()
  const url = config.url || ''
  const params = stableStringify(config.params)
  // 非 GET 的 body 也是请求身份的一部分：同一个 url 保存不同内容不能被去重/互取消。
  const body = method === 'GET' ? '' : stableStringify(config.data)
  return `${method}:${url}?${params}#${body}`
}

/** 该请求是否要跨导航存活（`cancelAll` 的唯一例外） */
function isRetainedAcrossNavigation(config?: AxiosRequestConfig): boolean {
  return config?.__retainAcrossNavigation === true
}

class RequestManager {
  public pendingRequests: Map<string, PendingRequest> = new Map()
  private requestCache: Map<string, { promise: Promise<unknown>; timestamp: number }> = new Map()
  private readonly CACHE_TTL = 3000 // 缓存 TTL 3秒

  /**
   * 生成请求唯一标识
   */
  private getKey(config: AxiosRequestConfig): string {
    return requestSignature(config)
  }

  /**
   * 添加请求到管理器
   */
  addRequest(config: AxiosRequestConfig): string {
    const requestId = `req-${++requestSequence}`
    const cancelSource = axios.CancelToken.source()
    const callerToken = config.cancelToken as (CancelToken & { subscribe?: (listener: Canceler) => void }) | undefined
    let unlinkCaller: (() => void) | undefined
    if (callerToken?.subscribe) {
      const listener = cancelSource.cancel
      callerToken.subscribe(listener)
      unlinkCaller = () => callerToken.unsubscribe(listener)
    }
    config.__requestId = requestId
    config.cancelToken = cancelSource.token
    this.pendingRequests.set(requestId, { cancel: cancelSource.cancel, config, unlinkCaller })
    return requestId
  }

  removeRequest(config: AxiosRequestConfig): void {
    const requestId = config?.__requestId
    if (!requestId) return
    const entry = this.pendingRequests.get(requestId)
    if (!entry || entry.config !== config) return
    entry.unlinkCaller?.()
    this.pendingRequests.delete(requestId)
  }

  /**
   * 取消**签名完全匹配**的那一个请求（H71）。
   *
   * 传 `{ method, url, params, data }` 里的任意子集即可：未给的字段不参与比较。
   * 传字符串时按 url 匹配（兼容旧调用），但请优先传对象 —— url 相同、参数不同的是
   * 两个不同请求，按 url 取消会误伤。
   */
  cancelRequest(target: string | Partial<AxiosRequestConfig>): void {
    if (typeof target === 'string') {
      this.cancelWhere(entry => entry.config.url === target, 'Request cancelled')
      return
    }
    const wantedMethod = target.method ? String(target.method).toUpperCase() : ''
    const wantedUrl = target.url || ''
    const wantedParams = target.params === undefined ? undefined : stableStringify(target.params)
    const wantedBody = target.data === undefined ? undefined : stableStringify(target.data)
    this.cancelWhere(entry => {
      const cfg = entry.config
      if (wantedMethod && String(cfg.method || 'GET').toUpperCase() !== wantedMethod) return false
      if (wantedUrl && cfg.url !== wantedUrl) return false
      if (wantedParams !== undefined && stableStringify(cfg.params) !== wantedParams) return false
      if (wantedBody !== undefined && stableStringify(cfg.data) !== wantedBody) return false
      return true
    }, 'Request cancelled')
  }

  /** 按谓词取消（供"只取消某个视图的请求"这类精确场景）。 */
  cancelWhere(predicate: (entry: PendingRequest) => boolean, reason = 'Request cancelled'): number {
    let cancelled = 0
    for (const [key, entry] of [...this.pendingRequests]) {
      if (!predicate(entry)) continue
      entry.unlinkCaller?.()
      entry.cancel(reason)
      this.pendingRequests.delete(key)
      cancelled += 1
    }
    return cancelled
  }

  /**
   * 取消所有请求 (路由切换时调用)
   *
   * 例外：带 `__retainAcrossNavigation` 的请求**不取消也不清表**。典型是
   * `POST /v1/auth/session` —— 它不是"上一个页面的数据"，而是**这次会话本身**：
   * 首屏导航恰好发生在它还没回来的时候（打包版 file:// 冷启动尤其稳定复现），
   * 取消掉就等于把身份丢了 → 之后所有 `/v1` 请求都缺 `Authorization` → 全场 401，
   * 而且 `initToken` 已经缓存了那次初始化，之后不会自发重试。
   */
  cancelAll(): void {
    for (const [key, entry] of [...this.pendingRequests]) {
      if (isRetainedAcrossNavigation(entry.config)) continue
      entry.unlinkCaller?.()
      entry.cancel('Page navigated away')
      this.pendingRequests.delete(key)
    }
  }

  /**
   * 请求去重：相同请求在 CACHE_TTL 时间内返回缓存结果
   * 只适用于 GET 请求
   */
  async deduplicateRequest<T = unknown>(config: AxiosRequestConfig): Promise<unknown> {
    // 只对 GET 请求进行去重
    if (config.method?.toUpperCase() !== 'GET') {
      return axios.request<T>(config)
    }

    const key = this.getKey(config)
    const now = Date.now()

    // 检查缓存
    const cached = this.requestCache.get(key)
    if (cached && now - cached.timestamp < this.CACHE_TTL) {
      return cached.promise
    }

    // 创建新请求并缓存
    //
    // H71：**失败必须立刻失效**。旧实现把 rejected promise 原样缓存 3 秒，
    // 于是"网络抖一下"的失败会被后续 3 秒内的所有调用方复用 —— 用户点重试也拿到
    // 同一个已经拒绝的 promise，界面表现是"重试按钮没用"。
    const promise = axios.request<T>(config)
    this.requestCache.set(key, { promise, timestamp: now })
    promise.catch(() => {
      // 只删"仍是这次失败的那个"条目，避免把后来者的新请求删掉。
      const current = this.requestCache.get(key)
      if (current && current.promise === promise) this.requestCache.delete(key)
    })

    return promise
  }

  /**
   * 清理过期的缓存
   */
  cleanupCache(): void {
    const now = Date.now()
    for (const [key, value] of this.requestCache.entries()) {
      if (now - value.timestamp > this.CACHE_TTL) {
        this.requestCache.delete(key)
      }
    }
  }

  /**
   * 获取当前待处理请求数量
   */
  get pendingCount(): number {
    return this.pendingRequests.size
  }
}

export const requestManager = new RequestManager()

/**
 * 弱网检测
 */
export function isSlowNetwork(): boolean {
  const conn = (navigator as Navigator & { connection?: { effectiveType?: string; saveData?: boolean } }).connection
  if (!conn) return false
  
  const slowTypes = ['slow-2g', '2g', '3g']
  return slowTypes.includes(conn.effectiveType) || conn.saveData
}

/**
 * Axios 拦截器集成
 * 自动注册到请求管理器
 */
export function setupRequestInterceptors(instance: AxiosInstance): void {
  // 请求拦截器：注册到管理器
  instance.interceptors.request.use(config => {
    requestManager.addRequest(config)
    return config
  })

  instance.interceptors.response.use(
    response => {
      requestManager.removeRequest(response.config)
      return response
    },
    error => {
      if (error.config) requestManager.removeRequest(error.config)
      return Promise.reject(error)
    }
  )
}

/**
 * 定期清理缓存
 */
setInterval(() => {
  requestManager.cleanupCache()
}, 10000)
