/**
 * 项目工作区（N7 v1）：哪些项目"打开着"、各自上次的会话与工作台状态。
 *
 * ## 为什么先做标签而不是双栏
 *
 * 同时渲染两个可写会话需要把 ChatView 拆成可复用 pane（输入框、流、审批、工作台
 * 全都有单实例假设）。先用**项目标签**把"多项目切换不丢上下文"这件事做对：
 * 每个项目记住自己的活跃会话与工作台开合，切换标签就等于恢复现场。
 * 状态是模块级的 —— 离开聊天页再回来不会把标签清空。
 *
 * ## 持久化
 *
 * localStorage，按 userId 分键：换账号不能看到上一个账号打开过哪些项目。
 * 项目 id 是服务端生成的、名字来自服务端，本地只存 id 与"上次会话 id"。
 *
 * ## 失效处理
 *
 * 项目被删/会话被删时，本地记录会指向不存在的东西。`prune()` 在每次挂载时
 * 用服务端项目列表校正；会话失效由调用方在切换时兜底（找不到就退到该项目第一个
 * 会话，再不行就新建）。
 */
import { reactive } from 'vue'

export interface ProjectWorkspaceState {
  /** 打开的项目 id（顺序 = 标签顺序） */
  open: string[]
  /** 当前激活的项目 id；空串 = 没有激活标签 */
  active: string
  /** projectId → 该项目上次停留的会话 id */
  sessions: Record<string, string>
  /** projectId → 工作台是否展开 */
  workbench: Record<string, boolean>
}

const STORAGE_PREFIX = 'chiron.project-workspace.v1'

const state = reactive<ProjectWorkspaceState>({
  open: [],
  active: '',
  sessions: {},
  workbench: {},
})

/** 当前状态对应的存储键（随 userId 切换）。 */
let storageKey = STORAGE_PREFIX

function persist(): void {
  try {
    localStorage.setItem(storageKey, JSON.stringify(state))
  } catch {
    /* 隐私模式/配额满：内存里仍然有效，只是下次启动恢复不了 */
  }
}

function resetInPlace(next: Partial<ProjectWorkspaceState> = {}): void {
  state.open = Array.isArray(next.open) ? next.open.filter(Boolean) : []
  state.active = typeof next.active === 'string' && state.open.includes(next.active) ? next.active : ''
  state.sessions = next.sessions && typeof next.sessions === 'object' ? { ...next.sessions } : {}
  state.workbench = next.workbench && typeof next.workbench === 'object' ? { ...next.workbench } : {}
}

/** 载入某个账号的标签状态。重复调用同一 userId 不会覆盖内存中的最新状态。 */
export function loadProjectWorkspace(userId: string): void {
  const nextKey = userId ? `${STORAGE_PREFIX}:${userId}` : STORAGE_PREFIX
  if (nextKey === storageKey) return
  storageKey = nextKey
  let parsed: Partial<ProjectWorkspaceState> = {}
  try {
    const raw = localStorage.getItem(storageKey)
    parsed = raw ? (JSON.parse(raw) as Partial<ProjectWorkspaceState>) : {}
  } catch {
    parsed = {}
  }
  resetInPlace(parsed)
}

/** 清空当前账号的标签状态（登出/切账号时调用）。 */
export function resetProjectWorkspace(): void {
  resetInPlace()
  persist()
}

/** 用服务端项目列表校正：丢掉已不存在的项目与会话记录。 */
export function pruneProjectWorkspace(knownProjectIds: Iterable<string>): void {
  const known = new Set(knownProjectIds)
  const open = state.open.filter(id => known.has(id))
  const sessions: Record<string, string> = {}
  for (const [pid, sid] of Object.entries(state.sessions)) {
    if (known.has(pid)) sessions[pid] = sid
  }
  const workbench: Record<string, boolean> = {}
  for (const [pid, openState] of Object.entries(state.workbench)) {
    if (known.has(pid)) workbench[pid] = openState
  }
  const active = open.includes(state.active) ? state.active : (open[open.length - 1] || '')
  resetInPlace({ open, active, sessions, workbench })
  persist()
}

export function ensureProjectTab(projectId: string): void {
  if (!projectId) return
  if (!state.open.includes(projectId)) {
    state.open.push(projectId)
    persist()
  }
}

export function activateProjectTab(projectId: string): void {
  if (!projectId) return
  ensureProjectTab(projectId)
  if (state.active !== projectId) {
    state.active = projectId
    persist()
  }
}

/** 关闭标签；返回关闭后应激活的项目 id（空串 = 没有剩余标签）。 */
export function closeProjectTab(projectId: string): string {
  const index = state.open.indexOf(projectId)
  if (index < 0) return state.active
  state.open.splice(index, 1)
  delete state.sessions[projectId]
  delete state.workbench[projectId]
  if (state.active === projectId) {
    // 优先右侧邻居，其次左侧（与浏览器标签一致）
    state.active = state.open[index] || state.open[index - 1] || ''
  }
  persist()
  return state.active
}

export function rememberProjectSession(projectId: string, sessionId: string): void {
  if (!projectId || !sessionId) return
  if (state.sessions[projectId] === sessionId) return
  state.sessions[projectId] = sessionId
  persist()
}

export function lastProjectSession(projectId: string): string {
  return state.sessions[projectId] || ''
}

export function setProjectWorkbench(projectId: string, open: boolean): void {
  if (!projectId) return
  if (state.workbench[projectId] === open) return
  state.workbench[projectId] = open
  persist()
}

export function projectWorkbenchOpen(projectId: string): boolean {
  return state.workbench[projectId] === true
}

/**
 * 该项目是否**明确记录过**工作台开合。
 *
 * 与 `projectWorkbenchOpen` 的区别在 `false` 的二义性：从未打开过与主动关闭过都是
 * false。启动恢复只在"记录过"时应用 —— 否则一个刚打开工作台、还没产生任何记录就
 * 离开聊天页的用户，下次回来会被强制关掉。
 */
export function hasProjectWorkbench(projectId: string): boolean {
  return Object.prototype.hasOwnProperty.call(state.workbench, projectId)
}

/** 组件用：同一份模块级状态。 */
export function useProjectWorkspace(): ProjectWorkspaceState {
  return state
}

/** 仅供测试：把模块状态恢复干净。 */
export function __resetProjectWorkspaceForTests(): void {
  storageKey = STORAGE_PREFIX
  resetInPlace()
}
