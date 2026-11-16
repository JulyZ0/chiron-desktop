import { describe, it, expect, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import TitleBar from '../TitleBar.vue'
import type { ChironDesktopHost, WindowState } from '../../../stores/device'

/**
 * 自绘标题栏的测试。
 *
 * 这里钉的都是**猜错就不可用**的点，而不是像素：
 *
 *  1. 没有 `titleBar` 的老外壳 / 浏览器预览里必须渲染成空 —— 那些环境还有系统标题栏，
 *     多画一组按钮就是两组关闭键。
 *  2. `mode: 'native'`（setup.html / error 这类保留系统框的静态页）同样必须为空。
 *  3. 三个按钮真的打到对应 IPC：frameless 窗口里没有别的出口，
 *     按钮点了没反应等于窗口关不掉。
 *  4. 最大化状态既要从 `getState` 读到，也要跟着 `onStateChange` 更新
 *     （Aero Snap / Win+方向键不经过任何点击）。
 *  5. 卸载时既撤销订阅、也收回 `--app-titlebar-h`：残留的高度会在顶部留一条空白，
 *     而那时已经没有标题栏去解释它。
 */

const DEFAULT_STATE: WindowState = { maximized: false, fullScreen: false, focused: true }

function installHost(titleBar: ChironDesktopHost['titleBar']) {
  ;(globalThis as { chironDesktop?: ChironDesktopHost }).chironDesktop = {
    isDesktop: true,
    platform: 'win32',
    titleBar,
  }
}

/** 造一份可断言的 titleBar API：每个动作都是一个 spy，状态变化可由测试手动推送 */
function fakeTitleBar(mode: 'custom' | 'inset' | 'native', state: WindowState = DEFAULT_STATE) {
  let listener: ((s: WindowState) => void) | null = null
  const unsubscribe = vi.fn(() => {
    listener = null
  })
  const api = {
    mode,
    minimize: vi.fn(async () => true),
    toggleMaximize: vi.fn(async () => ({ ...state, maximized: !state.maximized })),
    close: vi.fn(async () => true),
    getState: vi.fn(async () => state),
    onStateChange: vi.fn((cb: (s: WindowState) => void) => {
      listener = cb
      return unsubscribe
    }),
  }
  return {
    api,
    emit(next: WindowState) {
      listener?.(next)
    },
    unsubscribe,
  }
}

afterEach(() => {
  delete (globalThis as { chironDesktop?: ChironDesktopHost }).chironDesktop
  document.documentElement.style.removeProperty('--app-titlebar-h')
  document.documentElement.style.removeProperty('--app-titlebar-controls-w')
})

describe('TitleBar · 渲染条件', () => {
  it('没有 titleBar（浏览器 / 老外壳）时什么都不渲染', () => {
    installHost(undefined)
    const wrapper = mount(TitleBar)
    expect(wrapper.find('.titlebar').exists()).toBe(false)
    // 也不能给布局留高度
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('')
  })

  it('mode: native（保留系统标题栏的静态页）时什么都不渲染', () => {
    const { api } = fakeTitleBar('native')
    installHost(api)
    const wrapper = mount(TitleBar)
    expect(wrapper.find('.titlebar').exists()).toBe(false)
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('')
  })

  it('mode: custom 渲染三个按钮，并发布标题栏高度', async () => {
    const { api } = fakeTitleBar('custom')
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    expect(wrapper.find('.titlebar--custom').exists()).toBe(true)
    expect(wrapper.findAll('.titlebar-btn')).toHaveLength(3)
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('36px')
    // 三个 46px 的窗口按钮占右侧 138px，页面右上角的操作组要靠这个值躲开
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-controls-w')).toBe('138px')
  })

  it('mode: inset（macOS 红绿灯）只画拖拽带，不画按钮', async () => {
    const { api } = fakeTitleBar('inset')
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    expect(wrapper.find('.titlebar--inset').exists()).toBe(true)
    expect(wrapper.findAll('.titlebar-btn')).toHaveLength(0)
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('28px')
    // 红绿灯在左，右侧不占位：必须发布 0，否则右上角操作组会凭空左移
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-controls-w')).toBe('0px')
  })
})

describe('TitleBar · 窗口控制', () => {
  it('三个按钮分别打到 minimize / toggleMaximize / close', async () => {
    const { api } = fakeTitleBar('custom')
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    const [minimize, toggle, close] = wrapper.findAll('.titlebar-btn')
    await minimize!.trigger('click')
    await toggle!.trigger('click')
    await close!.trigger('click')

    expect(api.minimize).toHaveBeenCalledTimes(1)
    expect(api.toggleMaximize).toHaveBeenCalledTimes(1)
    expect(api.close).toHaveBeenCalledTimes(1)
  })

  it('IPC 抛错时不冒泡到组件（主进程未就绪不该让界面炸掉）', async () => {
    const { api } = fakeTitleBar('custom')
    api.close.mockRejectedValueOnce(new Error('no handler'))
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    const close = wrapper.findAll('.titlebar-btn')[2]!
    await expect(close.trigger('click')).resolves.toBeUndefined()
  })
})

describe('TitleBar · 最大化状态', () => {
  it('首帧从 getState 取状态：已最大化时按钮语义变成「还原」', async () => {
    const { api } = fakeTitleBar('custom', { ...DEFAULT_STATE, maximized: true })
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()
    await nextTick()

    const toggle = wrapper.findAll('.titlebar-btn')[1]!
    expect(toggle.attributes('aria-label')).toBe('向下还原')
    expect(toggle.attributes('title')).toBe('向下还原')
  })

  it('onStateChange 推送的状态会更新图标与文案（Aero Snap 不经过点击）', async () => {
    const { api, emit } = fakeTitleBar('custom')
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    const toggle = () => wrapper.findAll('.titlebar-btn')[1]!
    expect(toggle().attributes('aria-label')).toBe('最大化')

    emit({ ...DEFAULT_STATE, maximized: true })
    await nextTick()
    expect(toggle().attributes('aria-label')).toBe('向下还原')

    emit({ ...DEFAULT_STATE, maximized: false })
    await nextTick()
    expect(toggle().attributes('aria-label')).toBe('最大化')
  })

  it('切换后按返回的状态重绘，而不是自己取反', async () => {
    const { api } = fakeTitleBar('custom')
    // 主进程说「切完还是没最大化」——比如窗口不可最大化。页面必须听它的
    api.toggleMaximize.mockResolvedValueOnce({ ...DEFAULT_STATE, maximized: false })
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    await wrapper.findAll('.titlebar-btn')[1]!.trigger('click')
    await nextTick()
    expect(wrapper.findAll('.titlebar-btn')[1]!.attributes('aria-label')).toBe('最大化')
  })

  it('全屏时禁用最大化按钮（此时最大化没有意义）', async () => {
    const { api } = fakeTitleBar('custom', { ...DEFAULT_STATE, fullScreen: true })
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()
    await nextTick()

    expect(wrapper.findAll('.titlebar-btn')[1]!.attributes('disabled')).toBeDefined()
  })
})

describe('TitleBar · 卸载', () => {
  it('撤销状态订阅并收回标题栏高度', async () => {
    const { api, unsubscribe } = fakeTitleBar('custom')
    installHost(api)
    const wrapper = mount(TitleBar)
    await nextTick()

    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('36px')
    wrapper.unmount()

    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(document.documentElement.style.getPropertyValue('--app-titlebar-h')).toBe('')
  })
})
