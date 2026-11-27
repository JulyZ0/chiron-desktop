import { beforeEach, describe, expect, it } from 'vitest'

import {
  __resetProjectWorkspaceForTests,
  activateProjectTab,
  closeProjectTab,
  ensureProjectTab,
  lastProjectSession,
  loadProjectWorkspace,
  projectWorkbenchOpen,
  pruneProjectWorkspace,
  rememberProjectSession,
  resetProjectWorkspace,
  setProjectWorkbench,
  useProjectWorkspace,
} from '../useProjectWorkspace'

describe('useProjectWorkspace（N7 项目标签）', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetProjectWorkspaceForTests()
  })

  it('打开/激活/记住会话/工作台状态', () => {
    loadProjectWorkspace('u1')
    ensureProjectTab('p1')
    activateProjectTab('p2')
    rememberProjectSession('p1', 's1')
    setProjectWorkbench('p1', true)

    const state = useProjectWorkspace()
    expect(state.open).toEqual(['p1', 'p2'])
    expect(state.active).toBe('p2')
    // 激活新标签时它也会被打开
    expect(lastProjectSession('p1')).toBe('s1')
    expect(projectWorkbenchOpen('p1')).toBe(true)
    expect(projectWorkbenchOpen('p2')).toBe(false)
  })

  it('关闭激活标签按浏览器习惯切到右邻，再退左邻', () => {
    loadProjectWorkspace('u1')
    ensureProjectTab('a')
    ensureProjectTab('b')
    ensureProjectTab('c')
    activateProjectTab('b')

    expect(closeProjectTab('b')).toBe('c')
    expect(closeProjectTab('c')).toBe('a')
    expect(closeProjectTab('a')).toBe('')
  })

  it('账号之间互不可见', () => {
    loadProjectWorkspace('u1')
    ensureProjectTab('p1')
    rememberProjectSession('p1', 's1')

    loadProjectWorkspace('u2')
    expect(useProjectWorkspace().open).toEqual([])
    expect(lastProjectSession('p1')).toBe('')

    // 切回 u1 能恢复（从 localStorage 重新载入）
    loadProjectWorkspace('u1')
    expect(useProjectWorkspace().open).toEqual(['p1'])
    expect(lastProjectSession('p1')).toBe('s1')
  })

  it('prune 丢掉已删除的项目，并把激活标签收口到剩余项', () => {
    loadProjectWorkspace('u1')
    ensureProjectTab('p1')
    ensureProjectTab('p2')
    ensureProjectTab('p3')
    activateProjectTab('p2')
    rememberProjectSession('p2', 's2')

    pruneProjectWorkspace(['p1', 'p3'])
    const state = useProjectWorkspace()
    expect(state.open).toEqual(['p1', 'p3'])
    expect(state.active).toBe('p3')
    expect(state.sessions.p2).toBeUndefined()
  })

  it('reset 清空当前账号并落盘', () => {
    loadProjectWorkspace('u1')
    ensureProjectTab('p1')
    resetProjectWorkspace()
    expect(useProjectWorkspace().open).toEqual([])
    expect(localStorage.getItem('chiron.project-workspace.v1:u1')).toContain('"open":[]')
  })
})
