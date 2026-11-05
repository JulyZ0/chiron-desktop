/**
 * 从会话时间线里摘出「agent 这次改过哪些文件」。
 *
 * ## 为什么需要这一层
 *
 * 改动页签以 git 为准 —— 只有 git 知道工作区的真实状态，包括用户自己在编辑器里
 * 改的东西。但 git 有两个盲点：
 *
 * 1. 被 `.gitignore` 忽略的文件（构建产物、本地配置）里，agent 的写入**不会**出现在
 *    `git status` 里，用户会以为"它根本没改"；
 * 2. 改了又改回原样的文件，git 看是干净的，但用户仍然想知道"它碰过这里"。
 *
 * 所以这一层只回答一个更窄的问题：**agent 碰过哪些路径**。它不推断状态、不算增减行，
 * 那些一律交给 git；两边对不上时，界面要说清是"agent 改过但 git 没记录"。
 *
 * ## 为什么只认写工具
 *
 * `read_file` / `list_dir` 也有 `path` 参数。把它们算成"改过"，改动列表就会把整个
 * 读过的目录树都列出来 —— 那不是改动，是访问日志。
 */
import type { ChatItem } from '../chat/chat-types'
import { toolTargetPath } from '../../utils/toolFamily'

/**
 * 真正**写**文件的工具名。
 *
 * 逐条列出而不是按名字里有没有 "write"/"edit" 猜：`toolFamily.ts` 里已经记过
 * 这个坑（正则会把 `TodoWrite` 当成文件写入）。新增写工具时在这里补一行，
 * 漏了的表现只是"改动页签少一条标注"，不会给出错误信息。
 */
const WRITE_TOOL_NAMES = new Set(['write_file', 'edit_file'])

/**
 * 归一化路径用于**比较**（不是用于读取）。
 *
 * 同一份文件在不同工具参数里可能出现 `D:\proj\src\a.ts`、`./src/a.ts`、
 * `src\a.ts` 三种写法。比较时必须抹平，否则"agent 改过"的标注会时有时无。
 */
export function normalizeForCompare(path: string): string {
  return String(path || '')
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\.\//, '')
    .toLowerCase()
}

/**
 * 收集 agent 写过的路径（去重、保持首次出现顺序）。
 *
 * 返回的是**原始路径字符串**而不是归一化后的：调用方要用它去读文件或做绝对路径
 * 映射，归一化只该用于比较。比较时各自调用 `normalizeForCompare`。
 */
export function collectAgentWrittenPaths(items: ChatItem[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const item of items) {
    if (item.kind !== 'tool_call') continue
    if (!WRITE_TOOL_NAMES.has(item.name)) continue
    const path = toolTargetPath(item.name, item.arguments)
    if (!path) continue
    const key = normalizeForCompare(path)
    if (!key || seen.has(key)) continue
    seen.add(key)
    out.push(path)
  }
  return out
}
