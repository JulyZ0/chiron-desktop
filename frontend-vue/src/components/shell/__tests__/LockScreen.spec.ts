import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import LockScreen from '../LockScreen.vue'
import { useAuthStore } from '../../../stores/auth'
import { setActiveMode } from '../../../stores/device'

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

/**
 * 被动锁定：系统锁屏 / 休眠时主进程推一条信号，界面必须真的锁上。
 *
 * 这是用户报的"两步验证启用形同虚设"的第二条根因。在此之前 `locked` 只有一个
 * 置真来源（用户主动点菜单），而共用工作站上真正危险的场景是**人走开、屏幕锁上、
 * 回来的是另一个人** —— 那一刻没有任何人点菜单，于是启用过的 TOTP 从未被问到。
 *
 * 订阅挂在锁屏组件上（它在 App.vue 里常驻），所以这里同时钉住退订：
 * 卸载之后再来信号，不许还去改 store。
 */
describe('LockScreen · 被动锁定', () => {
  const originalHost = (globalThis as { chironDesktop?: unknown }).chironDesktop
  const originalMode = 'local'

  afterEach(() => {
    setActiveMode(null)
    if (originalHost === undefined) delete (globalThis as { chironDesktop?: unknown }).chironDesktop
    else (globalThis as { chironDesktop?: unknown }).chironDesktop = originalHost
  })

  /** 装一个只会推被动锁定信号的假外壳；返回"推信号"的入口 */
  function installAutoLockHost(): { fire: (reason?: string) => void; unsubscribed: () => number } {
    let callback: ((payload: { reason?: string }) => void) | null = null
    let unsubscribeCount = 0
    ;(globalThis as { chironDesktop?: unknown }).chironDesktop = {
      isDesktop: true,
      mode: 'local',
      onAutoLock: (cb: (payload: { reason?: string }) => void) => {
        callback = cb
        return () => {
          unsubscribeCount += 1
          callback = null
        }
      },
    }
    return {
      fire: (reason?: string) => callback?.(reason ? { reason } : {}),
      unsubscribed: () => unsubscribeCount,
    }
  }

  it('★ 收到信号后界面锁上（人走开后回来的人必须重新验证）', async () => {
    setActiveMode(originalMode)
    const host = installAutoLockHost()
    wrapper = mount(LockScreen)
    await nextTick()

    const auth = useAuthStore()
    expect(auth.locked).toBe(false)

    host.fire('lock-screen')
    await nextTick()

    expect(auth.locked).toBe(true)
    // 遮罩真的出现在 DOM 上：只改 store 而遮罩不渲染，等于没锁
    expect(document.querySelector('[data-test="lock-overlay"]')).not.toBeNull()
  })

  it('外壳没有这条通道时不崩，也不凭空锁定（老外壳照常可用）', async () => {
    setActiveMode(originalMode)
    ;(globalThis as { chironDesktop?: unknown }).chironDesktop = { isDesktop: true, mode: 'local' }
    wrapper = mount(LockScreen)
    await nextTick()

    expect(useAuthStore().locked).toBe(false)
    expect(document.querySelector('[data-test="lock-overlay"]')).toBeNull()
  })

  it('卸载时退订：此后的信号不许再改 store', async () => {
    setActiveMode(originalMode)
    const host = installAutoLockHost()
    wrapper = mount(LockScreen)
    await nextTick()

    wrapper.unmount()
    wrapper = null
    expect(host.unsubscribed()).toBe(1)

    host.fire('resume')
    await nextTick()
    expect(useAuthStore().locked).toBe(false)
  })
})
