/**
 * 验证码 Provider ↔ 部署 CSP 一致性（PRD H74 / 9.10）。
 *
 * 这条链路有三个独立配置点，任何一处漂移都会以「验证码框是空白」这种**静默**方式
 * 暴露（控制台只有一条 CSP violation，用户只看到提交不了）：
 *
 *   1. `src/utils/captchaProviders.ts`   —— 前端唯一事实来源（脚本 + 最小域名集）；
 *   2. `security-headers.conf`           —— nginx 侧 CSP（Web 部署）；
 *   3. `electron/captcha-csp.cjs` + `main.cjs` —— 桌面壳 `file://` 下注入的 CSP。
 *
 * 本文件把三者钉在一起：
 *   - 部署声明了哪些 provider，CSP 就必须**逐源**放行它们；
 *   - 没声明的 provider，其域名**一个都不许出现**（未启用不放行第三方域名）；
 *   - Electron 的域名表与前端表逐源相等，且 `buildCsp()` 真的用了它。
 */
import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import {
  CAPTCHA_PROVIDERS,
  DEFAULT_ENABLED_PROVIDERS,
  buildCaptchaCsp,
  enabledCaptchaProviders,
  isCaptchaProviderEnabled,
  parseProviderList,
} from '../captchaProviders'

const require = createRequire(import.meta.url)

/**
 * 找文件不依赖 `import.meta.url`（vitest 下不同 pool 给的值形态不一样）：
 * 依次试「当前工作目录」与「仓库根/frontend-vue」。
 */
function repoFile(rel: string): string {
  const candidates = [
    path.resolve(process.cwd(), rel),
    path.resolve(process.cwd(), 'frontend-vue', rel),
  ]
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  throw new Error(`找不到 ${rel}（试过：${candidates.join(', ')}）`)
}

const confText = readFileSync(repoFile('security-headers.conf'), 'utf8')
const mainText = readFileSync(repoFile('electron/main.cjs'), 'utf8')
const electronCsp = require(repoFile('electron/captcha-csp.cjs')) as {
  CAPTCHA_PROVIDERS: Record<string, { script: string[]; frame: string[]; connect: string[] }>
  DEFAULT_ENABLED_PROVIDERS: string[]
  parseProviderList: (raw?: string | null, fallback?: readonly string[]) => string[]
  enabledProviders: (raw?: string | null) => string[]
  buildCaptchaCsp: (enabled?: readonly string[]) => { script: string[]; frame: string[]; connect: string[] }
}

/** conf 里声明的实际启用服务商（机器可读标记行） */
function declaredProviders(conf: string): string[] {
  const match = /^#\s*captcha-providers:\s*(.*)$/m.exec(conf)
  if (!match) return []
  return match[1]
    .split(',')
    .map(item => item.trim().toLowerCase())
    .filter(Boolean)
}

/** 把 nginx 的 CSP 头拆成 指令 → 取值列表 */
function cspDirectives(conf: string): Record<string, string[]> {
  const match = /add_header Content-Security-Policy "([^"]+)" always;/.exec(conf)
  if (!match) throw new Error('security-headers.conf 里找不到 CSP 头')
  const directives: Record<string, string[]> = {}
  for (const rawPart of match[1].split(';')) {
    const part = rawPart.trim()
    if (!part) continue
    const [name, ...values] = part.split(/\s+/)
    directives[name] = values
  }
  return directives
}

const declared = declaredProviders(confText)
const directives = cspDirectives(confText)

/** 一个域名出现在任何一条指令里都算「放行」 */
function allowedAnywhere(origin: string): string[] {
  return Object.entries(directives)
    .filter(([, values]) => values.includes(origin))
    .map(([name]) => name)
}

describe('CSP 与「实际启用的 provider」一致', () => {
  it('conf 必须声明启用集合，且默认集合与前端一致', () => {
    expect(declared.length).toBeGreaterThan(0)
    expect([...declared].sort()).toEqual([...DEFAULT_ENABLED_PROVIDERS].sort())
  })

  it('★ 声明的 provider：script / frame / connect 三面都要逐源放行', () => {
    for (const id of declared) {
      const spec = CAPTCHA_PROVIDERS[id]
      expect(spec, `conf 声明了未知 provider: ${id}`).toBeTruthy()
      expect(directives['script-src'], `${id} script-src`).toEqual(expect.arrayContaining(spec.script))
      // frame-src 缺了，就算脚本加载成功验证码框也是空白
      expect(directives['frame-src'], `${id} frame-src`).toEqual(expect.arrayContaining(spec.frame))
      expect(directives['connect-src'], `${id} connect-src`).toEqual(expect.arrayContaining(spec.connect))
    }
  })

  it('★ 未启用的 provider：其域名一个都不许出现（不放行第三方域名）', () => {
    const disabled = Object.keys(CAPTCHA_PROVIDERS).filter(id => !declared.includes(id))
    expect(disabled.length).toBeGreaterThan(0) // 至少有一个是关着的，否则这条断言没意义
    for (const id of disabled) {
      const spec = CAPTCHA_PROVIDERS[id]
      for (const origin of [...spec.script, ...spec.frame, ...spec.connect]) {
        expect(allowedAnywhere(origin), `${id} 未启用但 ${origin} 被放行`).toEqual([])
      }
    }
  })

  it('CSP 里没有验证码相关的通配 / 整协议放开', () => {
    const cspLine = /add_header Content-Security-Policy "([^"]+)"/.exec(confText)![1]
    // 只检查与验证码有关的域名后缀；别的指令（img-src https:）另算
    for (const directive of ['script-src', 'frame-src', 'connect-src'] as const) {
      const values = directives[directive] ?? []
      expect(values, directive).not.toContain('*')
      expect(values, directive).not.toContain('https:')
      for (const value of values) {
        if (!value.startsWith('http')) continue
        expect(value, `${directive} 的 ${value} 必须是无路径的源`).toMatch(/^https:\/\/[^/]+$/)
      }
    }
    expect(cspLine).toContain("object-src 'none'")
  })

  it('renderCaptchaCspDirectives 与 conf 的取值完全对应', () => {
    const built = buildCaptchaCsp(declared)
    expect(directives['script-src']).toEqual(expect.arrayContaining(["'self'", ...built.script]))
    expect(directives['frame-src']).toEqual(expect.arrayContaining(["'self'", 'blob:', ...built.frame]))
    expect(directives['connect-src']).toEqual(
      expect.arrayContaining(["'self'", 'ws:', 'wss:', ...built.connect]),
    )
  })
})

describe('Electron 侧与前端共用同一份域名表', () => {
  it('cjs 表的每个 provider 与 TS 表逐源相等', () => {
    expect(Object.keys(electronCsp.CAPTCHA_PROVIDERS).sort()).toEqual(
      Object.keys(CAPTCHA_PROVIDERS).sort(),
    )
    for (const [id, spec] of Object.entries(CAPTCHA_PROVIDERS)) {
      const mirror = electronCsp.CAPTCHA_PROVIDERS[id]
      expect(mirror.script, `${id}.script`).toEqual(spec.script)
      expect(mirror.frame, `${id}.frame`).toEqual(spec.frame)
      expect(mirror.connect, `${id}.connect`).toEqual(spec.connect)
    }
  })

  it('默认启用集合一致，解析语义一致（空串 = 全关）', () => {
    expect([...electronCsp.DEFAULT_ENABLED_PROVIDERS].sort()).toEqual(
      [...DEFAULT_ENABLED_PROVIDERS].sort(),
    )
    for (const raw of [undefined, '', 'hcaptcha,Turnstile', ' turnstile , ']) {
      expect(electronCsp.parseProviderList(raw)).toEqual(parseProviderList(raw))
    }
  })

  it('buildCsp 真的用了 captcha-csp（否则改表不会生效）', () => {
    expect(mainText).toContain("require('./captcha-csp.cjs')")
    expect(mainText).toContain('captchaCsp.buildCaptchaCsp()')
    // script-src 仍保留本地来源与 wasm 许可（不能因为拼验证码把桌面壳自身弄坏）
    expect(mainText).toContain("'wasm-unsafe-eval'")
  })
})

describe('启用开关语义', () => {
  it('未设置 → 默认集合（turnstile）', () => {
    expect(enabledCaptchaProviders({})).toEqual(DEFAULT_ENABLED_PROVIDERS)
    expect(isCaptchaProviderEnabled('turnstile', {})).toBe(true)
  })

  it('显式空串 → 一个都不放行', () => {
    expect(enabledCaptchaProviders({ VITE_CAPTCHA_PROVIDERS: '' })).toEqual([])
    expect(isCaptchaProviderEnabled('turnstile', { VITE_CAPTCHA_PROVIDERS: '' })).toBe(false)
  })

  it('大小写 / 空格 / 重复都规整', () => {
    expect(enabledCaptchaProviders({ VITE_CAPTCHA_PROVIDERS: ' HCaptcha , turnstile,hcaptcha ' }))
      .toEqual(['hcaptcha', 'turnstile'])
  })

  it('只启用 hcaptcha 时，其它 provider 的域名不进入 CSP', () => {
    const built = buildCaptchaCsp(['hcaptcha'])
    expect(built.script).toEqual(['https://js.hcaptcha.com'])
    expect(built.frame).not.toContain('https://challenges.cloudflare.com')
    expect(built.connect).not.toContain('https://www.google.com')
  })

  it('custom 永远放行（它不加载任何第三方脚本）', () => {
    expect(isCaptchaProviderEnabled('custom', { VITE_CAPTCHA_PROVIDERS: '' })).toBe(true)
    expect(CAPTCHA_PROVIDERS.custom).toBeUndefined()
  })

  it('每个 provider 的脚本地址都落在自己的 script-src 白名单里', () => {
    for (const [id, spec] of Object.entries(CAPTCHA_PROVIDERS)) {
      expect(spec.scriptUrls.length, id).toBeGreaterThan(0)
      for (const url of spec.scriptUrls) {
        const origin = new URL(url).origin
        expect(spec.script, `${id} 的脚本 ${url} 不在白名单`).toContain(origin)
      }
    }
  })
})
