/**
 * 验证码 Provider 启用开关的行为契约（PRD H74 / 9.10）。
 *
 * 钉两件事：
 *  1. **未启用就一个第三方请求都不发**（不插 `<script>`、没有 iframe）——「先加载
 *     再看 CSP 拦不拦」是最难查的线上形态：控制台只有一条 violation，界面上是空白；
 *  2. 启用后脚本地址与 CSP 白名单同源（脚本真的能从放行的域名加载出来），并且
 *     token 回调这条链路仍然通（smoke test：脚本 → render → callback → emit）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import CaptchaWidget from '../CaptchaWidget.vue'
import { CAPTCHA_PROVIDERS } from '../../utils/captchaProviders'

const HCAPTCHA_SRC = CAPTCHA_PROVIDERS.hcaptcha.scriptUrls[0]

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/** 预置成「已加载」，避免 jsdom 真去拉 CDN */
function presetScript(src: string) {
  const s = document.createElement('script')
  s.src = src
  s.dataset.loaded = '1'
  document.head.appendChild(s)
  return s
}

function installFakeRenderer(globalName: 'hcaptcha' | 'turnstile') {
  const calls: Array<{ sitekey: string; opts: Record<string, unknown> }> = []
  const fake = {
    render: vi.fn((_el: HTMLElement, opts: { sitekey: string } & Record<string, unknown>) => {
      calls.push({ sitekey: opts.sitekey, opts })
      return 'w-1'
    }),
    reset: vi.fn(),
    remove: vi.fn(),
  }
  ;(window as unknown as Record<string, unknown>)[globalName] = fake
  return { fake, calls }
}

function cleanup() {
  document.head.querySelectorAll('script').forEach(s => s.remove())
  delete (window as unknown as Record<string, unknown>).hcaptcha
  delete (window as unknown as Record<string, unknown>).turnstile
}

beforeEach(cleanup)

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('CaptchaWidget —— 启用开关', () => {
  it('★ 未启用的 provider：不插任何第三方脚本，并如实报错', async () => {
    vi.stubEnv('VITE_CAPTCHA_PROVIDERS', 'turnstile')
    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'hcaptcha', siteKey: 'site-key' },
    })
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(document.head.querySelectorAll('script')).toHaveLength(0)
    expect(wrapper.find('.captcha-error').exists()).toBe(true)
    expect(wrapper.emitted('verified')).toBeUndefined()
  })

  it('显式空串（全关）时连默认的 turnstile 也不加载', async () => {
    vi.stubEnv('VITE_CAPTCHA_PROVIDERS', '')
    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'turnstile', siteKey: 'site-key' },
    })
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(document.head.querySelectorAll('script')).toHaveLength(0)
    expect(wrapper.find('.captcha-error').exists()).toBe(true)
  })

  it('★ 启用 hcaptcha：脚本地址来自白名单，token 回调仍然通', async () => {
    vi.stubEnv('VITE_CAPTCHA_PROVIDERS', 'hcaptcha')
    const preset = presetScript(HCAPTCHA_SRC)
    const { fake, calls } = installFakeRenderer('hcaptcha')

    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'hcaptcha', siteKey: 'site-key-42' },
    })
    await vi.waitFor(() => expect(fake.render).toHaveBeenCalled(), { timeout: 2000 })

    // 脚本确实是从 CSP 白名单里的域名加载的（否则真实环境会被 frame/script-src 拦）
    const origin = new URL(preset.src, window.location.href).origin
    expect(CAPTCHA_PROVIDERS.hcaptcha.script).toContain(origin)

    expect(calls[0]?.sitekey).toBe('site-key-42')
    const opts = calls[0]!.opts as { callback?: (t: string) => void; 'expired-callback'?: () => void }
    opts.callback?.('token-xyz')
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('verified')?.[0]?.[0]).toEqual({ token: 'token-xyz' })

    opts['expired-callback']?.()
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('expired')).toHaveLength(1)
  })

  it('custom 永远放行，但绝不加载第三方脚本（也不伪装已验证）', async () => {
    vi.stubEnv('VITE_CAPTCHA_PROVIDERS', '')
    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'custom', siteKey: '', verifyUrl: 'https://verify.example.com' },
    })
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(document.head.querySelectorAll('script')).toHaveLength(0)
    expect(wrapper.emitted('verified')).toBeUndefined()
    expect(wrapper.text()).toContain('https://verify.example.com')
  })

  it('未启用时的提示文案点名了启用开关（部署方要知道改哪里）', async () => {
    vi.stubEnv('VITE_CAPTCHA_PROVIDERS', '')
    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'recaptcha', siteKey: 'site-key' },
    })
    await flushPromises()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('VITE_CAPTCHA_PROVIDERS')
  })
})
