/**
 * 项目文件的数据面（只读）。
 *
 * 与 `./projectsApi.ts` 的分工：那边管项目本体与会话归属，这里管**项目里有哪些文件、
 * 文件长什么样**。两者都挂在 `/v1/session-map/projects/{id}` 之下，但语义不同。
 *
 * **只读**：写入仍然只由 agent 的文件工具完成。前端不提供第二个写入口 —— 两个入口
 * 意味着两套权限判断与两套并发保护，早晚有一条绕过审计（后端侧的理由见
 * `internal/api/project_files.go` 文件头）。
 *
 * 错误处理：与 `projectsApi.ts` 一致，**不吞异常**。调用方按 `code` 分支
 * （`project_root_unavailable` 要引导用户重选文件夹，`file_too_large` 要引导他改用
 * 系统程序打开 —— 两者的下一步动作完全不同）。
 */
import { api } from './index'

/** 目录列表里的一项。`path` 是相对项目根的 POSIX 路径。 */
export interface ProjectFileEntry {
  name: string
  path: string
  type: 'file' | 'dir'
  /** 只对文件有意义；目录恒为 0 / 缺省 */
  size?: number
}

/** 一层目录的读取结果。 */
export interface ProjectListing {
  /** 项目根**绝对路径**，用于界面上确认"我看的是哪个文件夹" */
  root: string
  /** 本次列出的相对路径；根目录为空串 */
  path: string
  entries: ProjectFileEntry[]
  /** 条目数触到后端上限、列表被截断。界面必须说出来，否则会被误读成"只有这些" */
  truncated: boolean
}

/** 一个文件的文本读取结果。 */
export interface ProjectFileContent {
  path: string
  /** 为真时 `content` 一定为空：无法当文本读的文件不是错误 */
  binary: boolean
  size: number
  content?: string
  total_lines?: number
  /** 本次内容窗口的起始行（0-based）。后端按行分屏时给出。 */
  offset?: number
  /**
   * 窗口之外还有内容。
   *
   * **必须**与 `total_lines` 一起用：后端对大文件只返回一屏，此时 `content` 的行数
   * 恰好等于 `PREVIEW_LINE_LIMIT`，前端的本地截断会算出 `dropped: 0` ——
   * 只看本地截断就会得出"文件已经完整"的错误结论。
   */
  truncated?: boolean
}

function normalizeEntry(raw: unknown): ProjectFileEntry | null {
  const e = (raw ?? {}) as Partial<ProjectFileEntry>
  if (typeof e.path !== 'string' || e.path === '') return null
  return {
    name: typeof e.name === 'string' ? e.name : e.path.split('/').pop() || e.path,
    path: e.path,
    // 后端只会给 'file' / 'dir'；未知值按文件处理（点开失败比当成目录展开失败更好解释）
    type: e.type === 'dir' ? 'dir' : 'file',
    size: typeof e.size === 'number' ? e.size : 0,
  }
}

function encodePath(path: string): string {
  return encodeURIComponent(path)
}

/**
 * 列出一层目录（不递归）。
 *
 * 懒加载是刻意的：前端展开哪个节点就请求哪一层。一次性返回整棵树在大仓库上是
 * 秒级开销，而用户可能只是想知道根目录里有没有那个文件。
 */
export async function listProjectFiles(
  projectId: string,
  path = '',
  options: { hidden?: boolean } = {},
): Promise<ProjectListing> {
  const params = new URLSearchParams()
  if (path) params.set('path', path)
  if (options.hidden) params.set('hidden', '1')
  const qs = params.toString()
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/files${qs ? `?${qs}` : ''}`,
  )
  const payload = data?.data ?? {}
  const list: unknown[] = Array.isArray(payload.entries) ? payload.entries : []
  return {
    root: typeof payload.root === 'string' ? payload.root : '',
    path: typeof payload.path === 'string' ? payload.path : path,
    entries: list.map(normalizeEntry).filter((e): e is ProjectFileEntry => e !== null),
    truncated: !!payload.truncated,
  }
}

/**
 * 按名字跨目录搜索文件 —— `@` 引用的数据源。
 *
 * 与 `listProjectFiles` 分开：这条的语义是"我大概记得它叫什么"，所以后端会递归并
 * 自己排序（文件名命中优先于路径命中），也会按预算截断。
 *
 * `signal` 用于取消上一次未完成的搜索：用户在输入框里连打几个字符时，每次击键都会
 * 让服务端重新递归整棵项目树。只丢弃结果（旧做法）并不能省掉那几次遍历 —— 请求
 * 仍然跑完，只是白跑；大仓库上这是实打实的 CPU 与磁盘开销。
 */
export async function searchProjectFiles(
  projectId: string,
  query: string,
  limit = 0,
  signal?: AbortSignal,
): Promise<{ files: ProjectFileEntry[]; truncated: boolean }> {
  const params = new URLSearchParams({ q: query })
  if (limit > 0) params.set('limit', String(limit))
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/files/search?${params.toString()}`,
    signal ? { signal } : undefined,
  )
  const payload = data?.data ?? {}
  const list: unknown[] = Array.isArray(payload.files) ? payload.files : []
  return {
    files: list.map(normalizeEntry).filter((e): e is ProjectFileEntry => e !== null),
    truncated: !!payload.truncated,
  }
}

/** 读取一个文件的文本内容。二进制文件返回 `binary: true`，不是错误。 */
export async function readProjectFile(
  projectId: string,
  path: string,
  /** 起始行（0-based）。超过一屏的文件靠它继续往下翻。 */
  offset = 0,
  /** 本屏最多多少行；0 交给后端默认（等于 PREVIEW_LINE_LIMIT）。 */
  limit = 0,
): Promise<ProjectFileContent> {
  const params = new URLSearchParams()
  params.set('path', path)
  if (offset > 0) params.set('offset', String(offset))
  if (limit > 0) params.set('limit', String(limit))
  const { data } = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/file?${params.toString()}`,
  )
  const payload = data?.data ?? {}
  return {
    path: typeof payload.path === 'string' ? payload.path : path,
    binary: !!payload.binary,
    size: typeof payload.size === 'number' ? payload.size : 0,
    content: typeof payload.content === 'string' ? payload.content : undefined,
    total_lines: typeof payload.total_lines === 'number' ? payload.total_lines : undefined,
    offset: typeof payload.offset === 'number' ? payload.offset : undefined,
    truncated: !!payload.truncated,
  }
}

/**
 * 取文件的原始字节，返回可直接放进 `<img src>` / `<iframe>` 的 objectURL。
 *
 * **为什么必须绕一圈 blob**：这个端点走 Bearer 鉴权，而 `<img src="…">` 无法携带
 * `Authorization` 头（`<img>` 不支持自定义头，也没有 cookie 可用 —— 桌面端是
 * `file://` 跨站）。直接写 URL 只会得到 401。
 *
 * 调用方**必须**在预览结束或切换文件时 `URL.revokeObjectURL(url)`，否则每一张
 * 打开过的图片都会把整个字节留在内存里。
 */
export async function projectFileObjectURL(projectId: string, path: string): Promise<string> {
  const res = await api.get(
    `/v1/session-map/projects/${encodeURIComponent(projectId)}/file/raw?path=${encodePath(path)}`,
    { responseType: 'blob' },
  )
  return URL.createObjectURL(res.data as Blob)
}

/**
 * 给 `<img>` 用的判定：按扩展名猜"能不能当图片看"。
 *
 * 不看后端返回的 Content-Type：那要再发一次请求才知道，而这个判断发生在**决定
 * 要不要发请求**的时候。后端仍会用扩展名给 Content-Type 并加 nosniff，所以猜错
 * 的后果只是"多打开一个打不开的预览"，不会变成安全事件。
 */
const IMAGE_EXTS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif', 'svg',
])

export function fileExt(path: string): string {
  const base = path.split('/').pop() ?? path
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : ''
}

export function isImagePath(path: string): boolean {
  return IMAGE_EXTS.has(fileExt(path))
}

/** PDF 单独识别：它要的是 `<iframe>` / `<embed>`，而不是 `<img>`。 */
export function isPdfPath(path: string): boolean {
  return fileExt(path) === 'pdf'
}
