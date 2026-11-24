'use strict'
/**
 * 验证码 Provider 的 CSP 白名单（主进程侧副本，PRD H74 / 9.10）。
 *
 * 桌面壳跑在 `file://` 下，主进程 `buildCsp()` 会往响应头里注入 CSP —— 它同样会
 * 拦住验证码脚本与组件 iframe。主进程是 CommonJS、不能 import 前端的 TS 模块，
 * 所以这里放一份**同构的**域名表，由 `src/utils/__tests__/captchaCspConsistency.spec.ts`
 * 比对两边逐源相等（唯一事实来源仍是 `src/utils/captchaProviders.ts`）。
 *
 * 启用开关用环境变量 `CHIRON_CAPTCHA_PROVIDERS`（逗号分隔，未设置 = turnstile，
 * 空串 = 一个都不放行），与前端 `VITE_CAPTCHA_PROVIDERS` 同语义。
 */

const CAPTCHA_PROVIDERS = {
  turnstile: {
    script: ['https://challenges.cloudflare.com'],
    frame: ['https://challenges.cloudflare.com'],
    connect: ['https://challenges.cloudflare.com'],
  },
  recaptcha: {
    script: ['https://www.google.com', 'https://www.gstatic.com'],
    frame: ['https://www.google.com', 'https://www.gstatic.com'],
    connect: ['https://www.google.com', 'https://www.gstatic.com'],
  },
  hcaptcha: {
    script: ['https://js.hcaptcha.com'],
    frame: ['https://js.hcaptcha.com', 'https://newassets.hcaptcha.com'],
    connect: ['https://js.hcaptcha.com', 'https://newassets.hcaptcha.com'],
  },
  tencent: {
    script: ['https://ssl.captcha.qq.com'],
    frame: ['https://ssl.captcha.qq.com', 'https://turing.captcha.qcloud.com'],
    connect: ['https://ssl.captcha.qq.com', 'https://turing.captcha.qcloud.com'],
  },
}

const DEFAULT_ENABLED_PROVIDERS = ['turnstile']

function parseProviderList(raw, fallback = DEFAULT_ENABLED_PROVIDERS) {
  if (raw === undefined || raw === null) return [...fallback]
  const list = String(raw)
    .split(',')
    .map(item => item.trim().toLowerCase())
    .filter(Boolean)
  if (!list.length) return []
  return [...new Set(list)]
}

function enabledProviders(raw = process.env.CHIRON_CAPTCHA_PROVIDERS) {
  return parseProviderList(raw)
}

function buildCaptchaCsp(enabled = enabledProviders()) {
  const script = new Set()
  const frame = new Set()
  const connect = new Set()
  for (const id of enabled) {
    const spec = CAPTCHA_PROVIDERS[id]
    if (!spec) continue
    spec.script.forEach(o => script.add(o))
    spec.frame.forEach(o => frame.add(o))
    spec.connect.forEach(o => connect.add(o))
  }
  // 返回数组而不是 Set：主进程要把它们直接拼进 CSP 字符串
  return { script: [...script], frame: [...frame], connect: [...connect] }
}

module.exports = {
  CAPTCHA_PROVIDERS,
  DEFAULT_ENABLED_PROVIDERS,
  parseProviderList,
  enabledProviders,
  buildCaptchaCsp,
}
