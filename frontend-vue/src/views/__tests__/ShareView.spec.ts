import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

// 分享页只依赖一个公开读接口：拿 share id → 渲染消息。
// mock 掉即可，真正要钉的是"路由参数从哪来"。
const shareApi = vi.hoisted(() => ({ getPublicShare: vi.fn() }))
vi.mock('../../api', () => ({
  getPublicShare: shareApi.getPublicShare,
}))

import ShareView from '../ShareView.vue'

/**
 * 分享落地页有两条路径指向同一个组件：
 * - `/s/:token` —— 对外发出去的短链形态（utils/shareLink.ts 的 SHARE_PATH_PREFIX）；
 * - `/share/:id` —— 早期链接的兼容别名，已经散落在聊天记录里，不能失效。
 *
 * 参数名不同，组件必须两处都读。只读 `params.id` 会让新短链在打开时静默请求
 * 一个空 id（后端 404），页面表现为"链接不存在"——链接其实好好的。
 */
function makeRouter(path: string) {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/s/:token', name: 'ShareShort', component: ShareView, meta: { public: true } },
      { path: '/share/:id', name: 'Share', component: ShareView, meta: { public: true } },
    ],
  })
}

async function mountAt(path: string) {
  const router = makeRouter(path)
  await router.push(path)
  await router.isReady()
  const wrapper = mount(ShareView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('分享落地页（/s/:token 与 /share/:id 共用）', () => {
  it('短链 /s/:token 用 token 作为 share id 去取数据', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockResolvedValue({ id: 'tok-1', title: 'T', created_at: '2026-11-18T10:00:00Z', messages: [] })

    await mountAt('/s/tok-1')

    expect(shareApi.getPublicShare).toHaveBeenCalledWith('tok-1')
  })

  it('旧路径 /share/:id 仍然可用（已发出去的链接不能变 404）', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockResolvedValue({ id: 'old-1', title: 'T', created_at: '2026-11-18T10:00:00Z', messages: [] })

    await mountAt('/share/old-1')

    expect(shareApi.getPublicShare).toHaveBeenCalledWith('old-1')
  })

  it('路径参数为 URL 编码时按解码后的值请求', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockResolvedValue({ id: 'a/b', title: 'T', created_at: '2026-11-18T10:00:00Z', messages: [] })

    await mountAt('/s/a%2Fb')

    expect(shareApi.getPublicShare).toHaveBeenCalledWith('a/b')
  })
})
