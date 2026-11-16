import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * 组件健康：网关统一用 `OK()` 包装成 `{success, data}`。
 *
 * 这里盯的是**解包层级**：早前取 `res.data` 拿到的是包装体本身，
 * 里面没有 postgres/redis/engine/gateway 这些键，面板每一行都显示「—」，
 * 看起来像"组件健康没有数据"，实际是取错了一层。
 */
const get = vi.fn()

vi.mock('../index', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
}))

const { fetchComponentsHealth } = await import('../systemHealth')

describe('fetchComponentsHealth', () => {
  beforeEach(() => get.mockReset())

  it('请求带 /v1 前缀（网关只注册了 GET /v1/system/components）', async () => {
    // 少写 `/v1` 时网关回 404，而下面的 catch 会把 404 吞成 null ——
    // 面板因此永远显示「—」，看起来像"后端没采到数据"。所以这里盯住 URL 本身。
    get.mockResolvedValue({ data: { data: { gateway: { healthy: true } } } })

    await fetchComponentsHealth()

    expect(get.mock.calls[0][0]).toBe('/v1/system/components')
  })

  it('解开 OK() 包装，返回内层组件数据', async () => {
    const inner = {
      postgres: { available: true },
      redis: { available: true },
      engine: { healthy: true },
      gateway: { healthy: true, goroutines: 42, uptime_seconds: 10 },
      memory: { postgres: { database_bytes: 1024 }, gateway: { heap_bytes: 2048 } },
    }
    get.mockResolvedValue({ data: { success: true, data: inner } })

    const got = await fetchComponentsHealth()

    expect(got).toEqual(inner)
    // 关键：不能把包装体当成结果交出去
    expect(got).not.toHaveProperty('success')
    expect(got?.postgres?.available).toBe(true)
    expect(got?.memory?.postgres?.database_bytes).toBe(1024)
  })

  it('兼容未包装的响应（本地 core 直出）', async () => {
    const inner = { postgres: { available: false } }
    get.mockResolvedValue({ data: inner })

    await expect(fetchComponentsHealth()).resolves.toEqual(inner)
  })

  it('响应为空时返回 null，不冒充"全部正常"', async () => {
    get.mockResolvedValue({ data: undefined })

    await expect(fetchComponentsHealth()).resolves.toBeNull()
  })

  it('非对象响应返回 null', async () => {
    get.mockResolvedValue({ data: { data: 'oops' } })

    await expect(fetchComponentsHealth()).resolves.toBeNull()
  })
})
