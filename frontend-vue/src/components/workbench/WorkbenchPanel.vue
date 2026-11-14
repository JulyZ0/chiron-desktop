<script setup lang="ts">
/**
 * 工作台：当前项目目录的文件树 + 只读预览。
 *
 * 三个"必须说清楚"的设计点：
 *
 * 1. **只读**。写入只由 agent 的文件工具完成（理由见 `internal/api/project_files.go`
 *    文件头：两个写入口 = 两套权限判断 + 两套并发保护，早晚有一条绕过审计）。
 *    界面上用一条常驻横幅说明，而不是让用户点进去才发现改不了 —— 那是幽灵行为。
 *
 * 2. **懒加载**。展开哪一层才请求哪一层。大仓库一次性拉整棵树是秒级开销，而用户
 *    多数时候只是想确认"那个文件在不在"。
 *
 * 3. **二进制不是错误**。图片/PDF 走 blob objectURL 预览，其它二进制文件明确说
 *    "无法预览"并给出路径，让用户能用系统程序打开，而不是转圈或报错。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  isImagePath,
  isPdfPath,
  listProjectFiles,
  projectFileObjectURL,
  readProjectFile,
  type ProjectFileEntry,
} from '../../api/projectFiles'
// 复用消息区那套高亮（懒加载 highlight.js + 行号 + 跨行 span 闭合）。
// 不另写一份：同一仓里两套高亮必然在语言映射与转义上漂移。
import { highlightCodeBlocks } from '../chat/codeHighlighter'
import { langOf, truncatePreview } from './previewCode'
import { listProjectChanges, type ProjectChange, type ProjectChanges } from '../../api/projectChanges'
import { pathKey, projectRelative } from './paths'
import { describeApiError } from '../../utils/apiError'
import { notify } from '../../utils/notify'
import { desktopHost } from '../../stores/device'
import { revealRequest, WORKBENCH_WIDTH_KEY } from './useWorkbench'

const { t: tr } = useI18n()

const props = withDefaults(
  defineProps<{
    /** 当前会话所属项目；空串 = 没有项目文件可看 */
    projectId?: string
    /** 项目名（头部展示，让用户确认"我看的是哪个项目"） */
    projectName?: string
    /**
     * 反查会话归属**失败**的原因（空串 = 没失败）。
     *
     * 与 `projectId` 为空是两件事：空 id 是"这会话确实没归入项目"（正常状态），
     * 而查询失败是故障。若把后者也渲染成「未归入任何项目」，用户刚拖进项目的
     * 会话就会显示成没归入 —— 自相矛盾且没有线索。
     */
    projectError?: string
    /** 分栏手柄贴哪条边：由父布局的 `is-swapped` 决定 */
    handleSide?: 'left' | 'right'
    /**
     * 本次会话里 agent 写过的文件路径（原始写法，未归一化）。
     *
     * 用于给改动列表加一条"agent 改过"的标注：git 看不到被 .gitignore 忽略的文件，
     * 也看不到"改了又改回去"的文件，而用户仍然想知道它碰过哪里。
     */
    agentWrittenPaths?: string[]
  }>(),
  // `handleSide` 兜底 'right'：默认布局下工作台在**最左**，与主会话相邻的是它的
  // 右边缘，手柄就该贴右边。兜底反了的话，只要调用方漏传，手柄会跑到窗口外缘，
  // 用户就再也拖不动这一栏了。
  { projectId: '', projectName: '', handleSide: 'right', agentWrittenPaths: () => [] },
)

const emit = defineEmits<{
  (e: 'close'): void
  /** 把文件交给对话（父组件转成附件）；`path` 是相对项目根的 POSIX 路径 */
  (e: 'reference', payload: { path: string; name: string }): void
  /**
   * 重新反查会话归属。
   *
   * 归属查询失败时面板只能显示错误，**重试必须由父组件执行** —— 项目归属是
   * ChatView 的状态（`activeProject`），面板拿不到会话 id，自己重试也无处可写。
   */
  (e: 'retry-project'): void
}>()

// ── 分栏宽度（与 SessionPreviewPane 同一套交互约定） ──
const MIN_WIDTH = 300
/** 上限取容器的一半：工作台与参考分屏可能同时打开，各占一半才都还能用 */
const MAX_RATIO = 0.5
const DEAD_ZONE_PX = 4

const paneRef = ref<HTMLElement | null>(null)
const paneWidth = ref<number | null>(readStoredWidth())
const resizing = ref(false)

function readStoredWidth(): number | null {
  try {
    const raw = localStorage.getItem(WORKBENCH_WIDTH_KEY)
    if (!raw) return null
    const n = Number.parseInt(raw, 10)
    return Number.isFinite(n) && n > 0 ? n : null
  } catch {
    return null
  }
}

function persistWidth() {
  try {
    if (paneWidth.value) localStorage.setItem(WORKBENCH_WIDTH_KEY, String(paneWidth.value))
    else localStorage.removeItem(WORKBENCH_WIDTH_KEY)
  } catch {
    /* 存不下就只在本次运行内生效 */
  }
}

function clampWidth(next: number): number {
  const container = paneRef.value?.parentElement?.getBoundingClientRect().width ?? 1200
  const max = Math.max(MIN_WIDTH, Math.floor(container * MAX_RATIO))
  return Math.min(max, Math.max(MIN_WIDTH, Math.round(next)))
}

let pressing = false
let startX = 0
let startWidth = 0

function onResizeStart(e: PointerEvent) {
  const el = paneRef.value
  if (!el) return
  pressing = true
  resizing.value = false
  startX = e.clientX
  startWidth = el.getBoundingClientRect().width
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
}

function onResizeMove(e: PointerEvent) {
  if (!pressing) return
  const dx = e.clientX - startX
  if (!resizing.value) {
    // 死区：轻点手柄或手抖不该被当成拖拽，更不该写进本地记忆
    if (Math.abs(dx) < DEAD_ZONE_PX) return
    resizing.value = true
    window.addEventListener('keydown', onResizeEscape, true)
  }
  // 手柄在左（面板在右）时向右拖是变窄
  const signed = props.handleSide === 'left' ? -dx : dx
  paneWidth.value = clampWidth(startWidth + signed)
}

function onResizeEnd(e: PointerEvent) {
  pressing = false
  ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
  if (!resizing.value) return
  resizing.value = false
  window.removeEventListener('keydown', onResizeEscape, true)
  persistWidth()
}

function onResizeEscape(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !resizing.value) return
  resizing.value = false
  pressing = false
  paneWidth.value = readStoredWidth()
  window.removeEventListener('keydown', onResizeEscape, true)
}

function onResizeKey(e: KeyboardEvent) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  e.preventDefault()
  const base = paneWidth.value ?? paneRef.value?.getBoundingClientRect().width ?? MIN_WIDTH
  const step = props.handleSide === 'left' ? -16 : 16
  const next = clampWidth(base + (e.key === 'ArrowLeft' ? -step : step))
  paneWidth.value = next
  persistWidth()
}

function resetWidth() {
  paneWidth.value = null
  persistWidth()
}

const paneStyle = computed(() =>
  paneWidth.value ? { width: `${paneWidth.value}px`, flex: '0 0 auto' } : {},
)
onBeforeUnmount(() => window.removeEventListener('keydown', onResizeEscape, true))

// ── 文件树（扁平化渲染：避免递归组件，键盘遍历也更直接） ──
const rootPath = ref('')
const childrenOf = ref<Record<string, ProjectFileEntry[]>>({})
const expanded = ref<Set<string>>(new Set())
const loadingDirs = ref<Set<string>>(new Set())
const treeError = ref('')
/**
 * 树的代次：每次 `reload()` 自增。
 *
 * 为什么需要它：`loadDir` 是异步的，而 `reload()` 会把 `childrenOf` 清空后重新拉根。
 * 用户切项目时，上一个项目那几个正在飞的请求回来得晚，直接写进 `childrenOf` 就会
 * 让新项目的树里混着旧项目的文件 —— 而且树看着不为空，用户不会想到去刷新。
 * 更糟的是 `loadingDirs` 也得跟着清，否则新的 `loadDir('')` 会被
 * `loadingDirs.has('')` 直接挡掉，新项目永远停在"加载中"。
 */
let treeGeneration = 0
/**
 * 被截断的目录集合。
 *
 * 必须是**按目录**记录而不是一个布尔量：同一个仓库里，根目录可能 20 条全显示，
 * 而 `node_modules` 的某一层触到上限。用一个布尔量的话，用户在根目录也会看到
 * "条目过多"的提示 —— 那是一句与眼前内容矛盾的假话（也清不掉）。
 */
const truncatedDirs = ref<Set<string>>(new Set())
const showHidden = ref(false)

/** 可见行 = 从根开始，按展开状态深度优先展开的结果 */
interface Row {
  entry: ProjectFileEntry
  depth: number
}
const rows = computed<Row[]>(() => {
  const out: Row[] = []
  const walk = (prefix: string, depth: number) => {
    for (const entry of childrenOf.value[prefix] ?? []) {
      out.push({ entry, depth })
      if (entry.type === 'dir' && expanded.value.has(entry.path)) walk(entry.path, depth + 1)
    }
  }
  walk('', 0)
  return out
})

/** 只有**当前真正看得到的**那一层被截断时，才把提示说出来 */
const truncatedVisible = computed(() => {
  if (truncatedDirs.value.has('')) return true
  return rows.value.some(
    r => r.entry.type === 'dir' && expanded.value.has(r.entry.path) && truncatedDirs.value.has(r.entry.path),
  )
})

/** 当前在预览区打开的文件（`null` = 只显示文件树） */
const selected = ref<ProjectFileEntry | null>(null)

// ── 改动页签（只读 Git 视图） ──
//
// 数据源是**两条**：git 给工作区的真实状态（含用户在编辑器里改的），会话记录给
// "agent 碰过哪些文件"。后者补的正是 git 的盲点 —— 被 .gitignore 忽略的写入，
// 以及"改了又改回去"（git 看是干净的，但用户仍想知道它动过这里）。
//
// 这里**没有**暂存 / 丢弃 / 提交：它们都会写用户的仓库，其中"丢弃"真的会回退
// 工作区。只读视图不提供需要撤销设计的按钮。
const activeTab = ref<'files' | 'changes'>('files')
const changesLoading = ref(false)
const changesError = ref('')
const changesData = ref<ProjectChanges | null>(null)

/** agent 写过的路径 → 相对项目根、可比较的键。落在项目外的直接丢弃。 */
const agentKeys = computed<Set<string>>(() => {
  const out = new Set<string>()
  for (const raw of props.agentWrittenPaths ?? []) {
    const { rel, inside } = projectRelative(raw, rootPath.value)
    if (inside && rel) out.add(pathKey(rel))
  }
  return out
})

/** 改动列表的一行：git 的条目，加上只在会话里出现过的那几个。 */
interface ChangeRow {
  path: string
  oldPath?: string
  status: string
  insertions?: number
  deletions?: number
  agentTouched: boolean
  /** git 没有记录，只凭 agent 的写入记录出现 */
  agentOnly: boolean
}

const changeRows = computed<ChangeRow[]>(() => {
  const agent = agentKeys.value
  const rows: ChangeRow[] = []
  const seen = new Set<string>()

  for (const c of changesData.value?.changes ?? []) {
    const key = pathKey(c.path)
    seen.add(key)
    rows.push({
      path: c.path,
      oldPath: c.old_path,
      status: c.status,
      insertions: c.insertions,
      deletions: c.deletions,
      agentTouched: agent.has(key),
      agentOnly: false,
    })
  }

  // 合并：agent 写过、但 git 一个字都没看见的路径。它们不是"零改动"，而是
  // "git 不管这块"（被忽略 / 改了又还原），所以单独标出来而不是混进状态字母里。
  for (const raw of props.agentWrittenPaths ?? []) {
    const { rel, inside } = projectRelative(raw, rootPath.value)
    if (!inside || !rel) continue
    const key = pathKey(rel)
    if (seen.has(key)) continue
    seen.add(key)
    rows.push({ path: rel, status: '', agentTouched: true, agentOnly: true })
  }
  return rows
})

/** 页签上的角标：只有真的读到 git 数据时才显示，避免"改动 0"这种噪音 */
const changeCount = computed(() => {
  const data = changesData.value
  if (!data?.repo) return 0
  return changeRows.value.filter(r => !r.agentOnly).length
})

async function loadChanges() {
  if (!props.projectId || changesLoading.value) return
  changesLoading.value = true
  changesError.value = ''
  try {
    changesData.value = await listProjectChanges(props.projectId)
  } catch (e) {
    // 错误留在页签里就地显示：用户正看着这块，弹到角落等于没说
    changesError.value = describeApiError(e)
  } finally {
    changesLoading.value = false
  }
}

/**
 * 打开一条改动。
 *
 * 删除的文件没有内容可预览 —— 明说，而不是让预览区去读一个不存在的路径再报
 * "文件不存在"（那会让人以为文件丢了）。
 */
async function openChange(row: ChangeRow) {
  if (row.status === 'D') {
    notify.info(tr('chat.workbench_change_deleted'))
    return
  }
  activeTab.value = 'files'
  await revealPath(row.path)
}


async function loadDir(path: string) {
  if (!props.projectId) return
  if (loadingDirs.value.has(path)) return
  // 代次 + 项目 id 都取值**再 await**：请求飞在路上时用户可能切了项目，那时
  // `props.projectId` 已经是新值，拿它去写结果就会把新项目的 id 安在旧数据上。
  const generation = treeGeneration
  const projectId = props.projectId
  loadingDirs.value = new Set(loadingDirs.value).add(path)
  try {
    const listing = await listProjectFiles(projectId, path, { hidden: showHidden.value })
    // 期间 reload 过（切项目 / 切隐藏开关 / 手动刷新）：这份结果属于上一棵树，
    // 写进去就是"新项目的树里混着旧项目的文件"，而且树看着不是空的，
    // 用户根本不会想到去点刷新。丢弃比落盘安全。
    if (generation !== treeGeneration) return
    rootPath.value = listing.root || rootPath.value
    childrenOf.value = { ...childrenOf.value, [path]: listing.entries }
    // 截断状态**每次刷新都重算**：之前被截断的目录这次可能没截断（文件被删了），
    // 只置位不清位的话，提示会一直挂在那里，而它描述的事实已经不成立。
    const nextTrunc = new Set(truncatedDirs.value)
    if (listing.truncated) nextTrunc.add(path)
    else nextTrunc.delete(path)
    truncatedDirs.value = nextTrunc
    treeError.value = ''
  } catch (e) {
    // 错误留在树里就地显示：用户正在看这个目录，把提示弹到别处等于没说
    treeError.value = describeApiError(e)
  } finally {
    const next = new Set(loadingDirs.value)
    next.delete(path)
    loadingDirs.value = next
  }
}

function toggleDir(entry: ProjectFileEntry) {
  const next = new Set(expanded.value)
  if (next.has(entry.path)) {
    next.delete(entry.path)
  } else {
    next.add(entry.path)
    if (!childrenOf.value[entry.path]) void loadDir(entry.path)
  }
  expanded.value = next
}

/** 重置整棵树（切项目/切隐藏开关/手动刷新） */
function reload() {
  // 代次先自增：下面 `loadDir('')` 之后每一个在飞的旧请求都会因代次不符而被丢弃。
  treeGeneration += 1
  childrenOf.value = {}
  expanded.value = new Set()
  // `loadingDirs` 必须一起清。不清的话，切项目时若上一个根目录请求还在飞，
  // 下面那句 `loadDir('')` 会被 `loadingDirs.has('')` 挡掉并直接返回 ——
  // 新项目的树永远不加载，面板停在"加载中"，而刷新按钮又走同一条死路。
  loadingDirs.value = new Set()
  truncatedDirs.value = new Set()
  treeError.value = ''
  selected.value = null
  // 改动缓存必须一起清：切了项目还留着上一个项目的改动列表，
  // 点进去会得到"文件不在项目内"—— 比空列表更让人困惑。
  changesData.value = null
  changesError.value = ''
  void loadDir('')
  if (activeTab.value === 'changes') void loadChanges()
}

/** 切页签：改动页签首次进入时才请求（用户不开这一页就不该有这次 git 调用） */
function selectTab(tab: 'files' | 'changes') {
  activeTab.value = tab
  if (tab === 'changes' && !changesData.value && !changesLoading.value) void loadChanges()
}

watch(
  () => [props.projectId, showHidden.value] as const,
  () => reload(),
  { immediate: true },
)

defineExpose({ reload })

// ── 预览 ──
const previewText = ref('')
const previewBinary = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
/** 当前 objectURL：切换文件必须 revoke，否则每开一张图都留一份字节在内存里。
 *  **必须是 ref**：模板要靠它决定渲染 `<img>` 还是"无法预览" —— 普通变量赋值
 *  不会触发重渲染，图片会一直显示空白。 */
const activeObjectURL = ref('')

function revokePreviewURL() {
  if (activeObjectURL.value) {
    URL.revokeObjectURL(activeObjectURL.value)
    activeObjectURL.value = ''
  }
}

function clearPreview() {
  revokePreviewURL()
  previewText.value = ''
  previewBinary.value = false
  previewError.value = ''
  previewLang.value = ''
  previewTruncatedLines.value = 0
}

const previewLang = ref('')
/** 被截断时告诉用户还剩多少行没显示 —— 不告知就等于假装文件只有这么长 */
const previewTruncatedLines = ref(0)
const previewHost = ref<HTMLElement | null>(null)

/** 挂载后按需增强：高亮是纯增量，失败也只是少个配色，代码本身始终可读可复制。 */
async function enhancePreview() {
  await nextTick()
  const host = previewHost.value
  if (host) await highlightCodeBlocks(host)
}

async function selectFile(entry: ProjectFileEntry) {
  selected.value = entry
  clearPreview()
  if (!props.projectId) return
  previewLoading.value = true
  try {
    if (isImagePath(entry.path) || isPdfPath(entry.path)) {
      // <img>/<iframe> 无法携带 Authorization 头，只能取 blob 再转 objectURL
      activeObjectURL.value = await projectFileObjectURL(props.projectId, entry.path)
      previewBinary.value = true
      return
    }
    const file = await readProjectFile(props.projectId, entry.path)
    previewBinary.value = file.binary
    // 后端对超过 2MB 的文本直接返回 `file_too_large`（不返回一段被截断的内容），
    // 所以这里不需要"内容可能不完整"的判断 —— 能拿到内容就是完整的。
    previewLang.value = langOf(entry.path)
    // 大文件在**渲染前**截断：塞进 DOM 之后再想办法隐藏没有意义，
    // 卡住的是渲染本身，不是可见性（见 previewCode.ts 的 PREVIEW_LINE_LIMIT）。
    const cut = truncatePreview(file.content ?? '')
    previewText.value = cut.text
    previewTruncatedLines.value = cut.dropped
  } catch (e) {
    previewError.value = describeApiError(e)
  } finally {
    // **必须先关 loading 再增强**：预览正文挂在 `v-else` 分支上，而那条分支只在
    // `previewLoading === false` 时才渲染。在 try 里调 enhancePreview 拿到的是
    // 上一屏（甚至 null）的 host，高亮与行号于是被静默跳过 —— 界面照常显示纯文本，
    // 看不出任何异常，只是"高亮没生效"。这是本功能最初的真实故障。
    previewLoading.value = false
  }
  await enhancePreview()
}

/**
 * 展开到某个文件并预览它。
 *
 * 逐层 `loadDir` 而不是"一次列出全树"：树是懒加载的，中间目录没展开过就没有
 * 子项；不补这一步，跳转过来的文件在树里根本不存在，用户会以为链接坏了。
 */
async function revealPath(rawPath: string) {
  if (!props.projectId) return
  const normalized = String(rawPath || '').trim().replace(/\\/g, '/')
  if (!normalized) return

  // 绝对路径是常见输入（工具参数、正文里的路径都是绝对路径）。
  // 只有落在当前项目根内才能映射；映射不到就明确告知，而不是猜一个相对路径去读。
  let rel = normalized
  const root = rootPath.value.replace(/\\/g, '/').replace(/\/+$/, '')
  if (root && rel.toLowerCase().startsWith(`${root.toLowerCase()}/`)) {
    rel = rel.slice(root.length + 1)
  } else if (/^[a-zA-Z]:\//.test(rel) || rel.startsWith('/')) {
    // 绝对路径但不在这个项目里：先确保树加载过（rootPath 才有值）再判一次
    if (!root) {
      await loadDir('')
      const root2 = rootPath.value.replace(/\\/g, '/').replace(/\/+$/, '')
      if (root2 && rel.toLowerCase().startsWith(`${root2.toLowerCase()}/`)) {
        rel = rel.slice(root2.length + 1)
      }
    }
    if (/^[a-zA-Z]:\//.test(rel) || rel.startsWith('/')) {
      notify.info(tr('chat.workbench_path_outside_project', { path: normalized }))
      return
    }
  }

  const parts = rel.split('/').filter(Boolean)
  if (!parts.length) return

  // 展开父目录链
  let prefix = ''
  for (let i = 0; i < parts.length - 1; i++) {
    prefix = prefix ? `${prefix}/${parts[i]}` : parts[i]
    expanded.value = new Set(expanded.value).add(prefix)
    if (!childrenOf.value[prefix]) await loadDir(prefix)
  }

  const parent = parts.slice(0, -1).join('/')
  const entry = (childrenOf.value[parent] ?? []).find(e => e.path === rel)
  if (!entry) {
    notify.info(tr('chat.workbench_file_not_found', { path: normalized }))
    return
  }
  if (entry.type === 'dir') {
    expanded.value = new Set(expanded.value).add(entry.path)
    if (!childrenOf.value[entry.path]) await loadDir(entry.path)
    return
  }
  await selectFile(entry)
  // 树是滚动容器：把选中的行滚进视野，否则"打开了"但用户看不到高亮在哪
  await nextTick()
  paneRef.value?.querySelector('.wb-row.active')?.scrollIntoView({ block: 'nearest' })
}

/**
 * 响应外部的「打开这个文件」请求（正文里的路径、工具卡片里的路径都经此进来）。
 *
 * 首次挂载时不响应 `immediate`：请求可能是在面板关着时发出的，此时 `projectId`
 * 可能还没解析出来；等根目录加载完（`rootPath` 有值）再处理才映射得准。
 */
watch(
  () => revealRequest.value?.seq,
  async seq => {
    if (!seq) return
    const req = revealRequest.value
    if (!req) return
    await revealPath(req.path)
  },
)

watch(selected, (next, prev) => {
  // 切文件时先释放旧 objectURL（selectFile 里 clearPreview 已做，这里兜住 null 的路径）
  if (!next && prev) revokePreviewURL()
})
onBeforeUnmount(revokePreviewURL)

function referenceSelected() {
  const entry = selected.value
  if (!entry) return
  emit('reference', { path: entry.path, name: entry.name })
}

/** 复制相对路径：这是用户最常要拿去粘到对话里的东西 */
async function copyPath(entry: ProjectFileEntry) {
  try {
    await navigator.clipboard.writeText(entry.path)
  } catch {
    /* 无剪贴板权限时静默失败：路径就在界面上，用户可以自己选中复制 */
  }
}

/**
 * 相对路径 → 绝对路径。
 *
 * 分隔符跟着 `rootPath` 走：Windows 上根是 `D:\proj`，直接拼 POSIX 相对路径会得到
 * `D:\proj/src/a.ts`。Node 的 `fs` 能吃混合分隔符，但把这种串交给用户或日志里看
 * 都是错的，所以在这里一次摆正。
 */
function toAbsolute(rel: string): string {
  const root = rootPath.value.replace(/[\\/]+$/, '')
  if (!root) return rel
  const sep = root.includes('\\') ? '\\' : '/'
  return `${root}${sep}${rel.split('/').join(sep)}`
}

/**
 * 用系统默认程序打开当前文件。
 *
 * 二进制文件本来就无法预览，提示里写着"用系统程序打开" —— 那就必须真的有一个
 * 能按的地方。Web 端没有这层能力时明说，而不是点了一下什么都不发生。
 */
async function openWithSystem(entry: ProjectFileEntry) {
  const host = desktopHost()
  if (!host?.openPath) {
    notify.info(tr('chat.workbench_open_needs_desktop'))
    return
  }
  try {
    const res = await host.openPath({ path: toAbsolute(entry.path) })
    if (!res?.ok) {
      notify.error(tr('chat.workbench_open_failed', { message: res?.message || res?.reason || '' }))
    }
  } catch (e) {
    notify.error(tr('chat.workbench_open_failed', { message: describeApiError(e) }))
  }
}
</script>

<template>
  <aside
    ref="paneRef"
    class="workbench-panel"
    :class="[`handle-${handleSide}`, { resizing }]"
    :style="paneStyle"
  >
    <div
      class="wb-resize"
      :class="`handle-${handleSide}`"
      role="separator"
      aria-orientation="vertical"
      :aria-label="tr('auth.drag_to_resize_double_click_to_reset')"
      tabindex="0"
      @pointerdown="onResizeStart"
      @pointermove="onResizeMove"
      @pointerup="onResizeEnd"
      @pointercancel="onResizeEnd"
      @dblclick="resetWidth"
      @keydown="onResizeKey"
    />

    <header class="wb-head">
      <span
        class="wb-title"
        :title="rootPath || projectName"
      >{{ projectName || tr('chat.workbench_title') }}</span>
      <label
        class="wb-toggle"
        :title="tr('chat.workbench_show_hidden')"
      >
        <input
          v-model="showHidden"
          type="checkbox"
        >
        <span>.*</span>
      </label>
      <button
        type="button"
        class="wb-btn"
        :title="tr('common.refresh')"
        @click="reload"
      >⟳</button>
      <button
        type="button"
        class="wb-btn"
        :title="tr('common.close')"
        @click="emit('close')"
      >×</button>
    </header>

    <!-- 只读横幅：常驻而不是首次提示。用户随时可能忘了这件事，
         而"改了没生效"的下一步动作是怀疑软件坏了。 -->
    <div class="wb-readonly">
      {{ tr('chat.workbench_readonly_hint') }}
    </div>

    <!-- 反查归属失败（网关 404 / 401 / 网络断）与"确实没归入项目"必须分开说：
         合并的话，故障会被显示成「未归入任何项目」，而用户可能刚把会话拖进项目 ——
         一句自相矛盾的话，且把真正的原因（接口不可用）藏了起来。 -->
    <div
      v-if="projectError"
      class="wb-error"
    >
      {{ projectError }}
      <button
        type="button"
        class="wb-btn"
        @click="emit('retry-project')"
      >{{ tr('common.retry') }}</button>
    </div>

    <div
      v-else-if="!projectId"
      class="wb-empty"
    >
      {{ tr('chat.workbench_no_project') }}
    </div>

    <template v-else>
      <!-- 页签：文件树与改动列表是同一块区域的两个视图，不该同时铺开 -->
      <div
        class="wb-tabs"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          class="wb-tab"
          :class="{ active: activeTab === 'files' }"
          :aria-selected="activeTab === 'files'"
          @click="selectTab('files')"
        >{{ tr('chat.workbench_tab_files') }}</button>
        <button
          type="button"
          role="tab"
          class="wb-tab"
          :class="{ active: activeTab === 'changes' }"
          :aria-selected="activeTab === 'changes'"
          @click="selectTab('changes')"
        >
          {{ tr('chat.workbench_tab_changes') }}
          <!-- 角标只在真读到 git 数据时出现：没装 git 时显示 "0" 会是假话 -->
          <span
            v-if="changeCount"
            class="wb-tab-count"
          >{{ changeCount }}</span>
        </button>
      </div>

      <div
        v-if="activeTab === 'files'"
        class="wb-tree"
      >
        <div
          v-if="treeError"
          class="wb-error"
        >
          {{ treeError }}
          <button
            type="button"
            class="wb-btn"
            @click="reload"
          >{{ tr('common.retry') }}</button>
        </div>
        <div
          v-else-if="!rows.length && loadingDirs.size"
          class="wb-empty"
        >
          {{ tr('common.loading') }}
        </div>
        <div
          v-else-if="!rows.length"
          class="wb-empty"
        >
          {{ tr('chat.workbench_empty_dir') }}
        </div>
        <div
          v-for="row in rows"
          :key="row.entry.path"
          class="wb-row"
          :class="{ active: selected?.path === row.entry.path }"
          :style="{ paddingInlineStart: `${8 + row.depth * 14}px` }"
          role="treeitem"
          :aria-expanded="row.entry.type === 'dir' ? expanded.has(row.entry.path) : undefined"
          tabindex="0"
          @click="row.entry.type === 'dir' ? toggleDir(row.entry) : selectFile(row.entry)"
          @keydown.enter.prevent="row.entry.type === 'dir' ? toggleDir(row.entry) : selectFile(row.entry)"
          @keydown.space.prevent="row.entry.type === 'dir' ? toggleDir(row.entry) : selectFile(row.entry)"
        >
          <span class="wb-caret">{{ row.entry.type === 'dir' ? (expanded.has(row.entry.path) ? '▾' : '▸') : '' }}</span>
          <span class="wb-name">{{ row.entry.name }}</span>
        </div>
        <div
          v-if="truncatedVisible"
          class="wb-empty"
        >{{ tr('chat.workbench_list_truncated') }}</div>
      </div>

      <!-- 改动页签：只读。没有暂存/丢弃/提交按钮 —— 见脚本里"改动页签"那段的理由 -->
      <div
        v-else
        class="wb-tree"
      >
        <div
          v-if="changesError"
          class="wb-error"
        >
          {{ changesError }}
          <button
            type="button"
            class="wb-btn"
            @click="loadChanges"
          >{{ tr('common.retry') }}</button>
        </div>
        <div
          v-else-if="changesLoading"
          class="wb-empty"
        >{{ tr('common.loading') }}</div>
        <div
          v-else-if="changesData && !changesData.repo"
          class="wb-empty"
        >{{ changesData.git_missing ? tr('chat.workbench_git_missing') : tr('chat.workbench_no_vcs') }}</div>
        <template v-else>
          <div
            v-if="changesData?.head"
            class="wb-changes-head"
          >{{ tr('chat.workbench_changes_head', { head: changesData.head }) }}</div>
          <div
            v-if="!changeRows.length"
            class="wb-empty"
          >{{ tr('chat.workbench_changes_empty') }}</div>
          <div
            v-for="row in changeRows"
            :key="row.path"
            class="wb-row wb-change"
            :class="{ 'agent-only': row.agentOnly }"
            role="treeitem"
            tabindex="0"
            :title="row.agentOnly ? tr('chat.workbench_agent_only') : row.agentTouched ? tr('chat.workbench_agent_touched') : row.path"
            @click="openChange(row)"
            @keydown.enter.prevent="openChange(row)"
            @keydown.space.prevent="openChange(row)"
          >
            <span
              class="wb-status"
              :class="`st-${row.status || 'x'}`"
            >{{ row.agentOnly ? '•' : row.status }}</span>
            <span class="wb-name">{{ row.path }}</span>
            <!-- 缺省 ≠ 0：二进制/重命名/未跟踪文件没有统计，就什么都不打印 -->
            <span
              v-if="row.insertions !== undefined || row.deletions !== undefined"
              class="wb-stat"
            ><span class="ins">+{{ row.insertions ?? 0 }}</span><span class="del">-{{ row.deletions ?? 0 }}</span></span>
            <span
              v-if="row.agentTouched"
              class="wb-agent-dot"
              :title="tr('chat.workbench_agent_touched')"
            >◇</span>
          </div>
          <div
            v-if="changesData?.truncated"
            class="wb-empty"
          >{{ tr('chat.workbench_changes_truncated') }}</div>
        </template>
      </div>

      <div
        v-if="selected && activeTab === 'files'"
        class="wb-preview"
      >
        <div class="wb-preview-head">
          <span
            class="wb-preview-path"
            :title="selected.path"
          >{{ selected.path }}</span>
          <button
            type="button"
            class="wb-btn"
            :title="tr('chat.workbench_copy_path')"
            @click="copyPath(selected)"
          >⧉</button>
          <button
            type="button"
            class="wb-btn"
            :title="tr('chat.workbench_open_system')"
            @click="openWithSystem(selected)"
          >↗</button>
          <button
            type="button"
            class="wb-btn wb-btn-primary"
            :title="tr('chat.workbench_reference_hint')"
            @click="referenceSelected"
          >{{ tr('chat.workbench_reference') }}</button>
        </div>
        <div class="wb-preview-body">
          <div
            v-if="previewLoading"
            class="wb-empty"
          >{{ tr('common.loading') }}</div>
          <div
            v-else-if="previewError"
            class="wb-error"
          >{{ previewError }}</div>
          <template v-else-if="activeObjectURL">
            <img
              v-if="isImagePath(selected.path)"
              class="wb-image"
              :src="activeObjectURL"
              :alt="selected.name"
            >
            <iframe
              v-else
              class="wb-frame"
              :src="activeObjectURL"
              :title="selected.name"
            />
          </template>
          <div
            v-else-if="previewBinary"
            class="wb-empty wb-empty-action"
          >
            <span>{{ tr('chat.workbench_binary_hint') }}</span>
            <!-- 提示语写着"用系统程序打开"，那就必须真的能点：
                 否则用户读到的是一句无法执行的建议。 -->
            <button
              type="button"
              class="wb-btn"
              @click="openWithSystem(selected)"
            >{{ tr('chat.workbench_open_system') }}</button>
          </div>
          <template v-else>
            <!-- 结构必须是 `pre > code[data-lang]`：codeHighlighter 按这个选择器增强，
                 语言标记走 data-lang，未识别时由它退回 highlightAuto。 -->
            <div
              ref="previewHost"
              class="wb-code"
            >
              <!-- `data-lang` 必须**总是渲染**（空串也行）：codeHighlighter 的选择器是
                   `pre code[data-lang]`，用 `|| undefined` 让 Vue 省略该属性时，
                   没有扩展名/未知扩展名的文件（`go.mod`、`Makefile`、`LICENSE`…）
                   会连行号都拿不到 —— 整块代码被当成不存在，静默跳过增强。
                   空串让高亮器退回 highlightAuto，行号照加。 -->
              <pre><code :data-lang="previewLang">{{ previewText }}</code></pre>
            </div>
            <div
              v-if="previewTruncatedLines > 0"
              class="wb-empty wb-empty-action"
            >
              <span>{{ tr('chat.workbench_preview_truncated', { n: previewTruncatedLines }) }}</span>
              <button
                type="button"
                class="wb-btn"
                @click="openWithSystem(selected)"
              >{{ tr('chat.workbench_open_system') }}</button>
            </div>
          </template>
        </div>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.workbench-panel {
  position: relative; /* 分栏手柄的定位锚点 */
  display: flex; flex-direction: column; min-width: 0; height: 100%;
  border-inline-end: var(--hairline-width) solid var(--border-subtle);
  overflow: hidden;
}
/* 交换布局下工作台被移到主会话右侧，与主会话相邻的那条边就换成了左侧。
   不改的话分隔线会出现在窗口外缘，主会话与面板之间反而没有界线。 */
.workbench-panel.handle-left {
  border-inline-end: 0;
  border-inline-start: var(--hairline-width) solid var(--border-subtle);
}
.wb-head {
  flex: none; display: flex; align-items: center; gap: 6px;
  padding: 6px 10px; border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.wb-title {
  flex: 1; min-width: 0; font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wb-toggle { display: inline-flex; align-items: center; gap: 3px; font-size: 11px; color: var(--text-tertiary); cursor: pointer; }
.wb-btn {
  flex: none; border: none; background: transparent; color: var(--text-tertiary);
  cursor: pointer; font-size: 13px; padding: 0 5px; border-radius: var(--radius-sm);
}
.wb-btn:hover { color: var(--text-primary); }
.wb-btn-primary { color: var(--accent); font-size: 12px; }
.wb-btn:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: var(--focus-ring-offset); }
/* 只读横幅：信息性而非警告，所以用中性底色 */
.wb-readonly {
  flex: none; padding: 4px 10px; font-size: 11px; color: var(--text-tertiary);
  background: var(--bg-secondary); border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.wb-tree { flex: 1; min-height: 0; overflow: auto; padding: 4px 0; }
.wb-row {
  display: flex; align-items: center; gap: 4px; padding: 2px 8px;
  font-size: 12px; color: var(--text-secondary); cursor: pointer; white-space: nowrap;
}
.wb-row:hover { background: var(--bg-hover); }
.wb-row.active { background: var(--primary-bg); color: var(--text-primary); }
.wb-row:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: -2px; }
.wb-caret { flex: none; width: 10px; color: var(--text-tertiary); font-size: 10px; }
.wb-name { overflow: hidden; text-overflow: ellipsis; }
.wb-empty { padding: 10px 12px; font-size: 12px; color: var(--text-tertiary); }
/* 带操作的提示（如二进制文件）：文字与按钮并排，按钮不继承提示的灰色文字 */
.wb-empty-action { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.wb-empty-action > span { flex: 1; min-width: 0; }
.wb-error { padding: 10px 12px; font-size: 12px; color: var(--danger); }
.wb-preview { flex: none; max-height: 55%; display: flex; flex-direction: column; border-top: var(--hairline-width) solid var(--border-subtle); }
.wb-preview-head {
  flex: none; display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.wb-preview-path {
  flex: 1; min-width: 0; font-size: 11px; color: var(--text-tertiary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: ltr;
}
.wb-preview-body { flex: 1; min-height: 0; overflow: auto; padding: 6px 10px; }
.wb-text {
  margin: 0; font-family: var(--font-mono, monospace); font-size: 12px; line-height: 1.5;
  color: var(--text-secondary); white-space: pre-wrap; word-break: break-word;
}
/* 代码预览：滚动交给**外层** `.wb-preview-body` 负责。
   这里刻意**不再设 `overflow`** —— 上一版给 `.wb-code` 也加了 `overflow: auto`，
   于是出现两个嵌套滚动容器：内层没有确定宽度就撑不出自己的滚动边界，
   配合 `white-space: pre` 的结果是整段代码被压成一行、横向无处滚动。
   `min-width: max-content` 让内容能自然撑宽，由外层统一滚动。 */
.wb-code {
  min-width: max-content;
}
.wb-code pre {
  margin: 0; font-family: var(--font-mono, monospace); font-size: 12px; line-height: 1.5;
  color: var(--text-secondary); white-space: pre; tab-size: 4;
}
.wb-code code { font-family: inherit; }
/* 行号（codeHighlighter 会把每行包成 .code-line/.code-line-no/.code-line-body）。
   聊天区那份样式带 `white-space: pre-wrap; word-break: break-all` —— 那是为窄消息列
   设计的**折行**排版。工作台直接复用高亮器却拿不到那些 `:deep()` 样式，结果长代码行
   既没行号列也没换行，全挤在一起。这里补一份**横向滚动**版本：行号定宽不换行，
   代码保持原样，由外层 .wb-preview-body 统一滚动。

   必须用 `:deep()`：这些 span 是 codeHighlighter 通过 `innerHTML` 动态插入的，
   不带 Vue 的 `data-v-*` 作用域属性，普通选择器**匹配不到** —— 写上去看起来生效了，
   实际 line 仍是 inline，所有行叠在同一个 top 上（正是"挤成一行"的成因）。 */
.wb-code :deep(.code-line) { display: block; }
.wb-code :deep(.code-line-no) {
  display: inline-block; width: 3em; padding-inline-end: 12px;
  text-align: end; color: var(--text-tertiary); user-select: none;
}
.wb-code :deep(.code-line-body) { white-space: pre; }
.wb-image { max-width: 100%; height: auto; }
.wb-frame { width: 100%; height: 320px; border: none; background: var(--bg-card); }

/* ── 页签 ── */
.wb-tabs {
  flex: none; display: flex; gap: 2px; padding: 0 8px;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.wb-tab {
  border: none; background: transparent; cursor: pointer;
  padding: 5px 8px; font-size: 12px; color: var(--text-tertiary);
  border-bottom: 2px solid transparent; margin-bottom: calc(-1 * var(--hairline-width));
  display: inline-flex; align-items: center; gap: 4px;
}
.wb-tab:hover { color: var(--text-primary); }
.wb-tab.active { color: var(--text-primary); border-bottom-color: var(--primary); }
.wb-tab:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: -2px; }
.wb-tab-count {
  font-size: 10px; padding: 0 4px; border-radius: var(--radius-sm);
  background: var(--bg-secondary); color: var(--text-tertiary);
}

/* ── 改动列表 ── */
.wb-changes-head {
  padding: 4px 10px; font-size: 11px; color: var(--text-tertiary);
  font-family: var(--font-mono, monospace); direction: ltr;
}
.wb-change { gap: 6px; }
/* 状态字母定宽等宽：不定宽的话 M / R / ? 宽度不同，右侧统计列会左右跳 */
.wb-status {
  flex: none; width: 12px; text-align: center;
  font-family: var(--font-mono, monospace); font-size: 11px; color: var(--text-tertiary);
}
/* 状态色只在能区分语义的地方用：新增绿、删除红，其余中立 —— 满屏彩色反而看不出重点 */
.wb-status.st-A, .wb-status.st-\? { color: var(--success); }
.wb-status.st-D { color: var(--danger); }
.wb-status.st-U { color: var(--warning); }
.wb-stat {
  flex: none; display: inline-flex; gap: 4px; font-size: 11px;
  font-family: var(--font-mono, monospace); margin-inline-start: auto;
}
.wb-stat .ins { color: var(--success); }
.wb-stat .del { color: var(--danger); }
.wb-agent-dot { flex: none; color: var(--accent); font-size: 10px; }
/* 只在会话里出现、git 没记录的条目：用斜体而不是换颜色 —— 它仍然是一条改动 */
.wb-change.agent-only .wb-name { font-style: italic; }

/* 分栏手柄：平时不可见，悬停/拖动/聚焦时才显形（与 SessionPreviewPane 一致） */
.wb-resize {
  position: absolute; top: 0; bottom: 0; width: 6px;
  z-index: var(--z-sticky);
  cursor: col-resize; background: transparent;
  transition: background-color var(--dur-fast) var(--ease-out);
  touch-action: none;
}
.wb-resize.handle-left { left: -3px; }
.wb-resize.handle-right { right: -3px; }
.wb-resize:hover,
.workbench-panel.resizing .wb-resize { background: var(--primary-bg); }
.wb-resize:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.workbench-panel.resizing { user-select: none; }
</style>
