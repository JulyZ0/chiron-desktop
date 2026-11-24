import { describe, expect, it, vi } from 'vitest'

vi.mock('../guard', () => ({ authGuard: vi.fn() }))
vi.mock('../progress', () => ({ setupRouteProgress: vi.fn() }))
vi.mock('../schemaMismatch', () => ({ setupSchemaMismatchWatch: vi.fn() }))

import router from '../index'

describe('后台服务商管理路由', () => {
  it('/admin/providers 使用新页面名和服务商管理标题', () => {
    const route = router.resolve('/admin/providers')
    expect(route.name).toBe('AdminProviders')
    expect(route.meta.title).toBe('服务商管理')
  })

  it('旧 /admin/api-keys 书签重定向到 /admin/providers', () => {
    const route = router.resolve('/admin/api-keys')
    expect(route.matched.at(-1)?.redirect).toEqual({ path: '/admin/providers' })
  })
})
