/**
 * WorkbenchPanel 的分栏手柄侧向契约。
 *
 * 只测**行为契约**，不测像素。这里要守的两件事都是"错了也不报错"的：
 *
 *  1. 不传 `handleSide` 时兜底贴右。默认布局下工作台在**最左**，与主会话相邻的
 *     是它的右边缘 —— 兜底写成 `'left'` 的话，手柄会贴到窗口外缘，用户再也拖不动
 *     这一栏，而且界面上没有任何提示。
 *  2. 换边时面板与手柄必须**一起**换。只换一半的后果是把手在一边、分隔线在另一边，
 *     线出现在窗口外缘，主会话与面板之间反而没有界线。
 *
 * 尺寸夹取（下限 300 / 上限容器一半）不在断言范围：起始宽度与容器同源，
 * jsdom 下无法构造出「起始窄于上限」的矩形，硬测只会得到脆弱断言。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

import WorkbenchPanel from '../WorkbenchPanel.vue'

/**
 * 不传 `projectId` 再挂载：`watch(immediate)` 会走一次 `reload()`，而 `loadDir`
 * 在没有项目时直接 return —— 测试因此不会发出任何网络请求，也不需要 mock api。
 */
function mountPanel(props: Record<string, unknown> = {}) {
  return mount(WorkbenchPanel, { props })
}

beforeEach(() => {
  localStorage.clear()
})

describe('分栏手柄侧向', () => {
  it('不传 handleSide 时兜底贴右', () => {
    const wrapper = mountPanel()
    const panel = wrapper.find('.workbench-panel')
    expect(panel.classes()).toContain('handle-right')
    expect(panel.classes()).not.toContain('handle-left')
    // 手柄与面板必须同侧：手柄贴错边就点不到
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-right')
  })

  it('handleSide=left 时面板与手柄一起换到左侧', () => {
    const wrapper = mountPanel({ handleSide: 'left' })
    expect(wrapper.find('.workbench-panel').classes()).toContain('handle-left')
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-left')
    // 不能残留旧的一侧，否则两条边框规则会同时命中
    expect(wrapper.find('.workbench-panel').classes()).not.toContain('handle-right')
  })

  it('父布局翻转时两个元素同步跟随', async () => {
    const wrapper = mountPanel({ handleSide: 'right' })
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-right')
    await wrapper.setProps({ handleSide: 'left' })
    expect(wrapper.find('.workbench-panel').classes()).toContain('handle-left')
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-left')
  })

  it('手柄是可键盘操作的 separator', () => {
    const handle = mountPanel().find('.wb-resize')
    expect(handle.attributes('role')).toBe('separator')
    expect(handle.attributes('aria-orientation')).toBe('vertical')
    expect(handle.attributes('tabindex')).toBe('0')
  })
})
