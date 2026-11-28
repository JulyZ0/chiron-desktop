import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MarqueeText from '../MarqueeText.vue'

/**
 * 跑马灯的边界都在"量宽度"上，而 jsdom 没有布局（clientWidth/scrollWidth 恒为 0），
 * 所以这里显式伪造宽度 + 捕获 ResizeObserver 回调手动触发测量。
 */

let roCallback: (() => void) | null = null

class FakeResizeObserver {
  constructor(cb: () => void) {
    roCallback = cb
  }
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

function stubWidths(host: Element, content: Element, hostWidth: number, contentWidth: number): void {
  Object.defineProperty(host, 'clientWidth', { configurable: true, value: hostWidth })
  Object.defineProperty(content, 'scrollWidth', { configurable: true, value: contentWidth })
}

function stubMatchMedia(matches: boolean): void {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })),
  })
}

beforeEach(() => {
  roCallback = null
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  stubMatchMedia(false)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MarqueeText', () => {
  it('短文本保持静态：不复制、不挂动画', async () => {
    const wrapper = mount(MarqueeText, { props: { text: '短标签' } })
    stubWidths(wrapper.find('.marquee').element, wrapper.find('.marquee__text').element, 160, 80)
    roCallback?.()
    await flushPromises()

    expect(wrapper.classes()).not.toContain('is-scrolling')
    expect(wrapper.findAll('.marquee__text')).toHaveLength(1)
  })

  it('溢出时复制一份并挂滚动轨道，时长按宽度换算', async () => {
    const wrapper = mount(MarqueeText, { props: { text: '一个特别特别长的项目名 · 一个特别长的会话名' } })
    const host = wrapper.find('.marquee').element
    const content = wrapper.find('.marquee__text').element
    stubWidths(host, content, 120, 480)
    roCallback?.()
    await flushPromises()

    expect(wrapper.classes()).toContain('is-scrolling')
    // 第二份只服务视觉循环，读屏必须跳过
    const texts = wrapper.findAll('.marquee__text')
    expect(texts).toHaveLength(2)
    expect(texts[1].attributes('aria-hidden')).toBe('true')
    // 480px / 32px·s⁻¹ = 15s
    expect((host as HTMLElement).style.getPropertyValue('--marquee-dur')).toBe('15s')
  })

  it('系统减少动效时：即使溢出也不滚动', async () => {
    stubMatchMedia(true)
    const wrapper = mount(MarqueeText, { props: { text: '一个特别特别长的项目名 · 一个特别长的会话名' } })
    stubWidths(wrapper.find('.marquee').element, wrapper.find('.marquee__text').element, 120, 480)
    roCallback?.()
    await flushPromises()

    expect(wrapper.classes()).not.toContain('is-scrolling')
    expect(wrapper.findAll('.marquee__text')).toHaveLength(1)
  })
})
