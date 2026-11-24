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
    const method = config.method?.toUpperCase() || 'GET'
    const url = config.url || ''
    const params = method === 'GET' ? JSON.stringify(config.params || {}) : ''
    return `${method}:${url}${params}`
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
   * 取消特定 URL 的请求
   */
  cancelRequest(url: string): void {
    this.pendingRequests.forEach((entry, key) => {
      if (entry.config.url === url) {
        entry.unlinkCaller?.()
        entry.cancel('Request cancelled')
        this.pendingRequests.delete(key)
      }
    })
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
    const promise = axios.request<T>(config)
    this.requestCache.set(key, { promise, timestamp: now })

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
