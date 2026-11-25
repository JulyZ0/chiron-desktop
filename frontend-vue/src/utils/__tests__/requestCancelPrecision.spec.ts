/**
 * H71：取消的粒度必须精确到"这一次请求"（PRD 9.10）。
 *
 * 缺陷形态：
 *   1. `cancelRequest(url)` 只比 url。用户在列表页连续切筛选时，两个请求的 url 相同、
 *      只有 params 不同 —— 后发的那个会把先发的取消掉，而且 axios 的 cancel 不是错误
 *      提示，界面表现就是"筛选项点了没反应"。
 *   2. `getKey` 对非 GET **完全忽略 body**：同一 url 保存不同内容会被当成同一个请求去重
 *      （写请求被去重 = 吞掉一次写入）。
 *   3. 去重缓存把 rejected promise 也存 3 秒：一次网络抖动之后，3 秒内所有"重试"都拿到
 *      同一个已拒绝的 promise —— 重试按钮形同虚设。
 *
 * 这组用例钉住签名构成、精确取消、以及"失败不进缓存"。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { AxiosRequestConfig } from 'axios'

const axiosMocks = vi.hoisted(() => {
  const cancels: Array<() => void> = []
  return {
    cancels,
    request: vi.fn((config?: unknown): Promise<unknown> => {
      void config
      return Promise.resolve({ data: 'ok' })
    }),
    CancelToken: {
      source: () => {
        const cancel = vi.fn()
        cancels.push(cancel)
        return {
          token: { promise: Promise.resolve(), subscribe: vi.fn(() => vi.fn()) },
          cancel,
        }
      },
    },
    isCancel: (e: unknown) => Boolean((e as { __cancel?: boolean })?.__cancel),
  }
})

vi.mock('axios', () => ({
  default: axiosMocks,
  ...axiosMocks,
}))

import { requestManager, requestSignature } from '../requestOptimizer'

function pendingUrls(): string[] {
  return [...requestManager.pendingRequests.values()].map(entry => String(entry.config.url || ''))
}

beforeEach(() => {
  requestManager.cancelAll()
  requestManager.pendingRequests.clear()
  axiosMocks.request.mockClear()
  axiosMocks.cancels.length = 0
})

describe('requestSignature (H71)', () => {
  it('同参数不同键序 → 同一个签名（否则去重失效）', () => {
    const a = requestSignature({ method: 'get', url: '/v1/list', params: { page: 1, size: 20 } })
    const b = requestSignature({ method: 'GET', url: '/v1/list', params: { size: 20, page: 1 } })
    expect(a).toBe(b)
  })

  it('params 不同 → 签名不同（这是精确取消的基础）', () => {
    const a = requestSignature({ method: 'get', url: '/v1/list', params: { page: 1 } })
    const b = requestSignature({ method: 'get', url: '/v1/list', params: { page: 2 } })
    expect(a).not.toBe(b)
  })

  it('非 GET 的 body 参与签名（否则"保存两次不同内容"会被去重）', () => {
    const a = requestSignature({ method: 'post', url: '/v1/x', data: { v: 1 } })
    const b = requestSignature({ method: 'post', url: '/v1/x', data: { v: 2 } })
    expect(a).not.toBe(b)
  })
})

describe('cancelRequest 精确取消 (H71)', () => {
  it('同 url 不同 params：只取消指定的那个', () => {
    requestManager.addRequest({ method: 'get', url: '/v1/media', params: { page: 1 } } as AxiosRequestConfig)
    requestManager.addRequest({ method: 'get', url: '/v1/media', params: { page: 2 } } as AxiosRequestConfig)
    expect(pendingUrls()).toHaveLength(2)

    requestManager.cancelRequest({ method: 'get', url: '/v1/media', params: { page: 1 } })

    const left = requestsLeft()
    expect(left).toHaveLength(1)
  })

  it('取消一个不存在的签名时什么都不做', () => {
    requestManager.addRequest({ method: 'get', url: '/v1/media', params: { page: 1 } } as AxiosRequestConfig)
    requestManager.cancelRequest({ method: 'get', url: '/v1/media', params: { page: 9 } })
    expect(requestsLeft()).toHaveLength(1)
  })

  it('cancelWhere 只影响命中的请求（按资源/视图收敛）', () => {
    requestManager.addRequest({ method: 'get', url: '/v1/media' } as AxiosRequestConfig)
    requestManager.addRequest({ method: 'get', url: '/v1/chat' } as AxiosRequestConfig)
    const n = requestManager.cancelWhere(entry => entry.config.url === '/v1/media')
    expect(n).toBe(1)
    expect(requestsLeft()).toHaveLength(1)
  })
})

/** 当前待处理表里的请求数（cancelRequest 会把条目删掉）。 */
function requestsLeft(): unknown[] {
  return [...requestManager.pendingRequests.values()]
}

describe('去重缓存：失败必须立即失效 (H71)', () => {
  it('第一次失败后，紧接着的调用会真的再发一次', async () => {
    axiosMocks.request.mockImplementationOnce(() => Promise.reject(new Error('network')))
    const cfg = { method: 'get', url: '/v1/fail-once', params: { a: 1 } } as AxiosRequestConfig

    await expect(requestManager.deduplicateRequest(cfg)).rejects.toThrow('network')
    axiosMocks.request.mockImplementationOnce(() => Promise.resolve({ data: 'second' }))
    await expect(requestManager.deduplicateRequest(cfg)).resolves.toEqual({ data: 'second' })

    expect(axiosMocks.request).toHaveBeenCalledTimes(2)
  })

  it('成功后仍按 TTL 复用（不能把去重彻底废掉）', async () => {
    const cfg = { method: 'get', url: '/v1/cached', params: { a: 1 } } as AxiosRequestConfig
    await requestManager.deduplicateRequest(cfg)
    await requestManager.deduplicateRequest(cfg)
    expect(axiosMocks.request).toHaveBeenCalledTimes(1)
  })
})
