import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import LockScreen from '../LockScreen.vue'
import { useAuthStore } from '../../../stores/auth'

/**
 * 锁屏按键拦截的测试。
 *
 * 这个组件在 `App.vue` 里**常驻挂载**（锁屏要能在任何页面立刻盖上来），所以它
 * 在 window 捕获阶段注册的 `swallowKeys` 从应用启动的第一秒就在生效。曾经漏了
 * "是否真的锁着"这道判断，结果是：遮罩没出现，但全站每一个按键都被
 * `preventDefault()` 吞掉 —— 所有输入框（两步验证的验证码、搜索框、聊天框）
 * 一起打不进字，用户完全看不出是锁屏干的。
 *
 * 所以这里钉两条：
 *   1. 未锁定时**一个键都不许碰**；
 *   2. 锁定后才拦截 —— 但遮罩内两个输入框里的按键必须放行，否则连密码都输不进去。
 */

let wrapper: ReturnType<typeof mount> | null = null

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

/** 派发一个可从事件对象上读到"是否被拦截"的 keydown */
function pressKey(target: EventTarget = document.body): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })
  target.dispatchEvent(event)
  return event
}

describe('LockScreen · 按键拦截', () => {
  it('未锁定时不拦截任何按键（否则全站输入框都失效）', async () => {
    wrapper = mount(LockScreen)
    await nextTick()

    const auth = useAuthStore()
    expect(auth.locked).toBe(false)

    const event = pressKey()
    expect(event.defaultPrevented).toBe(false)
  })

  it('锁定时拦截遮罩外的按键', async () => {
    wrapper = mount(LockScreen)
    await nextTick()
    useAuthStore().locked = true
    await nextTick()

    // 遮罩已渲染
    expect(document.querySelector('[data-test="lock-overlay"]')).not.toBeNull()

    const event = pressKey()
    expect(event.defaultPrevented).toBe(true)
  })

  it('锁定时放行密码框内的按键', async () => {
    wrapper = mount(LockScreen)
    await nextTick()
    useAuthStore().locked = true
    await nextTick()

    const input = document.querySelector('[data-test="lock-input"]') as HTMLInputElement
    expect(input).not.toBeNull()

    const event = pressKey(input)
    expect(event.defaultPrevented).toBe(false)
  })

  it('解锁后不再拦截按键', async () => {
    wrapper = mount(LockScreen)
    await nextTick()
    const auth = useAuthStore()
    auth.locked = true
    await nextTick()
    expect(pressKey().defaultPrevented).toBe(true)

    auth.locked = false
    await nextTick()
    expect(pressKey().defaultPrevented).toBe(false)
  })
})
