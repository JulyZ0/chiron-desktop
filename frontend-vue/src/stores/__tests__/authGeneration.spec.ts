import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

/**
 * H68（PRD 9.10）**登录、登出与账户切换的 auth generation 围栏**。
 *
 * 缺陷形态：token 被直接覆盖，profile / logout / 401 / 重试都不校验"这笔异步结果
 * 属于哪个账号"。于是出现三件事：
 *   · A 的迟到 401 把 B 清掉（B 刚登录就被踢回登录页）；
 *   · A 的迟到 profile 把 B 的 `user` 覆盖成 A；
 *   · 延迟重试把 B 的 token 附到 A 发出的请求上。
 *
 * 这里钉住 store 侧的行为：每次认证切换递增代次，迟到结果一律**不写回**。
 */

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    api: {
      post: vi.fn(),
      get: vi.fn(),
    },
  }
})

import { useAuthStore } from '../auth'
import { api, currentAuthGeneration } from '../../api'
import { privateKey } from '../../utils/privateStorage'

const userA = {
  id: 'u-a', email: 'a@x.c', name: 'A', role: 'admin', tenant_id: 1,
  password_set: true, must_change_password: false,
}
const userB = {
  id: 'u-b', email: 'b@x.c', name: 'B', role: 'user', tenant_id: 1,
  password_set: true, must_change_password: false,
}

/** 可手动 resolve/reject 的 promise —— 用来制造"A 的请求晚于 B 的登录返回" */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  setActivePinia(createPinia())
  try { localStorage.clear() } catch { /* jsdom 存储不可用不影响本用例 */ }
  vi.clearAllMocks()
})

describe('认证代次围栏（H68）', () => {
  it('登录与登出都递增代次，且与请求层共用同一个代次', async () => {
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-a', user: userA } } })
    const store = useAuthStore()

    const before = store.generation
    expect(before).toBe(currentAuthGeneration())

    await store.login('a@x.c', 'pw')
    const afterLogin = store.generation
    expect(afterLogin).toBeGreaterThan(before)
    expect(afterLogin).toBe(currentAuthGeneration())

    await store.logout()
    expect(store.generation).toBeGreaterThan(afterLogin)
    expect(store.generation).toBe(currentAuthGeneration())
  })

  it('★ A 的迟到 profile 不得覆盖 B 的身份', async () => {
    const store = useAuthStore()
    // 先以 A 登录（建立"上一个账号"的现场）
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-a', user: userA } } })
    await store.login('a@x.c', 'pw')
    expect(store.user?.id).toBe('u-a')

    // A 的 profile 请求发出去了，但还没回来
    const profile = deferred<{ data: { data: Record<string, unknown> } }>()
    vi.mocked(api.get).mockReturnValue(profile.promise as never)
    const inflight = store.fetchProfile()

    // 期间换成 B 登录（真实顺序：登出 → 登录）
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-b', user: userB } } })
    await store.login('b@x.c', 'pw')
    expect(store.user?.id).toBe('u-b')

    // A 的 profile 现在才回来
    profile.resolve({ data: { data: { user_id: 'u-a', email: 'a@x.c', name: 'A', role: 'admin' } } })
    await inflight

    // 身份仍是 B —— 迟到的旧账号结果被整条丢弃
    expect(store.user?.id).toBe('u-b')
    expect(store.user?.role).toBe('user')
    expect(store.token).toBe('tok-b')
  })

  it('★ A 的迟到 401（profile 失败）不得把 B 登出', async () => {
    const store = useAuthStore()
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-a', user: userA } } })
    await store.login('a@x.c', 'pw')

    const profile = deferred<never>()
    vi.mocked(api.get).mockReturnValue(profile.promise as never)
    const inflight = store.fetchProfile()

    // 换成 B 登录
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-b', user: userB } } })
    await store.login('b@x.c', 'pw')

    // A 的 profile 以 401 收尾 → 旧实现会调 logout()，把 B 一起登出
    profile.reject(Object.assign(new Error('401'), { response: { status: 401 } }))
    await inflight

    expect(store.user?.id).toBe('u-b')
    expect(store.token).toBe('tok-b')
    // 没有发生"清空本地态"的登出
    expect(localStorage.getItem('user')).not.toBeNull()
  })

  it('★ 连点登录时以最后一次为准：先回来的旧响应不写身份', async () => {
    const store = useAuthStore()
    const first = deferred<{ data: { data: Record<string, unknown> } }>()
    const second = deferred<{ data: { data: Record<string, unknown> } }>()
    vi.mocked(api.post)
      .mockReturnValueOnce(first.promise as never)
      .mockReturnValueOnce(second.promise as never)

    const p1 = store.login('a@x.c', 'pw')
    const p2 = store.login('b@x.c', 'pw')

    // 第二次（B）先落地
    second.resolve({ data: { data: { token: 'tok-b', user: userB } } })
    await p2
    expect(store.user?.id).toBe('u-b')

    // 第一次（A）晚到：不得把身份改回 A
    first.resolve({ data: { data: { token: 'tok-a', user: userA } } })
    await p1

    expect(store.user?.id).toBe('u-b')
    expect(store.token).toBe('tok-b')
  })

  it('换账号时清掉账号域本地缓存（输入历史等），同账号重登不清', async () => {
    const store = useAuthStore()
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-a', user: userA } } })
    await store.login('a@x.c', 'pw')

    localStorage.setItem(privateKey('chiron:composer-history', 'u-a'), JSON.stringify(['A 的输入']))

    // 同账号重新登录：history 属于这个账号，不清
    await store.login('a@x.c', 'pw')
    expect(localStorage.getItem(privateKey('chiron:composer-history', 'u-a'))).not.toBeNull()

    // 换账号：上一个账号的私有数据必须清掉
    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-b', user: userB } } })
    await store.login('b@x.c', 'pw')
    expect(localStorage.getItem(privateKey('chiron:composer-history', 'u-a'))).toBeNull()
  })

  it('API Key 换绑失败只在仍是同一代认证时才回滚凭据', async () => {
    const store = useAuthStore()
    /**
     * 场景：A 填了一把无效 key（profile 慢），期间正常登录成 B；A 那次 profile 才失败。
     * 旧实现会在 catch 里 `applyToken('')` —— 把 B 的凭据一起清掉。
     */
    const invalidKeyProfile = deferred<never>()
    vi.mocked(api.get).mockReturnValue(invalidKeyProfile.promise as never)
    const inflight = store.loginWithApiKey('sk-invalid')

    vi.mocked(api.post).mockResolvedValue({ data: { data: { token: 'tok-b', user: userB } } })
    await store.login('b@x.c', 'pw')
    expect(store.token).toBe('tok-b')

    invalidKeyProfile.reject(Object.assign(new Error('401'), { response: { status: 401 } }))
    await expect(inflight).rejects.toBeTruthy()

    // B 的凭据完好
    expect(store.token).toBe('tok-b')
    expect(store.user?.id).toBe('u-b')
  })

  it('本地会话（ensureLocalSession）迟到时同样不写回旧身份', async () => {
    const store = useAuthStore()
    const session = deferred<{ data: { data: Record<string, unknown> } }>()
    // 第一次 post 是建会话（挂住），其后的登出调用要正常返回，否则用例自己会挂死
    vi.mocked(api.post)
      .mockReturnValueOnce(session.promise as never)
      .mockResolvedValue({ data: {} } as never)

    const inflight = store.ensureLocalSession(true)
    // 期间登出（这也是一次认证切换）
    await store.logout()

    session.resolve({ data: { data: { token: 'tok-local', user: userA } } })
    await expect(inflight).resolves.toBe(false)
    expect(store.user).toBeNull()
  })
})
