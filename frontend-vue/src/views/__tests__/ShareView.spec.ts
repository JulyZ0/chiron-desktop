import { describe, expect, it, vi, afterEach } from 'vitest'
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

/** 加载态（骨架）是否还在 —— 页面"一直转圈"就是它永不为 false。 */
function isLoading(wrapper: Awaited<ReturnType<typeof mountAt>>) {
  return wrapper.find('.page-skeleton').exists()
}

function errorText(wrapper: Awaited<ReturnType<typeof mountAt>>) {
  return wrapper.find('.empty-state').text()
}

function httpError(status: number, code?: string) {
  return Object.assign(new Error(`request failed with status ${status}`), {
    response: { status, data: { success: false, code } },
  })
}

afterEach(() => {
  vi.useRealTimers()
})

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

/**
 * ── 加载必须有终态 ──
 *
 * 事故形态：`/s/<token>` 落地页**一直转圈、永远不结束**。根因不是"请求失败没处理"，
 * 而是页面根本没有"等不到"这一态：清 `loading` 只写在 `finally` 里，而请求被全局
 * 拦截器挂住 / 迟迟不 settle 时 `finally` 永远到不了。所以这里逐个钉住三种终态：
 *   成功渲染 / 明确失效（404、410）/ 网络超时（可重试）。
 */
describe('分享落地页的加载终态', () => {
  it('成功：渲染标题与消息，骨架必须结束', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockResolvedValue({
      id: 'tok-2',
      title: '会话标题',
      created_at: '2026-11-18T10:00:00Z',
      messages: [{ role: 'user', content: '你好', created_at: '2026-11-18T10:00:00Z' }],
    })

    const wrapper = await mountAt('/s/tok-2')

    expect(isLoading(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('会话标题')
    expect(wrapper.text()).toContain('你好')
    expect(wrapper.find('.empty-state').exists()).toBe(false)
  })

  it('404：明确失效页，且不给无意义的重试按钮', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockRejectedValue(httpError(404, 'not_found'))

    const wrapper = await mountAt('/s/missing')

    expect(isLoading(wrapper)).toBe(false)
    expect(errorText(wrapper)).toContain('分享不存在或已失效')
    expect(wrapper.find('.share-retry').exists()).toBe(false)
  })

  it('410 撤销：说成"创建者删除"（与过期区分）', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockRejectedValue(httpError(410, 'share_revoked'))

    const wrapper = await mountAt('/s/revoked')

    expect(isLoading(wrapper)).toBe(false)
    expect(errorText(wrapper)).toContain('删除')
  })

  it('410 过期（无 code）：不能误报成"创建者删除"', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockRejectedValue(httpError(410, 'share_expired'))

    const wrapper = await mountAt('/s/expired')

    expect(isLoading(wrapper)).toBe(false)
    expect(errorText(wrapper)).toContain('分享不存在或已失效')
  })

  it('★ 请求永不 settle：10s 后必须收口并可重试', async () => {
    vi.useFakeTimers()
    shareApi.getPublicShare.mockReset()
    // 模拟被挂住的请求：既不 resolve 也不 reject（全局拦截器 401 跳转 / 退避时就是这样）
    shareApi.getPublicShare.mockReturnValue(new Promise(() => {}))

    const wrapper = await mountAt('/s/hang')

    // 超时之前仍是骨架（这是正常的加载态）
    expect(isLoading(wrapper)).toBe(true)

    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    expect(isLoading(wrapper)).toBe(false)
    expect(errorText(wrapper)).toContain('加载失败')
    expect(wrapper.find('.share-retry').exists()).toBe(true)

    // 重试：真的再发一次请求（且立刻回到加载态）
    shareApi.getPublicShare.mockResolvedValue({
      id: 'hang', title: '恢复了', created_at: '2026-11-18T10:00:00Z', messages: [],
    })
    await wrapper.find('.share-retry').trigger('click')
    await flushPromises()

    expect(shareApi.getPublicShare).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('恢复了')
    expect(isLoading(wrapper)).toBe(false)
  })

  it('401（匿名页被鉴权拦下）：归为可重试失败，而不是把访客扔走', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockRejectedValue(httpError(401, 'auth_required'))

    const wrapper = await mountAt('/s/needs-auth')

    expect(isLoading(wrapper)).toBe(false)
    expect(wrapper.find('.share-retry').exists()).toBe(true)
  })

  it('200 但 body 不是分享（网关兜底吐 HTML）：按失败收口，不渲染空白', async () => {
    shareApi.getPublicShare.mockReset()
    shareApi.getPublicShare.mockRejectedValue(new Error('share: empty payload'))

    const wrapper = await mountAt('/s/html')

    expect(isLoading(wrapper)).toBe(false)
    expect(errorText(wrapper)).toContain('加载失败')
  })
})
