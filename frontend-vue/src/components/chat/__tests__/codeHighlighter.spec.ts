import { beforeEach, describe, expect, it } from 'vitest'
import {
  commentPrefixes,
  findCommentRuns,
  highlightCodeBlocks,
  htmlToText,
  isCommentLine,
  setHighlightAdapter,
  splitHtmlLines,
  type HighlightAdapter,
} from '../codeHighlighter'

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

describe('codeHighlighter（注释折叠）', () => {
  it('连续注释成块，返回半开区间', () => {
    const texts = ['code', '# a', '# b', '# c', 'code2']
    expect(findCommentRuns(texts, ['#'])).toEqual([[1, 4]])
  })

  it('短于阈值的注释不折（折起来只剩一个碍事的按钮）', () => {
    expect(findCommentRuns(['# a', '# b', 'code'], ['#'])).toEqual([])
  })

  it('文件以注释结尾时那块也能闭合', () => {
    // 少了末尾哨兵这块永远不折 —— 这是最容易写漏的一处。
    expect(findCommentRuns(['code', '# a', '# b', '# c'], ['#'])).toEqual([[1, 4]])
  })

  it('只认同该语言的前缀（Python 的 # 不是 Go 的注释）', () => {
    expect(commentPrefixes('python')).toEqual(['#'])
    expect(commentPrefixes('go')).toEqual(['//'])
    expect(isCommentLine('# x', commentPrefixes('go'))).toBe(false)
  })

  it('按纯文本判定，不依赖高亮是否成功', async () => {
    // 让高亮把注释包进 span：判定必须仍然成立，否则"高亮失败则折叠失效"。
    setHighlightAdapter({
      ...fake,
      highlight: () => ({ value: ['x', '<span class="c"># n1</span>', '<span class="c"># n2</span>', '<span class="c"># n3</span>', 'y'].join('\n') }),
    })
    const el = host('<pre><code data-lang="python">placeholder</code></pre>')
    await highlightCodeBlocks(el)

    expect(el.querySelectorAll('.code-fold-head')).toHaveLength(1)
    expect(el.querySelectorAll('.code-fold-body')).toHaveLength(2)
    expect(el.querySelector('.code-fold-head')!.getAttribute('data-fold-hidden')).toBe('2')
  })

  it('点击头部展开，再点收起', async () => {
    const el = host(`<pre><code data-lang="python">${['x', '# a', '# b', '# c', 'y'].join('\n')}</code></pre>`)
    await highlightCodeBlocks(el)

    const head = el.querySelector<HTMLElement>('.code-fold-head')!
    const bodies = () => Array.from(el.querySelectorAll<HTMLElement>('.code-fold-body'))

    expect(bodies().every(row => row.dataset.foldState === 'collapsed')).toBe(true)

    head.click()
    expect(head.dataset.foldOpen).toBe('1')
    expect(bodies().every(row => row.dataset.foldState === 'open')).toBe(true)

    head.click()
    expect(head.dataset.foldOpen).toBe('0')
    expect(bodies().every(row => row.dataset.foldState === 'collapsed')).toBe(true)
  })

  it('展开状态不会串到别的折叠块', async () => {
    const body = ['# a', '# b', '# c', 'mid', '# x', '# y', '# z'].join('\n')
    const el = host(`<pre><code data-lang="python">${body}</code></pre>`)
    await highlightCodeBlocks(el)

    const heads = Array.from(el.querySelectorAll<HTMLElement>('.code-fold-head'))
    expect(heads).toHaveLength(2)

    heads[0]!.click()

    const state = (head: HTMLElement) => {
      const id = head.dataset.foldId!
      return Array.from(el.querySelectorAll<HTMLElement>('.code-fold-body'))
        .filter(row => row.dataset.foldId === id)
        .map(row => row.dataset.foldState)
    }
    expect(state(heads[0]!)).toEqual(['open', 'open'])
    expect(state(heads[1]!)).toEqual(['collapsed', 'collapsed'])
  })

  it('重复增强不会重复绑定点击处理（幂等）', async () => {
    // 必须让第二次调用**真的**再增强一块，否则 `PENDING_SELECTOR` 已无候选、
    // 函数提前返回，这条用例就只能证明"提前返回了"，测不到监听器是否绑了两遍。
    const el = host(`<pre><code data-lang="python">${['x', '# a', '# b', '# c', 'y'].join('\n')}</code></pre>`)
    await highlightCodeBlocks(el)

    el.insertAdjacentHTML(
      'beforeend',
      `<pre><code data-lang="python">${['p', '# d', '# e', '# f', 'q'].join('\n')}</code></pre>`,
    )
    await highlightCodeBlocks(el)

    const heads = Array.from(el.querySelectorAll<HTMLElement>('.code-fold-head'))
    expect(heads).toHaveLength(2)

    heads[0]!.click()
    // 容器上若绑了两次，一次点击会连跳两拍，`foldOpen` 回到 '0'。
    expect(heads[0]!.dataset.foldOpen).toBe('1')
  })

  it('少于行号阈值时不折叠（没有行结构可折）', async () => {
    const el = host('<pre><code data-lang="python"># a\n# b\n# c</code></pre>')
    await highlightCodeBlocks(el)
    expect(el.querySelectorAll('.code-fold-head')).toHaveLength(0)
  })

  it('htmlToText 不重复解码实体', () => {
    // `&amp;lt;` 是字面的 "&lt;"，只能还原一次成 "&lt;"，不能变成 "<"。
    expect(htmlToText('<span>&amp;lt;</span>')).toBe('&lt;')
  })
})
