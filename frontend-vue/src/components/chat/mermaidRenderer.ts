import DOMPurify from 'dompurify'
import { t } from '../../i18n'

/**
 * mermaid 图表渲染（把 ```mermaid 代码块变成 SVG）。
 *
 * 两点取舍：
 * 1. **懒加载**：mermaid 体积数百 KB，只在页面真的出现图表块时才 `import()`。
 * 2. **失败不吞内容**：渲染失败时保留原始源码并标注失败原因 —— 图表语法有误是
 *    常见情况（模型写的图偶尔不合法），此时把源码展示出来比留白有用得多。
 *
 * 加载器可注入（`setMermaidAdapter`）：渲染契约能在测试里验证，而不必依赖
 * 图库本身在 jsdom 中是否可用。
 */

export interface MermaidAdapter {
  initialize(options: Record<string, unknown>): void
  render(id: string, code: string): Promise<{ svg: string }>
}

export interface RenderOptions {
  /** 暗色主题下用 mermaid 的 dark 主题，否则图表配色与页面不搭 */
  dark: boolean
}

let injected: MermaidAdapter | null = null
let loader: Promise<MermaidAdapter> | null = null
let seq = 0

/** 测试注入替身；传 null 恢复真实懒加载 */
export function setMermaidAdapter(adapter: MermaidAdapter | null): void {
  injected = adapter
  loader = null
}

function loadAdapter(): Promise<MermaidAdapter> {
  if (injected) return Promise.resolve(injected)
  if (!loader) {
    loader = import('mermaid')
      .then(mod => ((mod as { default?: MermaidAdapter }).default ?? (mod as unknown as MermaidAdapter)))
      .catch((err) => {
        loader = null   // 允许下次重试（临时失败不该被永久放弃）
        throw err
      })
  }
  return loader
}

/* ────────────────────────── 二次消毒（H63） ──────────────────────────
 *
 * mermaid 已配置 `securityLevel: 'strict'`，但那是**库内部实现细节**：版本升级、
 * 配置漂移或换渲染器都可能让那层过滤变松。图内容直接来自模型输出（可被提示注入
 * 影响），所以写 DOM 前再剥一遍。
 *
 * 与旧实现（只删 `<script>` / `on*` / 两类 href）的差别：
 *  1. 走 **DOMPurify 的 SVG/MathML profile** —— 用成熟 sanitizer 处理 mXSS 归一化
 *     （HTML 实体、命名空间混淆、`<svg><style><img>` 之类）而不是自己写正则；
 *  2. **标签与属性白名单**：只保留 mermaid 实际产出、且无脚本语义的元素/属性；
 *  3. **协议规范化后校验**：去掉控制字符/零宽字符、统一小写后再判协议，避免
 *     `jav\tascript:`、`&#106;avascript:` 这类绕过；
 *  4. **禁用 foreignObject 与内联 CSS 的攻击面**：foreignObject 整棵子树丢弃
 *     （securityLevel='strict' 下 mermaid 不会用它排 HTML），`style` 属性与
 *     `<style>` 块按属性白名单重写，去掉 `url()` / `expression` / `@import`；
 *  5. 消毒后如果根节点不是 `<svg>`、或仍出现 `<script>`，整体放弃（退回源码展示）。
 *
 * 严格 CSP（部署侧 `script-src 'self'`、`object-src 'none'`、无 `unsafe-inline`
 * 脚本）是这一层的兜底：即使本文件被绕过，内联脚本也没有执行许可。
 */

/** mermaid 输出里允许保留的元素。全部小写，另按原样大小写双份展开见 `expandCase`。 */
const SVG_ALLOWED_TAGS = [
  'svg', 'g', 'defs', 'desc', 'title', 'symbol', 'use', 'marker', 'pattern', 'style',
  'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
  'text', 'tspan', 'textPath',
  'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'a',
  'filter', 'feGaussianBlur', 'feOffset', 'feBlend', 'feColorMatrix',
  'feMerge', 'feMergeNode', 'feFlood', 'feComposite', 'feDropShadow',
  // MathML 子集：mermaid 的数学公式走 KaTeX，会产出这些（不含 annotation-xml 等 mXSS 常用点）
  'math', 'mrow', 'mi', 'mo', 'mn', 'ms', 'mtext', 'mspace', 'msub', 'msup',
  'msubsup', 'mfrac', 'msqrt', 'mroot', 'mover', 'munder', 'munderover',
  'mtable', 'mtr', 'mtd',
] as const

/** 允许保留的属性。交互/资源加载类（src、target、formaction…）一律不在表内。 */
const SVG_ALLOWED_ATTRS = [
  'id', 'class', 'role', 'aria-label', 'aria-hidden', 'aria-roledescription',
  'xmlns', 'xmlns:xlink', 'version', 'xml:space', 'transform',
  'viewBox', 'preserveAspectRatio',
  'd', 'points', 'pathLength', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'dx', 'dy', 'rotate',
  'cx', 'cy', 'r', 'rx', 'ry', 'fx', 'fy', 'width', 'height', 'offset',
  'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
  'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset',
  'stroke-miterlimit', 'opacity', 'color', 'paint-order', 'shape-rendering',
  'vector-effect', 'visibility', 'display', 'clip-path', 'clip-rule', 'mask',
  'marker-start', 'marker-mid', 'marker-end', 'markerWidth', 'markerHeight',
  'markerUnits', 'refX', 'refY', 'orient', 'gradientUnits', 'gradientTransform',
  'patternUnits', 'patternContentUnits', 'clipPathUnits', 'maskUnits',
  'stop-color', 'stop-opacity', 'filter', 'filterUnits',
  'font-family', 'font-size', 'font-weight', 'font-style', 'letter-spacing',
  'word-spacing', 'text-anchor', 'dominant-baseline', 'alignment-baseline',
  'text-decoration', 'textLength', 'lengthAdjust',
  'href', 'xlink:href', 'rel', 'target',
  'data-id', 'data-et', 'data-points', 'data-type', 'data-look',
] as const

/**
 * 内联 CSS 属性白名单：只允许**视觉**属性。
 * 定位类（position/top/left/z-index）与资源类（background-image/url()）一律不放行 ——
 * 前者能做 UI 覆盖，后者能做外带。
 */
const SVG_ALLOWED_CSS_PROPS = new Set([
  'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
  'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset',
  'stroke-miterlimit', 'opacity', 'color', 'font-family', 'font-size', 'font-weight',
  'font-style', 'letter-spacing', 'word-spacing', 'text-anchor', 'text-decoration',
  'dominant-baseline', 'alignment-baseline', 'shape-rendering', 'paint-order',
  'visibility', 'display', 'vector-effect', 'stop-color', 'stop-opacity',
])

/** 允许的 URI 协议（相对地址 / 片段没有协议，另行放行）。 */
const URI_ALLOWED_PROTOCOLS = new Set(['http:', 'https:', 'mailto:'])

const URI_ATTRS = new Set(['href', 'xlink:href', 'src', 'action', 'formaction', 'poster', 'cite'])

/** 大小写双份展开：SVG 在 HTML/XHTML 两种解析路径下大小写不同，白名单两边都认。 */
function expandCase(names: readonly string[]): string[] {
  const out = new Set<string>()
  for (const n of names) {
    out.add(n)
    out.add(n.toLowerCase())
  }
  return [...out]
}

const ALLOWED_TAGS = expandCase(SVG_ALLOWED_TAGS)
const ALLOWED_ATTRS = expandCase(SVG_ALLOWED_ATTRS)
const ALLOWED_TAG_SET = new Set(ALLOWED_TAGS.map(n => n.toLowerCase()))
const ALLOWED_ATTR_SET = new Set(ALLOWED_ATTRS.map(n => n.toLowerCase()))

/**
 * 规范化 URI 再判协议。
 *
 * 先去掉所有「看不见」的字符（C0 控制符、DEL/C1、零宽、双向控制、换行）——
 * 浏览器解析 `java\tscript:` 时会把这些字符吞掉，只对原串做正则就会被绕过。
 */
function normalizeUri(raw: string): string {
  return raw
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0020\u007f-\u00a0\u200b-\u200f\u202a-\u202e\u2060\ufeff]/g, '')
    .toLowerCase()
}

function isSafeUri(raw: string): boolean {
  const value = normalizeUri(raw)
  if (!value) return false
  const scheme = /^([a-z][a-z0-9+.-]*):/.exec(value)
  if (!scheme) return true // 相对路径 / #fragment
  const proto = `${scheme[1]}:`
  if (proto === 'data:') {
    // 只允许图片数据（图标、图案）；data:text/html 与 data:image/svg+xml 都是执行面
    return /^data:image\/(png|jpe?g|gif|webp|bmp)[;,]/.test(value)
  }
  return URI_ALLOWED_PROTOCOLS.has(proto)
}

function isSafeCssValue(value: string): boolean {
  const v = value.toLowerCase()
  return !/url\s*\(|expression\s*\(|javascript:|vbscript:|-moz-binding|behavior\s*:|@import|image-set|image\s*\(|var\s*\(/.test(v)
}

/** 按属性白名单重写一条声明串（`fill:#fff; stroke:none`）。 */
export function sanitizeCssDeclarations(css: string): string {
  return css
    .split(';')
    .map(decl => decl.trim())
    .filter(Boolean)
    .map((decl) => {
      const colon = decl.indexOf(':')
      if (colon <= 0) return ''
      const prop = decl.slice(0, colon).trim().toLowerCase()
      const value = decl.slice(colon + 1).trim()
      if (!prop || !value) return ''
      if (!SVG_ALLOWED_CSS_PROPS.has(prop)) return ''
      if (!isSafeCssValue(value)) return ''
      return `${prop}: ${value}`
    })
    .filter(Boolean)
    .join('; ')
}

/**
 * 清洗 SVG `<style>` 块。
 *
 * 逐条规则处理：丢弃 at-rule（`@import` / `@font-face` / `@keyframes` —— mermaid
 * 不产出，却正是 CSS 外带与内存放大的常用载体），选择器里带 `url(` / `expression`
 * 的整条丢，声明体走属性白名单。
 */
export function sanitizeCssText(css: string): string {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '')
  // 先整段摘掉 at-rule（`@import …;` / `@font-face { … }`）：它们在声明块里出现时
  // 会与后面的普通规则连成一片，先剔掉才不会把安全规则一起吞掉。
  const withoutAtRules = withoutComments.replace(/@[^{;}]*(\{[^{}]*\}|;)?/g, '')
  const rules = withoutAtRules.match(/[^{}]+\{[^{}]*\}/g) ?? []
  const out: string[] = []
  for (const rule of rules) {
    const brace = rule.indexOf('{')
    const selector = rule.slice(0, brace).trim()
    const body = rule.slice(brace + 1, rule.lastIndexOf('}'))
    if (!selector || selector.includes('@')) continue
    if (/url\s*\(|expression|javascript:|-moz-binding|behavior\s*:/i.test(selector)) continue
    const declarations = sanitizeCssDeclarations(body)
    if (!declarations) continue
    out.push(`${selector} { ${declarations} }`)
  }
  return out.join('\n')
}

/** DOMPurify 实例（jsdom / 浏览器都有 window，模块加载时即绑定）。 */
const purify = DOMPurify as unknown as {
  sanitize?: (dirty: string, config: Record<string, unknown>) => string
}

/**
 * 二次消毒 SVG。消毒不可信时返回 `''`（调用方据此退回源码展示）。
 */
export function sanitizeMermaidSvg(svg: string): string {
  if (!svg || typeof svg !== 'string') return ''
  if (typeof purify.sanitize !== 'function') return ''

  // ⓪ 先确认输入本身是**良构 SVG**：mermaid 用 XMLSerializer 输出，良构是它的契约。
  //    解析不了（或根不是 svg）就意味着这份内容不该被当成图信任 —— 直接放弃，
  //    而不是让 sanitizer 去"修"一个我们看不懂的东西。
  const raw = new DOMParser().parseFromString(svg, 'image/svg+xml')
  if (raw.querySelector('parsererror')) return ''
  if (raw.documentElement?.localName.toLowerCase() !== 'svg') return ''

  // ① 成熟 sanitizer 归一化 + SVG/MathML profile（不引入 HTML profile：
  //    图里没有任何理由是 HTML）。
  const cleaned = purify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true, mathMl: true },
    ALLOWED_TAGS,
    ALLOWED_ATTRS,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: [
      'script', 'foreignobject', 'iframe', 'frame', 'object', 'embed', 'audio',
      'video', 'animate', 'animatemotion', 'animatetransform', 'set', 'handler',
      'listener', 'image', 'link', 'meta', 'base', 'form', 'input', 'button',
    ],
    FORBID_ATTRS: ['src', 'srcset', 'formaction', 'action', 'ping', 'nonce'],
    SANITIZE_DOM: true,
    KEEP_CONTENT: false,
  })
  if (!cleaned) return ''

  // ② 结构/属性白名单复核 + 协议与 CSS 规范化。
  const doc = new DOMParser().parseFromString(cleaned, 'image/svg+xml')
  if (doc.querySelector('parsererror')) return ''
  const root = doc.documentElement
  if (!root || root.localName.toLowerCase() !== 'svg') return ''

  for (const el of [root, ...Array.from(root.querySelectorAll('*'))]) {
    const tag = el.localName.toLowerCase()
    if (tag !== 'svg' && !ALLOWED_TAG_SET.has(tag)) {
      el.remove() // 连同子树一起丢弃（foreignObject / script / image …）
      continue
    }
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase()
      if (name.startsWith('on')) {
        el.removeAttribute(attr.name)
        continue
      }
      if (URI_ATTRS.has(name)) {
        if (!isSafeUri(attr.value)) el.removeAttribute(attr.name)
        continue
      }
      if (name === 'style') {
        const css = sanitizeCssDeclarations(attr.value)
        if (css) el.setAttribute(attr.name, css)
        else el.removeAttribute(attr.name)
        continue
      }
      if (!ALLOWED_ATTR_SET.has(name)) el.removeAttribute(attr.name)
    }
    if (tag === 'style') {
      const css = sanitizeCssText(el.textContent || '')
      if (css) el.textContent = css
      else el.remove()
    }
  }

  const out = new XMLSerializer().serializeToString(root)
  // ③ 兜底断言：走到这里还有 <script> 说明上面的遍历被绕过，整体放弃。
  return /<script[\s>]/i.test(out) ? '' : out
}

/**
 * 渲染 host 内尚未处理的 `.mermaid` 块。
 * 幂等：已处理的块带 `data-rendered`，重复调用不会重复渲染。
 */
export async function renderMermaidBlocks(host: HTMLElement, options: RenderOptions): Promise<void> {
  const slots = host.querySelectorAll<HTMLElement>('.mermaid:not([data-rendered])')
  if (!slots.length) return

  let mermaid: MermaidAdapter
  try {
    mermaid = await loadAdapter()
  } catch {
    return   // 加载失败：保持源码展示，不打断阅读
  }

  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: options.dark ? 'dark' : 'default' })

  for (const slot of Array.from(slots)) {
    const code = slot.dataset.code ? decodeURIComponent(slot.dataset.code) : slot.textContent || ''
    slot.dataset.rendered = '1'
    try {
      const { svg } = await mermaid.render(`mmd-${++seq}`, code)
      // 二次消毒后再写入（见 sanitizeMermaidSvg）；消毒失败则退回源码展示。
      const safe = sanitizeMermaidSvg(svg)
      if (!safe) {
        slot.classList.add('mermaid-error')
        slot.dataset.error = t('errors.chart_syntax_error_showing_source')
        continue
      }
      slot.innerHTML = safe
      slot.classList.add('mermaid-diagram')
    } catch {
      slot.classList.add('mermaid-error')
      slot.dataset.error = t('errors.chart_syntax_error_showing_source')
    }
  }
}
