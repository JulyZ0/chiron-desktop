/**
 * 代码块的按需增强：语法高亮 + 行号。
 *
 * 为什么不在 markdown 渲染时直接高亮：highlight.js 有上百 KB，而含代码的消息
 * 只占少数。这里改成「先输出带语言标记的纯代码块，挂载后再增强」：
 * - 首屏不再为用不到的高亮付出体积；
 * - 增强是纯增量，加载失败或渲染失败也只是少个配色/行号，代码本身始终可读可复制。
 *
 * 加载器可注入（`setHighlightAdapter`），增强契约因此可在测试里验证。
 */

export interface HighlightAdapter {
  getLanguage(name: string): unknown
  highlight(code: string, options: { language: string }): { value: string }
  highlightAuto(code: string): { value: string }
}

/** 需要增强的代码元素：带 data-lang 且尚未处理 */
const PENDING_SELECTOR = 'pre code[data-lang]:not([data-highlighted])'

/** 少于该行数不加行号：片段加行号是噪声，行号只在长代码里有用 */
export const LINE_NUMBER_MIN_LINES = 5

let injected: HighlightAdapter | null = null
let loader: Promise<HighlightAdapter> | null = null

/** 测试注入替身；传 null 恢复真实懒加载 */
export function setHighlightAdapter(adapter: HighlightAdapter | null): void {
  injected = adapter
  loader = null
}

function loadAdapter(): Promise<HighlightAdapter> {
  if (injected) return Promise.resolve(injected)
  if (!loader) {
    loader = import('highlight.js/lib/common')
      .then(mod => ((mod as { default?: HighlightAdapter }).default ?? (mod as unknown as HighlightAdapter)))
      .catch((err) => {
        loader = null   // 允许下次重试（临时失败不该被永久放弃）
        throw err
      })
  }
  return loader
}

/**
 * 按行切分高亮后的 HTML，并在行尾闭合、行首重开跨行的 `<span>`。
 *
 * 为什么必须做：hljs 的输出里多行字符串/注释会形成**跨行 span**，直接按 `\n`
 * 切开会让每行 HTML 不闭合（浏览器虽会容错，但行号列与代码列会错位）。
 * 这里用栈跟踪未闭合的标签，纯字符串处理，不依赖 DOM。
 */
export function splitHtmlLines(html: string): string[] {
  const lines: string[] = []
  const open: string[] = []
  let current = ''
  let i = 0

  while (i < html.length) {
    const ch = html[i]!
    if (ch === '<') {
      const end = html.indexOf('>', i)
      if (end === -1) {
        current += html.slice(i)
        break
      }
      const tag = html.slice(i, end + 1)
      current += tag
      if (/^<span[\s>]/i.test(tag)) open.push(tag)
      else if (/^<\/span/i.test(tag)) open.pop()
      i = end + 1
      continue
    }
    if (ch === '\n') {
      lines.push(current + '</span>'.repeat(open.length))
      current = open.join('')
      i++
      continue
    }
    current += ch
    i++
  }
  lines.push(current)
  return lines
}

/**
 * 连续注释少于该行数就不折 —— 折起来只剩一个小按钮，比原文更碍事。
 *
 * 3 行是有意的：两行注释通常是紧贴代码的说明（"为什么这样写"），
 * 折掉它就等于把上下文藏了；三行往上多见于文件头、许可声明、被注释掉的旧实现。
 */
export const COMMENT_FOLD_MIN_LINES = 3

/** 按语言取行注释前缀。认不出的语言用最常见的两种，宁可少折不误折。 */
const COMMENT_PREFIXES: Record<string, string[]> = {
  python: ['#'], ruby: ['#'], bash: ['#'], yaml: ['#'], ini: ['#'], toml: ['#'],
  go: ['//'], rust: ['//'], java: ['//'], kotlin: ['//'], csharp: ['//'],
  javascript: ['//'], typescript: ['//'], scss: ['//'], php: ['//', '#'],
  css: ['/*', '*'], sql: ['--'],
}

const DEFAULT_COMMENT_PREFIXES = ['//', '#']

export function commentPrefixes(lang: string): string[] {
  return COMMENT_PREFIXES[lang] ?? DEFAULT_COMMENT_PREFIXES
}

/**
 * 剥掉标签与实体，只留可读文本。
 *
 * 判断"这行是不是注释"必须在**纯文本**上做：高亮后的行里注释被包成
 * `<span class="comment">`，直接看 HTML 会同时把"是否高亮成功"变成前置条件 ——
 * 高亮失败（加载不出来）时折叠就整体失效，而折叠本不该依赖配色。
 *
 * `&amp;` 必须最后还原，否则 `&amp;lt;` 会被二次解码成 `<`。
 */
export function htmlToText(html: string): string {
  return html
    .replace(/<[^>]*>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&')
}

export function isCommentLine(text: string, prefixes: string[]): boolean {
  const trimmed = text.trim()
  if (!trimmed) return false
  return prefixes.some(prefix => trimmed.startsWith(prefix))
}

/**
 * 找出连续注释行，返回 `[start, end)` 半开区间。
 *
 * 返回半开区间而不是 `{start, length}`：调用方要遍历时 `for (i = start; i < end)` 更少出错，
 * 且空区间（`start === end`）天然表示"没有"，不需要额外判空。
 */
export function findCommentRuns(
  texts: string[],
  prefixes: string[],
  minLines = COMMENT_FOLD_MIN_LINES,
): Array<[number, number]> {
  const runs: Array<[number, number]> = []
  let start = -1
  // 多走一步（`<=`）让末尾的注释块也能闭合，否则文件以注释结尾时那块永远不折。
  for (let i = 0; i <= texts.length; i++) {
    const isComment = i < texts.length && isCommentLine(texts[i]!, prefixes)
    if (isComment) {
      if (start === -1) start = i
    } else if (start !== -1) {
      if (i - start >= minLines) runs.push([start, i])
      start = -1
    }
  }
  return runs
}

/** 行号加在 code 内部（不改动 pre），行号本身 user-select: none，不会进复制内容 */
function addLineNumbers(block: HTMLElement, startLine = 1): number {
  const lines = splitHtmlLines(block.innerHTML)
  if (lines.length < LINE_NUMBER_MIN_LINES) return lines.length

  // 折叠区间按纯文本判定（见 `htmlToText` 的理由），与高亮成败无关。
  const runs = findCommentRuns(
    lines.map(htmlToText),
    commentPrefixes(block.dataset.lang || ''),
  )
  // 行索引 → 所属折叠块。`head` 行就是那个可点击的按钮所在行。
  const foldOf = new Map<number, { id: string; head: boolean; count: number }>()
  runs.forEach(([from, to], index) => {
    const id = `f${index}`
    for (let line = from; line < to; line++) {
      foldOf.set(line, { id, head: line === from, count: to - from })
    }
  })

  block.dataset.numbered = '1'
  block.innerHTML = lines
    .map((line, index) => {
      const fold = foldOf.get(index)
      let attrs = ''
      let extraClass = ''
      if (fold?.head) {
        // 头部行承担"点我展开"的职责，因此带上块 id 与被隐藏的行数。
        attrs = ` data-fold-toggle="1" data-fold-id="${fold.id}" data-fold-hidden="${fold.count - 1}"`
        extraClass = ' code-fold-head'
      } else if (fold) {
        // 默认折叠：注释块默认折起，点头部展开。保留 `data-fold-state` 作为唯一状态源，
        // 样式与切换逻辑都读它，不另存变量。
        attrs = ` data-fold-id="${fold.id}" data-fold-state="collapsed"`
        extraClass = ' code-fold-body'
      }
      return `<span class="code-line${extraClass}"${attrs}><span class="code-line-no">${index + startLine}</span><span class="code-line-body">${line}</span></span>`
    })
    .join('')
  return lines.length
}

/**
 * 事件委托绑定折叠开关；幂等。
 *
 * 用委托而不是给每行绑 `onclick`：预览是**分块追加**的，后到的块会带上新的折叠按钮，
 * 逐行绑定就得每次重新挂一遍。绑在容器上一次，新增的按钮天然生效。
 */
function bindFoldToggles(host: HTMLElement): void {
  if (host.dataset.foldBound === '1') return
  host.dataset.foldBound = '1'
  host.addEventListener('click', (event) => {
    const target = event.target as HTMLElement | null
    const head = target?.closest<HTMLElement>('[data-fold-toggle]')
    if (!head || !host.contains(head)) return
    const id = head.dataset.foldId
    if (!id) return
    const expanded = head.dataset.foldOpen === '1'
    head.dataset.foldOpen = expanded ? '0' : '1'
    const next = expanded ? 'collapsed' : 'open'
    // 只动同属一个块的行；`data-fold-id` 是本次渲染生成的，不来自用户输入。
    host.querySelectorAll<HTMLElement>('[data-fold-id]').forEach((row) => {
      if (row === head || row.dataset.foldId !== id) return
      row.dataset.foldState = next
    })
  })
}

/**
 * 增强 host 内尚未处理的代码块；幂等，重复调用不会重复处理。
 *
 * `startLine` 让行号从指定值开始：大文件预览是**分块追加**的（每块一个
 * `pre > code`，旧块完全不动，只增强新来的那一块）。若每个块都从 1 开始编号，
 * 第二块的第一行会显示成"第 1 行" —— 行号错乱比没有行号更容易误导人。
 * 单块场景传默认值即可，行为与之前完全一致。
 */
export async function highlightCodeBlocks(host: HTMLElement, startLine = 1): Promise<void> {
  const blocks = host.querySelectorAll<HTMLElement>(PENDING_SELECTOR)
  if (!blocks.length) return

  // 委托要在**增强之前**绑：块可能随时被替换，但容器始终是同一个。
  bindFoldToggles(host)

  let hljs: HighlightAdapter | null = null
  try {
    hljs = await loadAdapter()
  } catch {
    hljs = null   // 加载失败：保持无高亮的可读代码，行号仍然加
  }

  let lineCursor = startLine
  for (const block of Array.from(blocks)) {
    const lang = block.dataset.lang || ''
    const code = block.textContent || ''
    block.dataset.highlighted = '1'
    if (hljs) {
      try {
        block.innerHTML = lang && hljs.getLanguage(lang)
          ? hljs.highlight(code, { language: lang }).value
          : hljs.highlightAuto(code).value
        block.classList.add('hljs')
      } catch {
        // 高亮失败：保留原文（已经由 markdown 转义过，安全）
        block.textContent = code
        block.classList.remove('hljs')
      }
    }
    // 优先用块自带的起点（`data-start-line`）：分块预览里每块的行号起点在**渲染时**
    // 就定好了，与"第几个待增强块"无关。此前增强的是整块，光标才够用；分块后只增强
    // 新来的那一块，光标永远是 startLine，第二块会从 1 重新编号。
    const declared = Number(block.dataset.startLine || '')
    const from = Number.isFinite(declared) && declared > 0 ? declared : lineCursor
    lineCursor = from + addLineNumbers(block, from)
  }
}
