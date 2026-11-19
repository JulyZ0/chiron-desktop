/**
 * 媒体页拖拽上传的**目录展开**（纯逻辑 + DataTransfer 遍历）。
 *
 * 浏览器在 drop 时只给一个 DataTransfer：文件夹在其中只是一个
 * `DataTransferItem`，真正的文件要自己递归读。这段逻辑有三处容易踩空，
 * 都属于"不报错、只是少传文件"的那一类，所以单独抽出来测：
 *
 * 1. `readEntries()` **一次最多返回 100 条**（Chrome/Firefox 的历史限制），
 *    必须循环读到空数组为止。读一次就当读完，会把大文件夹里第 101 个之后
 *    的文件静默丢掉 —— 用户看到的是"文件夹传了一半"。
 * 2. `webkitGetAsEntry()` 必须在事件处理器的**同步阶段**调用。一旦 await 过，
 *    DataTransfer 可能已被浏览器收回，之后拿到的全是 null。
 * 3. Windows 给的是反斜杠（`a\b\c.txt`），macOS/Linux 是正斜杠。同一个路径
 *    在两种机器上必须落到同一层级，否则同一个文件夹在不同系统上会建出
 *    `a\b` 这种带反斜杠的怪目录（后端 `splitFolderPath` 会直接 400 拒绝）。
 */

/** 一个待上传的文件，连同它相对"拖拽根"的目录层级。 */
export interface DroppedEntry {
  file: File
  /** 目录段，按从外到内排列。空数组表示直接落在当前目录。 */
  dirs: string[]
}

/**
 * 从拖拽结果里剔除的系统噪声文件。
 *
 * 拖 macOS 导出的文件夹几乎必带 `.DS_Store`，Windows 则常见 `Thumbs.db`、
 * `desktop.ini`。它们对用户毫无意义，却会真的出现在媒体库里 ——
 * 一个"拖进来多出 5 个看不见的东西"的媒体库比不传更让人困惑。
 */
const IGNORED_NAMES = new Set(['.ds_store', 'thumbs.db', 'desktop.ini', '.localized'])

/** macOS 在 zip/文件夹里塞的资源分支目录，整棵子树都该跳过。 */
const IGNORED_DIRS = new Set(['__macosx'])

/**
 * 单次拖拽允许的最大目录深度，与后端 `maxAssetFolderDepth`（16）取同一个数。
 *
 * 这里的前端截断是**唯一**的一道，别指望后端兜底：拖拽建目录是逐层 POST 单段名字
 * （`{name, parent_id}`），后端 `splitFolderPath` 每次只看到 1 段，深度校验永远
 * 不会触发；直传也只带 `parent_id`，同样不查绝对深度。也就是说放宽这个常量
 * 就能真的建出无限深的树。
 *
 * 取 16 是为了与按路径建目录/写资产（`POST /v1/media/folders` 传 `a/b/c`、
 * 内部接口传 `reports/2024/q3.md`）的口径一致 —— 同一棵树被两条入口建出来时，
 * 不该出现"拖进去能建、手输路径却 400"。
 */
export const MAX_DROP_DEPTH = 16

/** 目录层级的稳定标识（用 `/` 拼接，与后端路径语义一致）。 */
export function dirKey(dirs: readonly string[]): string {
  return dirs.join('/')
}

/** 媒体库已有目录的一条记录（`GET /v1/media/folders` 的形状）。 */
export interface ExistingFolder {
  id: string
  name: string
  parent_id: string
}

/**
 * 在已有目录表里查某条路径对应的目录 id。
 *
 * 逐段匹配而不是拼一条路径字符串去比：库里的 `parent_id` 有 `NULL` 与 `''`
 * 两种根表示（见 `media_crud.go` 的 `COALESCE` 注释），字符串路径比对会在
 * 根目录这一层全军覆没。逐段走只依赖 id，两种表示都能走通。
 *
 * `baseParentId` 是**起点**（当前所在目录）。少了它，站在 `2024/` 里拖入一个
 * `report/` 时会命中根目录下另一个同名 `report/`，文件被静默传去别处 ——
 * 媒体库里看起来就是"拖进来的东西不见了"。
 */
export function folderIdOf(
  existing: readonly ExistingFolder[],
  dirs: readonly string[],
  baseParentId = '',
): string | null {
  // 空链 = 起点自身。根（''）不是一个目录 id，所以返回 null 而不是空串 ——
  // 空串在调用方会被误当成"找到了一个 id 为空的目录"。
  if (dirs.length === 0) return baseParentId || null
  let parent = baseParentId
  let found: string | null = null
  for (const name of dirs) {
    const hit = existing.find(
      (f) => (f.parent_id || '') === parent && f.name === name,
    )
    if (!hit) return null
    parent = hit.id
    found = hit.id
  }
  return found
}

/** 是否为应当忽略的系统噪声文件名（大小写不敏感）。 */
export function isIgnoredDropName(name: string): boolean {
  return IGNORED_NAMES.has(name.trim().toLowerCase())
}

/** 是否为应当整棵跳过的系统噪声目录名。 */
export function isIgnoredDropDir(name: string): boolean {
  return IGNORED_DIRS.has(name.trim().toLowerCase())
}

/**
 * 规划一次拖拽要创建哪些目录，以及**哪些文件根本不该传**。
 *
 * `baseDepth` 是当前所在目录的深度（根为 0；媒体页 `breadcrumbs.length - 1`
 * 即为此值）。层级超限的文件必须在这里剔除 —— 后端的 `splitFolderPath` 会对
 * 超过 16 段的路径直接 400，而在上传完成阶段才失败意味着前面的目录都已建好、
 * 文件却一个都没落库，用户看到一堆空目录。
 */
export function planDrop(
  entries: readonly DroppedEntry[],
  baseDepth = 0,
): { entries: DroppedEntry[]; folders: string[][]; droppedDeep: number } {
  const kept: DroppedEntry[] = []
  let droppedDeep = 0
  for (const entry of entries) {
    if (baseDepth + entry.dirs.length > MAX_DROP_DEPTH) {
      droppedDeep++
      continue
    }
    kept.push(entry)
  }
  return { entries: kept, folders: planFolderCreation(kept), droppedDeep }
}

/**
 * 按「父先子后」的顺序把目录建出来，返回 `路径 → 目录 id`。
 *
 * `create` 由调用方注入（它才是唯一知道怎么打接口的地方），返回 `null` 表示
 * 这一次没建成。三个细节：
 *
 * 1. 建之前先查 `existing`：媒体库里通常已经有一批同名目录，重复 POST 只会
 *    拿到后端的 400「已存在」，然后被误报成失败。
 * 2. 某个目录失败时，**它的所有子孙都不再尝试**。否则每层都会各报一次错，
 *    用户看到的是一屏红字，而根因只有一个。
 * 3. 失败只记录路径，不抛异常：其余目录与文件照常处理，让"能传的"传上去。
 */
export async function ensureDropFolders(
  folders: readonly string[][],
  existing: readonly ExistingFolder[],
  create: (
    name: string,
    parentId: string,
  ) => Promise<ExistingFolder | null>,
  baseParentId = '',
): Promise<{ ids: Map<string, string>; failed: string[] }> {
  const known: ExistingFolder[] = [...existing]
  const ids = new Map<string, string>()
  const failed: string[] = []
  const broken = new Set<string>()

  for (const dirs of folders) {
    const key = dirKey(dirs)
    if (broken.has(key)) {
      failed.push(key)
      continue
    }
    // `baseParentId` 必须一起传：少了它，站在 `2024/` 里拖入 `report/` 会命中根下的
    // 另一个同名 `report/`，文件被静默传到别处，而这里的“已存在”判定还显示一切正常。
    const hit = folderIdOf(known, dirs, baseParentId)
    if (hit !== null) {
      ids.set(key, hit)
      continue
    }
    // 父级已经在上面处理过（`planFolderCreation` 保证父先子后），
    // 它失败时这里必然拿不到 id —— 直接把这个目录记为失败，不再发请求。
    const parentId = dirs.length > 1
      ? ids.get(dirKey(dirs.slice(0, -1)))
      : baseParentId
    if (parentId === undefined) {
      broken.add(key)
      failed.push(key)
      continue
    }
    const created = await create(dirs[dirs.length - 1], parentId)
    if (!created) {
      broken.add(key)
      failed.push(key)
      continue
    }
    known.push(created)
    ids.set(key, created.id)
  }
  return { ids, failed }
}

/**
 * 规划需要创建的目录，**父目录一定排在子目录之前**。
 *
 * 输入是所有待上传条目；输出是去重后的完整目录链（不含根）。例如拖入
 * `a/b/x.png` 与 `a/c/y.png` 会得到 `[['a'], ['a','b'], ['a','c']]` ——
 * 只建叶子目录会导致父路径不存在，后端 `ensureFolderChain` 直接 400。
 *
 * 依赖 `Array.prototype.sort` 的稳定性来保持同深度目录的发现顺序，
 * 这样"先建哪个"可预测（也让测试可断言）。
 */
export function planFolderCreation(entries: readonly DroppedEntry[]): string[][] {
  const seen = new Set<string>()
  const all: string[][] = []
  for (const entry of entries) {
    for (let depth = 1; depth <= entry.dirs.length; depth++) {
      const prefix = entry.dirs.slice(0, depth)
      const key = dirKey(prefix)
      if (seen.has(key)) continue
      seen.add(key)
      all.push(prefix)
    }
  }
  return all.sort((a, b) => a.length - b.length)
}

/** 递归读取一个目录的全部条目：`readEntries` 每次最多 100 条，必须读空为止。 */
function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  const out: FileSystemEntry[] = []
  return new Promise((resolve, reject) => {
    const step = () => {
      reader.readEntries((batch) => {
        if (batch.length === 0) {
          resolve(out)
          return
        }
        out.push(...batch)
        step()
      }, reject)
    }
    step()
  })
}

/** `FileSystemFileEntry.file()` 的回调式 API 包成 Promise。 */
function fileOf(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file((f) => resolve(f), () => resolve(null))
  })
}

async function collectDirectory(
  entry: FileSystemDirectoryEntry,
  parent: string[],
  out: DroppedEntry[],
): Promise<void> {
  if (isIgnoredDropDir(entry.name)) return
  const dirs = [...parent, entry.name]
  let children: FileSystemEntry[]
  try {
    children = await readAllEntries(entry.createReader())
  } catch {
    // 单个子目录读失败（权限、已被移动）不应让整个拖拽失败：
    // 其余文件照传，用户至少拿到能拿到的那些。
    return
  }
  for (const child of children) {
    if (child.isDirectory) {
      await collectDirectory(child as FileSystemDirectoryEntry, dirs, out)
    } else if (child.isFile) {
      const file = await fileOf(child as FileSystemFileEntry)
      if (!file || isIgnoredDropName(file.name)) continue
      out.push({ file, dirs })
    }
  }
}

/**
 * 把一次 drop 展开成「文件 + 所属目录」的扁平列表。
 *
 * `webkitGetAsEntry()` 在这里**同步**取完（见文件头注释第 2 条）。若浏览器不给
 * entry（老版本、或拖的是纯文件且实现省略了它），退化为 `dataTransfer.files`：
 * 至少把能看到的文件传上去，而不是让整个操作无声地什么都没发生。
 */
export async function collectDroppedEntries(dt: DataTransfer): Promise<DroppedEntry[]> {
  const items = dt.items ? Array.from(dt.items) : []
  // ⚠ 同步阶段先取 entry，之后才能 await。
  const roots: Array<FileSystemEntry | null> = items.map((it) =>
    typeof it.webkitGetAsEntry === 'function' ? it.webkitGetAsEntry() : null,
  )

  if (roots.every((r) => r === null)) {
    return Array.from(dt.files || [])
      .filter((f) => !isIgnoredDropName(f.name))
      .map((file) => ({ file, dirs: [] }))
  }

  const out: DroppedEntry[] = []
  for (const root of roots) {
    if (!root) continue
    if (root.isDirectory) {
      await collectDirectory(root as FileSystemDirectoryEntry, [], out)
    } else if (root.isFile) {
      const file = await fileOf(root as FileSystemFileEntry)
      if (!file || isIgnoredDropName(file.name)) continue
      out.push({ file, dirs: [] })
    }
  }
  return out
}

/** 拖拽里是否真的带文件（拖选中的文本也会触发 dragover，不该显示上传遮罩）。 */
export function dragHasFiles(dt: DataTransfer | null | undefined): boolean {
  if (!dt) return false
  const types = dt.types ? Array.from(dt.types as ArrayLike<string>) : []
  return types.includes('Files')
}

/**
 * 受控并发地跑一批任务，返回**按输入顺序**排列的结果。
 *
 * 为什么不能直接 `Promise.all(items.map(upload))`：拖进来一个 500 张图的文件夹时，
 * 500 个分片上传会同时打开上千个请求（`createChunkUpload` 内部还有 3 路并发），
 * 浏览器连接池被打满，连页面自己的接口都会被挤到超时 —— 表现是"拖文件夹进来整个
 * 应用卡死"。串行又太慢。按固定并发跑是唯一两头都不坏的选择。
 *
 * 单个任务抛错**不会中断整批**：拖拽上传的正确语义是"能传的传上去，
 * 失败的最后汇总"，而不是第一个文件撞到重名就全盘放弃。
 */
export async function runWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>,
): Promise<Array<{ ok: true; value: R } | { ok: false; error: unknown }>> {
  const results = new Array<{ ok: true; value: R } | { ok: false; error: unknown }>(items.length)
  let cursor = 0
  const width = Math.max(1, Math.min(limit, items.length))
  const run = async () => {
    while (true) {
      const i = cursor++
      if (i >= items.length) return
      try {
        results[i] = { ok: true, value: await worker(items[i], i) }
      } catch (error) {
        results[i] = { ok: false, error }
      }
    }
  }
  await Promise.all(Array.from({ length: width }, run))
  return results
}
