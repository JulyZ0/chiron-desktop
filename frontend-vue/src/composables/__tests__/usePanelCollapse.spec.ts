/**
 * 折叠状态的三条约定：
 * 1. 用户的选择会持久化（刷新后仍生效）；
 * 2. 从未选择过时用 defaultOpen()；
 * 3. 直接给 open 赋值（视图里的 panelOpen.value = false）也写存储 —— 否则"关掉后刷新
 *    又自己回来"会从另一条入口复现。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { usePanelCollapse } from '../usePanelCollapse'

describe('usePanelCollapse', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('从未选择过时使用 defaultOpen', () => {
    const { open } = usePanelCollapse({
      storageKey: 'test:panel',
      defaultOpen: () => true,
      narrowQuery: '(max-width: 1px)',
    })
    expect(open.value).toBe(true)
  })

  it('读取已保存的选择（宽屏）', () => {
    localStorage.setItem('test:panel', '0')
    const { open } = usePanelCollapse({
      storageKey: 'test:panel',
      defaultOpen: () => true,
      narrowQuery: '(max-width: 1px)',
    })
    expect(open.value).toBe(false)
  })

  it('setOpen 与直接赋值都会持久化', () => {
    const a = usePanelCollapse({
      storageKey: 'test:panel',
      defaultOpen: () => true,
      narrowQuery: '(max-width: 1px)',
    })
    a.setOpen(false)
    expect(localStorage.getItem('test:panel')).toBe('0')

    a.open.value = true
    expect(localStorage.getItem('test:panel')).toBe('1')

    // 新实例（模拟刷新）读到的是最后一次选择
    const b = usePanelCollapse({
      storageKey: 'test:panel',
      defaultOpen: () => false,
      narrowQuery: '(max-width: 1px)',
    })
    expect(b.open.value).toBe(true)
  })

  it('toggle 翻转并保存', () => {
    const { toggle, open } = usePanelCollapse({
      storageKey: 'test:panel',
      defaultOpen: () => false,
      narrowQuery: '(max-width: 1px)',
    })
    toggle()
    expect(open.value).toBe(true)
    expect(localStorage.getItem('test:panel')).toBe('1')
  })
})
