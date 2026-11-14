/**
 * 工作台文件预览的纯逻辑：语言识别 + 大文件截断。
 *
 * 抽成独立模块而不是留在 SFC 里：这两条都是"错了也不报错"的行为，
 * 必须能被直接断言。留在 `script setup` 里就只能靠挂载组件间接观察，
 * 或者（更糟）在测试里复制一份实现 —— 那测的是副本，不是产品代码。
 */

/**
 * 预览最多渲染多少行。
 *
 * 后端允许 2MB 文本（`file_too_large` 只在超过时才拒），而 2MB 源码可能有十几万行。
 * 一次性把它们塞进 DOM：每个字符都成了一个文本节点，滚动与高亮都会卡死标签页 ——
 * 用户看到的是"点开大文件以后整个界面没反应"，比"只显示前几千行"糟得多。
 * 2000 行 ≈ 一次能读完的量，超出部分明确告知并让用户用「用系统程序打开」。
 */
export const PREVIEW_LINE_LIMIT = 2000

/** 扩展名 → highlight.js 语言名。认不出的返回空串，让高亮器走 `highlightAuto`。 */
const EXT_TO_LANG: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', js: 'javascript', jsx: 'javascript', mjs: 'javascript',
  cjs: 'javascript', vue: 'xml', html: 'xml', htm: 'xml', xml: 'xml', svg: 'xml',
  css: 'css', scss: 'scss', less: 'less',
  py: 'python', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', rb: 'ruby',
  php: 'php', cs: 'csharp', cpp: 'cpp', cc: 'cpp', h: 'cpp', c: 'c',
  sh: 'bash', bash: 'bash', zsh: 'bash', ps1: 'powershell',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'ini', ini: 'ini',
  sql: 'sql', md: 'markdown', dockerfile: 'dockerfile',
}

/** 无扩展名但约定俗成的文件名 → 语言名（按整名匹配，优先级高于扩展名）。 */
const NAME_TO_LANG: Record<string, string> = {
  dockerfile: 'dockerfile',
  makefile: 'makefile',
  'go.mod': 'go',
  'go.sum': 'go',
  gemfile: 'ruby',
  rakefile: 'ruby',
  '.gitignore': 'bash',
  '.dockerignore': 'bash',
  '.env': 'bash',
}

/**
 * 从路径推出 highlight.js 的语言名；认不出返回空串。
 *
 * 只看**文件名**的最后一个扩展名：路径中间的点（`my.dir/file.ts`）不该参与判定，
 * 否则 `a.b.c/file` 会被当成扩展名 `c` 而套上 C 语言配色。
 */
export function langOf(path: string): string {
  const name = String(path || '').split('/').pop() || ''
  // 整名优先：`go.mod` 的"扩展名"是 `mod`，按扩展名查不到；`Makefile` 则根本没扩展名。
  const byName = NAME_TO_LANG[name.toLowerCase()]
  if (byName) return byName
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : ''
  return EXT_TO_LANG[ext] || ''
}

/**
 * 按行截断到 `PREVIEW_LINE_LIMIT`，返回要渲染的文本与**被丢掉的行数**。
 *
 * 必须同时返回丢掉的行数：只截断不告知，等于让用户以为文件只有这么长 ——
 * 而他可能会据此判断"这个函数不存在"。
 *
 * `more` 用于**后端已经分屏**的情形：此时 `raw` 最多就是一屏，本地算出的
 * dropped 恒为 0。只看本地会得出"文件已经完整"的结论，所以要用后端给出的
 * `truncated` / `total_lines` 补上缺口。
 */
export function truncatePreview(
  raw: string,
  more?: { serverTruncated?: boolean; totalLines?: number },
): { text: string; dropped: number } {
  const source = String(raw ?? '')
  const lines = source.split('\n')
  if (lines.length > PREVIEW_LINE_LIMIT) {
    return {
      text: lines.slice(0, PREVIEW_LINE_LIMIT).join('\n'),
      dropped: lines.length - PREVIEW_LINE_LIMIT,
    }
  }
  if (more?.serverTruncated) {
    const total = typeof more.totalLines === 'number' ? more.totalLines : 0
    // 至少报 1：后端说还有内容时，"0 行未显示"是自相矛盾的。
    return { text: source, dropped: Math.max(total - lines.length, 1) }
  }
  return { text: source, dropped: 0 }
}
