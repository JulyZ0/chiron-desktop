/**
 * 路径归一化：把工具参数/正文里各种写法的路径，映射成「相对项目根的 POSIX 路径」。
 *
 * ## 为什么单独抽出来
 *
 * 同一份文件在不同来源里的写法不一样：工具参数可能是 `D:\proj\src\a.ts`，正文里
 * 可能是 `./src/a.ts`，git 输出一定是 `src/a.ts`。改动页签要把这些**对到一起**，
 * 工作台的跳转也要按同一套规则判断"在不在这个项目里"。
 *
 * 两处各写一遍的后果不是"多几行代码"，而是两边判据漂移：改动列表把一个文件认作
 * 项目内，点 оно却提示"不在项目内" —— 这种自相矛盾比功能缺失更难解释。
 */

/** 是否像绝对路径（Windows 盘符或 POSIX 根）。 */
export function isAbsolutePath(path: string): boolean {
  const p = String(path || '').trim()
  return /^[a-zA-Z]:[\\/]/.test(p) || p.startsWith('/') || p.startsWith('\\\\')
}

function trimTrailingSlash(path: string): string {
  return path.replace(/[\\/]+$/, '')
}

/**
 * 把 `raw` 映射到相对 `root` 的路径。
 *
 * - `inside: false` 表示它是一条绝对路径，但落在项目之外 —— 调用方应当提示而不是
 *   硬读（读一个项目外的文件既会失败，也绕过了"项目文件"这个语义）。
 * - 相对路径一律视为项目内：它没有别的解释，用户写 `src/a.ts` 就是指项目里的那个。
 */
export function projectRelative(raw: string, root: string): { rel: string; inside: boolean } {
  const normalized = String(raw || '').trim().replace(/\\/g, '/')
  if (!normalized) return { rel: '', inside: true }

  const rootNorm = trimTrailingSlash(String(root || '').trim().replace(/\\/g, '/'))
  if (rootNorm) {
    const lower = normalized.toLowerCase()
    const rootLower = rootNorm.toLowerCase()
    if (lower === rootLower) return { rel: '', inside: true }
    if (lower.startsWith(`${rootLower}/`)) {
      return { rel: normalized.slice(rootNorm.length + 1), inside: true }
    }
  }

  // 根还不知道（树没加载过）时，绝对路径无法判断归属。按"不在项目内"处理：
  // 宁可暂时不显示一条标注，也不要把别的目录的文件算进这个项目的改动。
  if (isAbsolutePath(normalized)) return { rel: '', inside: false }

  return { rel: normalized.replace(/^\.\//, ''), inside: true }
}

/** 比较用的键：小写 + POSIX 分隔符（Windows 路径不区分大小写）。 */
export function pathKey(path: string): string {
  return String(path || '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\.\//, '')
    .toLowerCase()
}
