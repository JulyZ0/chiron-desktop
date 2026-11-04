import { beforeEach, describe, expect, it } from 'vitest'
import { highlightCodeBlocks, setHighlightAdapter, splitHtmlLines, type HighlightAdapter } from '../codeHighlighter'

let highlighted: { code: string; language: string }[] = []
let autoHighlighted: string[] = []

const fake: HighlightAdapter = {
  getLanguage: name => (name === 'python' ? {} : undefined),
  highlight(code, options) {
    highlighted.push({ code, language: options.language })
    return { value: `<span class="k">${code}</span>` }
  },
  highlightAuto(code) {
    autoHighlighted.push(code)
    return { value: `<span class="auto">${code}</span>` }
  },
}

function host(html: string): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = html
  return el
}

beforeEach(() => {
  highlighted = []
  autoHighlighted = []
  setHighlightAdapter(fake)
})

describe('codeHighlighter（按需语法高亮）', () => {
  it('按 data-lang 指定的语言高亮，并打上 hljs 类', async () => {
    const el = host('<pre><code class="language-python" data-lang="python">print(1)</code></pre>')
    await highlightCodeBlocks(el)

    expect(highlighted).toEqual([{ code: 'print(1)', language: 'python' }])
    const code = el.querySelector('code')!
    expect(code.classList.contains('hljs')).toBe(true)
    expect(code.innerHTML).toContain('class="k"')
  })

  it('未知语言走自动检测', async () => {
    const el = host('<pre><code data-lang="xyz">foo</code></pre>')
    await highlightCodeBlocks(el)
    expect(autoHighlighted).toEqual(['foo'])
  })

  it('幂等：已处理的块不会被重复高亮', async () => {
    const el = host('<pre><code data-lang="python">a</code></pre>')
    await highlightCodeBlocks(el)
    await highlightCodeBlocks(el)
    expect(highlighted).toHaveLength(1)
  })

  it('没有代码块时不做任何高亮（懒加载的意义）', async () => {
    const el = host('<p>纯文本，没有代码</p>')
    await highlightCodeBlocks(el)
    expect(highlighted).toHaveLength(0)
    expect(autoHighlighted).toHaveLength(0)
  })

  it('高亮抛错时保留原文，不留空白', async () => {
    setHighlightAdapter({
      ...fake,
      highlight() { throw new Error('boom') },
    })
    const el = host('<pre><code data-lang="python">keep me</code></pre>')
    await highlightCodeBlocks(el)
    expect(el.querySelector('code')!.textContent).toBe('keep me')
  })
})

describe('codeHighlighter（行号）', () => {
  const lines = (n: number) => Array.from({ length: n }, (_, i) => `line${i}`).join('\n')

  it('多行代码按行加行号，行号与内容分列', async () => {
    const el = host(`<pre><code data-lang="python">${lines(6)}</code></pre>`)
    await highlightCodeBlocks(el)

    const rows = el.querySelectorAll('.code-line')
    expect(rows).toHaveLength(6)
    expect(rows[0]!.querySelector('.code-line-no')!.textContent).toBe('1')
    expect(rows[5]!.querySelector('.code-line-no')!.textContent).toBe('6')
    expect(rows[0]!.querySelector('.code-line-body')!.textContent).toContain('line0')
  })

  it('少于阈值的片段不加行号（片段加行号是噪声）', async () => {
    const el = host('<pre><code data-lang="js">a\nb\nc</code></pre>')
    await highlightCodeBlocks(el)
    expect(el.querySelectorAll('.code-line')).toHaveLength(0)
    expect(el.querySelector('code')!.dataset.numbered).toBeUndefined()
  })

  it('高亮结果里的跨行 span 会按行闭合（否则行号列与代码列会错位）', async () => {
    setHighlightAdapter({
      ...fake,
      highlight: () => ({ value: `<span class="s">${['a', 'b', 'c', 'd', 'e'].join('\n')}</span>` }),
    })
    const el = host('<pre><code data-lang="python">placeholder</code></pre>')
    await highlightCodeBlocks(el)

    const rows = el.querySelectorAll('.code-line-body')
    expect(rows).toHaveLength(5)
    for (const row of Array.from(rows)) {
      const html = row.innerHTML
      expect((html.match(/<span/g) ?? []).length).toBe((html.match(/<\/span>/g) ?? []).length)
    }
  })

  it('按行切分对纯文本同样成立（高亮不可用时也加行号）', () => {
    expect(splitHtmlLines('a\nb')).toEqual(['a', 'b'])
    expect(splitHtmlLines('<span class="x">a\nb</span>')).toEqual([
      '<span class="x">a</span>',
      '<span class="x">b</span>',
    ])
  })
})
