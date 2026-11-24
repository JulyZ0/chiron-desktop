/**
 * 验证码 Provider 的最小 CSP 白名单与启用开关（PRD H74 / 9.10）。
 *
 * 背景：`CaptchaWidget` 会动态插入各家验证码 CDN 的 `<script>`，验证码组件本身又是
 * 一个跨域 `<iframe>`，令牌回调还走 `connect-src`。而部署侧 CSP 是
 * `script-src 'self'` 且**没有** `frame-src` —— 于是：
 *   - 没配 CSP 时能用的验证码，配了 CSP 之后静默加载失败；
 *   - 为了让验证码能用而随手放开 `*.cloudflare.com`/`*.google.com` 的整段通配，
 *     又把 XSS 的外发通道开回去了。
 *
 * 这个模块是**唯一事实来源**：每个 provider 需要放行哪些源、默认是否启用，都写在这里。
 *   - `security-headers.conf` 的 CSP 必须与 `buildCaptchaCsp(conf 里声明的 provider)`
 *     逐源一致（由 `captchaCspConsistency.spec.ts` 强制）；
 *   - Electron 侧 `electron/captcha-csp.cjs` 是同一份表的 CJS 副本，测试比对两边相等
 *     （主进程不能 import TS，只能用副本 + 测试拴住）。
 *
 * 启用开关是 `VITE_CAPTCHA_PROVIDERS`（逗号分隔）：
 *   - 未设置 → 只用 `DEFAULT_ENABLED_PROVIDERS`（turnstile）；
 *   - 显式空串 → 一个都不放行（想彻底关掉验证码时用）；
 *   - 未启用的 provider 在 `CaptchaWidget` 里**连脚本都不加载**，不放行任何第三方域名。
 */

/** 一个 provider 需要放行的最小域名集合（不含协议通配、不含 `*`）。 */
export interface CaptchaCspOrigins {
  /** 需要插入的脚本自身（`script-src`） */
  script: string[]
  /** 验证码组件 iframe（`frame-src`） */
  frame: string[]
  /** 令牌/挑战接口（`connect-src`） */
  connect: string[]
}

export interface CaptchaProviderSpec extends CaptchaCspOrigins {
  id: string
  /** 需要动态加载的脚本 URL */
  scriptUrls: string[]
}

/**
 * 各家官方文档给出的最小域名集。
 *
 * 刻意不写 `https:` / `*.cloudflare.com`：验证码只需要这几个精确 host，多放一个
 * 都是给 XSS 留外发口。
 */
export const CAPTCHA_PROVIDERS: Record<string, CaptchaProviderSpec> = {
  turnstile: {
    id: 'turnstile',
    scriptUrls: ['https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'],
    script: ['https://challenges.cloudflare.com'],
    frame: ['https://challenges.cloudflare.com'],
    connect: ['https://challenges.cloudflare.com'],
  },
  recaptcha: {
    id: 'recaptcha',
    scriptUrls: ['https://www.google.com/recaptcha/api.js?render=explicit'],
    // reCAPTCHA 的静态资源与 challenge iframe 都在 gstatic 上
    script: ['https://www.google.com', 'https://www.gstatic.com'],
    frame: ['https://www.google.com', 'https://www.gstatic.com'],
    connect: ['https://www.google.com', 'https://www.gstatic.com'],
  },
  hcaptcha: {
    id: 'hcaptcha',
    scriptUrls: ['https://js.hcaptcha.com/1/api.js?render=explicit'],
    script: ['https://js.hcaptcha.com'],
    frame: ['https://js.hcaptcha.com', 'https://newassets.hcaptcha.com'],
    connect: ['https://js.hcaptcha.com', 'https://newassets.hcaptcha.com'],
  },
  tencent: {
    id: 'tencent',
    scriptUrls: ['https://ssl.captcha.qq.com/TCaptcha.js'],
    script: ['https://ssl.captcha.qq.com'],
    frame: ['https://ssl.captcha.qq.com', 'https://turing.captcha.qcloud.com'],
    connect: ['https://ssl.captcha.qq.com', 'https://turing.captcha.qcloud.com'],
  },
}

/** 默认启用集合：与 `security-headers.conf` 里 `# captcha-providers:` 声明一致。 */
export const DEFAULT_ENABLED_PROVIDERS: string[] = ['turnstile']

/** 启用开关的 env 名（也写进部署文档与 conf 注释里）。 */
export const CAPTCHA_PROVIDERS_ENV = 'VITE_CAPTCHA_PROVIDERS'

/** 解析逗号分隔的 provider 列表；`undefined` 用默认值，空串表示「一个都不放行」。 */
export function parseProviderList(
  raw: string | null | undefined,
  fallback: readonly string[] = DEFAULT_ENABLED_PROVIDERS,
): string[] {
  if (raw === undefined || raw === null) return [...fallback]
  const list = raw
    .split(',')
    .map(item => item.trim().toLowerCase())
    .filter(Boolean)
  if (!list.length) return []
  return [...new Set(list)]
}

/** 当前构建启用的 provider 集合。 */
export function enabledCaptchaProviders(
  env: Record<string, unknown> | undefined = import.meta.env as unknown as Record<string, unknown>,
): string[] {
  const raw = env?.[CAPTCHA_PROVIDERS_ENV]
  return parseProviderList(typeof raw === 'string' ? raw : undefined)
}

/**
 * 某个 provider 是否被部署方启用。
 *
 * `custom` 永远放行：它是部署方自己的前端组件，不加载任何第三方脚本，没有域名要放行。
 */
export function isCaptchaProviderEnabled(
  provider: string,
  env?: Record<string, unknown>,
): boolean {
  if (provider === 'custom') return true
  return enabledCaptchaProviders(env).includes(provider)
}

/** 该 provider 的脚本 URL（未启用的 provider 不返回任何东西）。 */
export function captchaScriptUrl(provider: string): string | null {
  return CAPTCHA_PROVIDERS[provider]?.scriptUrls[0] ?? null
}

/** 按启用集合汇总出需要放行的源集合（去重、保持稳定顺序）。 */
export function buildCaptchaCsp(enabled: readonly string[]): CaptchaCspOrigins {
  const script = new Set<string>()
  const frame = new Set<string>()
  const connect = new Set<string>()
  for (const id of enabled) {
    const spec = CAPTCHA_PROVIDERS[id]
    if (!spec) continue
    spec.script.forEach(o => script.add(o))
    spec.frame.forEach(o => frame.add(o))
    spec.connect.forEach(o => connect.add(o))
  }
  return { script: [...script], frame: [...frame], connect: [...connect] }
}

/**
 * 生成可直接粘进 nginx / meta 标签的 CSP 片段（只含验证码相关的三条指令）。
 *
 * 部署配置必须与**实际启用的 provider** 一致：多列是白放开，漏列就是验证码静默失败。
 */
export function renderCaptchaCspDirectives(enabled: readonly string[] = enabledCaptchaProviders()): string[] {
  const { script, frame, connect } = buildCaptchaCsp(enabled)
  const directives: string[] = []
  if (script.length) directives.push(`script-src 'self' ${script.join(' ')}`)
  if (frame.length) directives.push(`frame-src 'self' blob: ${frame.join(' ')}`)
  if (connect.length) directives.push(`connect-src 'self' ws: wss: ${connect.join(' ')}`)
  return directives
}
