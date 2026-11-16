/**
 * unified diff 的轻量解析（纯函数，无 DOM）。
 *
 * 后端 edit_file 用 `difflib.unified_diff` 输出 diff
 * （python-engine/app/tools/edit_file.py），前端此前把它当普通 JSON 打印。
 * 这里只做行级分类与增删计数：着色交给 CSS，不引入额外依赖 ——
 * hunk 头里的范围数字对阅读没有帮助，完整解析器是过度设计。
 */

export type DiffLineKind = 'file' | 'hunk' | 'add' | 'del' | 'context'

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export interface ParsedDiff {
  lines: DiffLine[]
  additions: number
  deletions: number
  /** 参与改动的文件路径（已去掉 a/ b/ 前缀） */
  files: string[]
}

/**
 * 判据保守：必须出现 hunk 头（`@@`），或成对的 `---`/`+++` 文件头。
 * 只看 `+`/`-` 开头会把 Markdown 列表、`-` 分隔线误判成 diff。
 */
export function looksLikeDiff(content: string): boolean {
  let sawOldFile = false
  for (const line of content.split('\n')) {
    if (line.startsWith('@@')) return true
    if (line.startsWith('---')) sawOldFile = true
    else if (line.startsWith('+++')) return sawOldFile
  }
  return false
}

export function parseUnifiedDiff(content: string): ParsedDiff {
  const lines: DiffLine[] = []
  const files: string[] = []
  let additions = 0
  let deletions = 0

  for (const raw of content.split('\n')) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw
    if (isFileHeader(line)) {
      lines.push({ kind: 'file', text: line })
      const path = stripFilePrefix(line)
      if (path && !files.includes(path)) files.push(path)
    } else if (line.startsWith('@@')) {
      lines.push({ kind: 'hunk', text: line })
    } else if (line.startsWith('+')) {
      additions++
      lines.push({ kind: 'add', text: line })
    } else if (line.startsWith('-')) {
      deletions++
      lines.push({ kind: 'del', text: line })
    } else {
      lines.push({ kind: 'context', text: line })
    }
  }

  // split 产生的尾部空行不是 diff 内容，不进渲染序列
  while (lines.length > 0 && lines[lines.length - 1]!.kind === 'context' && lines[lines.length - 1]!.text === '') lines.pop()
  return { lines, additions, deletions, files }
}

function isFileHeader(line: string): boolean {
  return line.startsWith('+++') || line.startsWith('---')
    || line.startsWith('diff --git') || line.startsWith('index ')
}

function stripFilePrefix(line: string): string {
  const git = /^diff --git\s+a\/(\S+)\s+b\/(\S+)/.exec(line)
  if (git) return git[2]!
  const header = /^(?:\+\+\+|---)\s+(?:a\/|b\/)?(\S+)/.exec(line)
  if (!header) return ''
  return header[1] === '/dev/null' ? '' : header[1]!
}
