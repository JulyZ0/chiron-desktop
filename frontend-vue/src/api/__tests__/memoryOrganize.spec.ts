import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../index'
import {
  getOrganizeStatus,
  startOrganize,
  listMemory,
  listConflicts,
  listSummaries,
  searchMemory,
} from '../memory'

/**
 * 记忆页「智能整理」的渲染崩溃回归（2026-11-18，用户报"页面渲染出错"）。
 *
 * 根因是**响应多包了一层**：桌面端 `/v1/memory/*` 全程经 Go 网关转发，网关用
 * `OK()` 统一包成 `{success:true, data:<引擎响应>}`（internal/api/response.go:63），
 * 引擎自己的响应又带 `success` 字段 —— 前端真正要的数据在 `data.data` 里。
 *
 * `startOrganize()` 当时直接 `return data`，于是 `res.status` 是 `undefined`；
 * `runOrganize` 把它写进 `organize` 之后，模板无条件读 `organize.running` 抛
 * TypeError，整页被 `ErrorBoundary` 接住 —— 用户看到的就是那句
 * "An unexpected error occurred. Please try again."，而接口**没有任何 HTTP 错误**。
 *
 * 所以这里锁两层：
 *  1. 包装形态必须解对（否则又会静默拿到 undefined）；
 *  2. 任何一层缺字段，`status` 也必须落到**完整空态** —— 模板读字段的假设不能破。
 */
describe('记忆接口解开网关 {success,data} 包装', () => {
  afterEach(() => vi.restoreAllMocks())

  const STATUS = {
    running: false,
    started_at: 1,
    finished_at: 2,
    result: { backfilled: 1, merged: 2, archived: 3, evicted: 4, errors: [] },
    error: null,
  }

  it('startOrganize 从 data.data 取到 status（不再是把 undefined 写进 organize）', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { success: true, data: { success: true, started: true, status: STATUS } },
    } as never)

    const res = await startOrganize()
    expect(res.started).toBe(true)
    expect(res.status.running).toBe(false)
    expect(res.status.result?.merged).toBe(2)
  })

  it('startOrganize 缺 status 时兜底成完整空态（模板仍然读得到 running）', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true, data: {} } } as never)

    const res = await startOrganize()
    expect(res.started).toBe(false)
    expect(res.status).toEqual({
      running: false,
      started_at: null,
      finished_at: null,
      result: null,
      error: null,
    })
  })

  it('getOrganizeStatus 逐层解开 {success,data:{success,status}}', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: { success: true, data: { success: true, status: STATUS } },
    } as never)

    expect((await getOrganizeStatus()).finished_at).toBe(2)
  })

  it('getOrganizeStatus 拿不到 status 时返回空态而不是 undefined', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { success: true, data: {} } } as never)
    expect((await getOrganizeStatus()).running).toBe(false)
  })

  it('listMemory 解包后 organize 字段不会丢（列表自带的整理态）', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: {
        success: true,
        data: {
          success: true,
          entries: [],
          counts: {},
          total: 0,
          slots: [],
          organize: { ...STATUS, running: true },
        },
      },
    } as never)

    expect((await listMemory()).organize.running).toBe(true)
  })

  it('listMemory 缺 organize 时也给出空态', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: { success: true, data: { success: true, entries: [], total: 0 } },
    } as never)

    expect((await listMemory()).organize.running).toBe(false)
  })

  it('listSummaries / listConflicts 同样解包，且缺字段时回落到空集合', async () => {
    vi.spyOn(api, 'get')
      .mockResolvedValueOnce({ data: { success: true, data: { summaries: [{ id: 's1' }], count: 1 } } } as never)
      .mockResolvedValueOnce({ data: { success: true, data: {} } } as never)

    expect((await listSummaries()).count).toBe(1)
    expect(await listConflicts()).toEqual({ conflicts: [], count: 0 })
  })

  it('searchMemory 解包后能拿到 results（否则检索永远显示"没找到"）', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: {
        success: true,
        data: {
          success: true,
          query: '编辑器',
          mode: 'semantic',
          count: 1,
          results: [{ id: 'm1', similarity: 0.9, score: 0.8 }],
          summaries: [],
          summary_count: 0,
          summary_mode: 'recent',
        },
      },
    } as never)

    const res = await searchMemory('编辑器')
    expect(res.results).toHaveLength(1)
    expect(res.mode).toBe('semantic')
    expect(res.summary_mode).toBe('recent')
  })
})
