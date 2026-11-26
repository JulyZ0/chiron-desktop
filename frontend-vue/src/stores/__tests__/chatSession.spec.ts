import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

/**
 * 会话身份 / 项目归属（`stores/chatSession.ts`）。
 *
 * 这块逻辑从 ChatView 抽出，正是因为它有三个"改坏了不会报错"的判据：
 *  1. `root_path` 为空的旧「画布」不算项目；
 *  2. 切换会话后晚到的项目查询必须丢弃（否则 `@` 会搜上一个会话的目录）；
 *  3. 查询失败不能伪装成"没有项目"。
 */
const projectsApi = vi.hoisted(() => ({
  sessionProject: vi.fn(),
}))
vi.mock('../../views/sessionmap/projectsApi', () => projectsApi)

import { useChatSessionStore } from '../chatSession'

describe('chatSession store：项目归属解析', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
  })

  it('root_path 非空才算项目', async () => {
    projectsApi.sessionProject.mockResolvedValue({
      id: 'p1',
      name: 'demo',
      root_path: 'D:/ws/demo',
    })
    const store = useChatSessionStore()
    store.activeSessionId = 's1'
    await vi.waitFor(() => expect(store.activeProject?.id).toBe('p1'))
    expect(store.activeProject?.name).toBe('demo')
    expect(store.activeProjectError).toBe('')
  })

  it('root_path 为空的旧画布不算项目，且不报错', async () => {
    projectsApi.sessionProject.mockResolvedValue({ id: 'canvas', name: '画布', root_path: '' })
    const store = useChatSessionStore()
    store.activeSessionId = 's1'
    await vi.waitFor(() => expect(projectsApi.sessionProject).toHaveBeenCalledWith('s1'))
    await Promise.resolve()
    expect(store.activeProject).toBeNull()
    expect(store.activeProjectError).toBe('')
  })

  it('晚到的项目结果必须丢弃（切会话后不再写回）', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    projectsApi.sessionProject.mockImplementation((sid: string) => {
      if (sid === 's1') return new Promise(resolve => { resolveFirst = resolve })
      return Promise.resolve({ id: 'p2', name: 'second', root_path: 'D:/ws/second' })
    })
    const store = useChatSessionStore()
    store.activeSessionId = 's1'
    await vi.waitFor(() => expect(projectsApi.sessionProject).toHaveBeenCalledWith('s1'))
    store.activeSessionId = 's2'
    await vi.waitFor(() => expect(projectsApi.sessionProject).toHaveBeenCalledWith('s2'))
    await vi.waitFor(() => expect(store.activeProject?.id).toBe('p2'))

    resolveFirst({ id: 'p1', name: 'first', root_path: 'D:/ws/first' })
    await Promise.resolve()
    await Promise.resolve()
    expect(store.activeProject?.id).toBe('p2')
  })

  it('查询失败留下原因，不伪装成"没有项目"', async () => {
    projectsApi.sessionProject.mockRejectedValue(new Error('boom'))
    const store = useChatSessionStore()
    store.activeSessionId = 's1'
    await vi.waitFor(() => expect(store.activeProjectError).not.toBe(''))
    expect(store.activeProject).toBeNull()
  })

  it('清空 activeSessionId 时清掉项目与错误', async () => {
    projectsApi.sessionProject.mockResolvedValue({ id: 'p1', name: 'demo', root_path: 'D:/ws' })
    const store = useChatSessionStore()
    store.activeSessionId = 's1'
    await vi.waitFor(() => expect(store.activeProject?.id).toBe('p1'))
    store.activeSessionId = ''
    await vi.waitFor(() => expect(store.activeProject).toBeNull())
    expect(store.activeProjectError).toBe('')
  })
})
