/**
 * Mermaid SVG 二次消毒（PRD H63 / 9.9）回归测试。
 *
 * 钉的是「恶意 SVG 到不了 DOM」这条不变量。图内容来自模型输出（可被提示注入影响），
 * mermaid 自己的 securityLevel 只是库内部实现细节，不能当作唯一一道闸。
 *
 * 覆盖的攻击面：
 *  1. `<script>` / `on*` 事件属性；
 *  2. `javascript:` 协议的各种归一化绕过（大小写、控制字符、HTML 实体）；
 *  3. `data:text/html` 与 `data:image/svg+xml`（可执行）vs `data:image/png`（图片）；
 *  4. `<foreignObject>` 里的整棵 HTML 子树；
 *  5. `<style>` 里的 `@import url()` 与内联 `style="…url(…)"`；
 *  6. 结构白名单：没列进来的标签（iframe / image / animate）整棵丢弃；
 *  7. 消毒不可判定（坏 XML / 根不是 svg）时**整体放弃**，由调用方退回源码展示。
 */
import { describe, it, expect, afterEach } from 'vitest'
import {
  renderMermaidBlocks,
  sanitizeCssDeclarations,
  sanitizeCssText,
  sanitizeMermaidSvg,
  setMermaidAdapter,
} from '../mermaidRenderer'

const SAFE_DIAGRAM = [
  '<svg id="mmd-1" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">',
  '<style>#mmd-1 .node rect { fill: #ECECFF; stroke: #9370DB }</style>',
  '<g class="node default" transform="translate(10,10)">',
  '<rect class="basic label-container" width="80" height="40" style="fill: #ECECFF !important; stroke: #9370DB" />',
  '<text x="10" y="20" text-anchor="middle">hello</text>',
  '</g>',
  '<path d="M0,0 L10,10" stroke-width="2" />',
  '</svg>',
].join('')

function svg(inner: string, attrs = ''): string {
  return `<svg xmlns="http://www.w3.org/2000/svg"${attrs}>${inner}</svg>`
}

afterEach(() => {
  setMermaidAdapter(null)
})

describe('sanitizeMermaidSvg —— 常规图表必须原样保留', () => {
  it('保留 path / rect / text / class / transform / viewBox 与安全内联样式', () => {
    const out = sanitizeMermaidSvg(SAFE_DIAGRAM)
    expect(out).toContain('<svg')
    expect(out).toContain('path')
    expect(out).toContain('rect')
    expect(out).toContain('hello')
    expect(out).toContain('class="node default"')
    expect(out).toContain('transform="translate(10,10)"')
    expect(out).toContain('fill')
  })
})

describe('sanitizeMermaidSvg —— 脚本与事件属性', () => {
  it('剥离 <script> 元素', () => {
    const out = sanitizeMermaidSvg(svg('<script>alert(1)</script><text>x</text>'))
    expect(out).not.toMatch(/<script/i)
    expect(out).not.toContain('alert(1)')
    // 同期正常内容不受影响
    expect(out).toContain('x')
  })

  it('剥离大小写混写的事件属性，保留普通属性', () => {
    const out = sanitizeMermaidSvg(svg('<rect ONLOAD="alert(1)" onclick="alert(2)" width="10" />'))
    expect(out.toLowerCase()).not.toContain('onload')
    expect(out.toLowerCase()).not.toContain('onclick')
    expect(out).not.toContain('alert')
    expect(out).toContain('width="10"')
  })
})

describe('sanitizeMermaidSvg —— 协议规范化', () => {
  it('大小写混写的 javascript: 链接被移除', () => {
    const out = sanitizeMermaidSvg(svg('<a href="JaVaScRiPt:alert(1)"><text>open</text></a>'))
    expect(out.toLowerCase()).not.toContain('javascript')
    expect(out).toContain('open')
  })

  it('控制字符插空的 javascript: 链接被移除', () => {
    const out = sanitizeMermaidSvg(svg('<a href="java\tscript:alert(1)"><text>open</text></a>'))
    expect(out.toLowerCase()).not.toContain('javascript')
  })

  it('HTML 实体编码的 javascript: 链接被移除', () => {
    const out = sanitizeMermaidSvg(svg('<a href="&#106;avascript:alert(1)"><text>open</text></a>'))
    expect(out.toLowerCase()).not.toContain('javascript')
  })

  it('xlink:href 走同一套协议校验', () => {
    const out = sanitizeMermaidSvg(svg('<use xlink:href="javascript:alert(1)" />'))
    expect(out.toLowerCase()).not.toContain('javascript')
  })

  it('data:text/html 与 data:image/svg+xml 被移除，data:image/png 也不放行', () => {
    const html = sanitizeMermaidSvg(svg('<a href="data:text/html;base64,PHNjcmlwdD4="><text>t</text></a>'))
    expect(html.toLowerCase()).not.toContain('data:text/html')

    const svgData = sanitizeMermaidSvg(svg('<a href="data:image/svg+xml;base64,PHN2Zz4="><text>t</text></a>'))
    expect(svgData.toLowerCase()).not.toContain('data:image/svg+xml')

    // DOMPurify 默认整类剥掉 data: URI（ALLOWED_URI_REGEXP 未放开）—— 比白名单更严，
    // 图表不需要 data: 链接，保持这个默认即可。
    const png = sanitizeMermaidSvg(
      svg('<a href="data:image/png;base64,iVBORw0KGgo="><text>t</text></a>'),
    )
    expect(png.toLowerCase()).not.toContain('data:image/png')
  })

  it('https 与相对 / 片段链接保留', () => {
    const https = sanitizeMermaidSvg(svg('<a href="https://example.com/doc"><text>t</text></a>'))
    expect(https).toContain('https://example.com/doc')
    const frag = sanitizeMermaidSvg(svg('<a href="#node-1"><text>t</text></a>'))
    expect(frag).toContain('#node-1')
  })
})

describe('sanitizeMermaidSvg —— foreignObject 与结构白名单', () => {
  it('foreignObject 整棵子树丢弃（含其内的 HTML 与事件）', () => {
    const out = sanitizeMermaidSvg(
      svg('<foreignObject width="100" height="50"><div xmlns="http://www.w3.org/1999/xhtml" onclick="alert(1)">payload</div></foreignObject><text>safe</text>'),
    )
    expect(out.toLowerCase()).not.toContain('foreignobject')
    expect(out).not.toContain('payload')
    expect(out).not.toContain('alert')
    expect(out).toContain('safe')
  })

  it('iframe / image / animate 一律丢弃', () => {
    const out = sanitizeMermaidSvg(
      svg('<iframe src="https://evil.example"></iframe><image href="https://evil.example/x.svg" /><animate attributeName="href" to="javascript:alert(1)"/><text>ok</text>'),
    )
    expect(out.toLowerCase()).not.toContain('<iframe')
    expect(out.toLowerCase()).not.toContain('<image')
    expect(out.toLowerCase()).not.toContain('<animate')
    expect(out).not.toContain('evil.example')
    expect(out).toContain('ok')
  })
})

describe('sanitizeMermaidSvg —— CSS', () => {
  it('<style> 丢弃 @import 与 url()，保留安全规则', () => {
    const out = sanitizeMermaidSvg(
      svg('<style>@import url("https://evil.example/x.css"); .node { fill: #ECECFF } .evil { background: url(javascript:alert(1)) }</style><text>t</text>'),
    )
    expect(out).not.toContain('@import')
    expect(out.toLowerCase()).not.toContain('evil.example')
    expect(out.toLowerCase()).not.toContain('url(')
    expect(out).toContain('fill: #ECECFF')
  })

  it('内联 style 只保留白名单视觉属性', () => {
    expect(sanitizeCssDeclarations('fill:#fff; position:absolute; top:0; background:url(https://evil.example/x)'))
      .toBe('fill: #fff')
    // 定位与资源类属性都不在白名单里
    expect(sanitizeCssDeclarations('z-index:9999; behavior:url(#default#time2)')).toBe('')
  })

  it('sanitizeCssText 丢弃 at-rule 与带 url() 的选择器', () => {
    const css = sanitizeCssText(
      '@font-face { font-family: x; src: url(https://evil.example/f.woff) } .a { fill: red } url(x) { fill: blue }',
    )
    expect(css).not.toContain('@font-face')
    expect(css).not.toContain('evil.example')
    expect(css).toContain('.a { fill: red }')
  })
})

describe('sanitizeMermaidSvg —— 不可判定时整体放弃', () => {
  it('坏 XML 返回空串', () => {
    expect(sanitizeMermaidSvg('<svg><g></svg')).toBe('')
  })

  it('根不是 svg 返回空串', () => {
    expect(sanitizeMermaidSvg('<div><p>x</p></div>')).toBe('')
  })

  it('空输入返回空串', () => {
    expect(sanitizeMermaidSvg('')).toBe('')
  })
})

describe('renderMermaidBlocks —— 恶意 SVG 到不了 DOM', () => {
  const evil = [
    '<svg id="mmd-9" xmlns="http://www.w3.org/2000/svg">',
    '<script>window.__pwned = 1</script>',
    '<rect onload="window.__pwned = 2" width="10" height="10" />',
    '<a href="javascript:alert(1)"><text>click</text></a>',
    '</svg>',
  ].join('')

  it('注入的渲染器返回恶意 SVG 时，落进 DOM 的内容已被剥掉执行面', async () => {
    setMermaidAdapter({
      initialize: () => {},
      render: async () => ({ svg: evil }),
    })
    const host = document.createElement('div')
    const slot = document.createElement('div')
    slot.className = 'mermaid'
    slot.textContent = 'graph TD; A-->B'
    host.appendChild(slot)

    await renderMermaidBlocks(host, { dark: false })

    const html = slot.innerHTML
    expect(html.toLowerCase()).not.toContain('<script')
    expect(html.toLowerCase()).not.toContain('onload')
    expect(html.toLowerCase()).not.toContain('javascript')
    expect(slot.classList.contains('mermaid-diagram')).toBe(true)
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('消毒不可判定时退回源码展示，而不是写入未过滤内容', async () => {
    setMermaidAdapter({
      initialize: () => {},
      render: async () => ({ svg: '<svg><g></svg' }),
    })
    const host = document.createElement('div')
    const slot = document.createElement('div')
    slot.className = 'mermaid'
    slot.textContent = 'graph TD; A-->B'
    host.appendChild(slot)

    await renderMermaidBlocks(host, { dark: false })

    // 失败不吞内容：原始代码块文本留在原处（这是产品取舍），但**绝不能**有 SVG 写入，
    // 也不能被标成渲染成功。
    expect(slot.innerHTML).not.toContain('<svg')
    expect(slot.classList.contains('mermaid-error')).toBe(true)
    expect(slot.classList.contains('mermaid-diagram')).toBe(false)
    expect(slot.dataset.error).toBeTruthy()
  })
})
