/**
 * 会话身份与项目归属（从 `ChatView.vue` 抽出的第一刀）。
 *
 * 为什么先抽这一块：
 *  1. `activeSessionId` / `sessions` 是**唯一身份事实**，几乎所有别的状态都依赖它；
 *  2. "当前会话属于哪个项目"有一段容易回归的逻辑（`root_path` 为空不算项目、
 *     晚到结果必须丢弃、查询失败不能伪装成"没有项目"），它值得独立单测；
 *  3. 这块逻辑与视图无关，抽出来之后 ChatView 不再持有第二份事实源。
 *
 * 仍然是 Pinia setup store：`sessions` / `activeSessionId` 是 `ref`，ChatView 用
 * `storeToRefs` 解构后与原来的 `ref` 读写完全一致（既有 50+ 处引用不用改）。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import type { ChatSession } from '../components/chat/chat-types'
import { describeApiError } from '../utils/apiError'

export interface ActiveProject {
  id: string
  name: string
}

export const useChatSessionStore = defineStore('chatSession', () => {
  /** 会话列表（登录用户来自 DB，guest 来自 localStorage，由调用方填充）。 */
  const sessions = ref<ChatSession[]>([])
  /** 当前会话 id；空串 = 新会话/未选择。 */
  const activeSessionId = ref('')
  /** 当前会话所属项目（`root_path` 非空才算）；null = 没有项目。 */
  const activeProject = ref<ActiveProject | null>(null)
  /**
   * 反查项目**失败**的原因（空串 = 没失败）。
   *
   * 必须与 `activeProject === null` 分开：前者是故障（网关 404/401/网络断），
   * 后者是正常状态（未归入项目）。合并会让"刚拖进项目却显示未归入"变得无法解释。
   */
  const activeProjectError = ref('')

  const activeSession = computed(
    () => sessions.value.find(s => s.id === activeSessionId.value) || null,
  )

  /**
   * 重新解析"当前会话属于哪个项目"。
   *
   * 不能只在 `activeSessionId` 变化时跑：用户在侧边栏把**当前正在聊的会话**拖进
   * 项目（或移出）时，id 没变，watch 不触发 —— 工作台会继续显示"未归入"。
   * 侧边栏改归属时走 `@binding-changed` 显式调一次这个方法。
   */
  async function refreshActiveProject(): Promise<void> {
    const sid = activeSessionId.value
    activeProject.value = null
    activeProjectError.value = ''
    if (!sid) return
    try {
      const { sessionProject } = await import('../views/sessionmap/projectsApi')
      const project = await sessionProject(sid)
      // 期间切走了会话：晚到的结果属于上一个会话，写进去会让 `@` 搜错目录。
      if (activeSessionId.value !== sid) return
      // **`root_path` 为空不算项目**：那是旧形态的「画布」，没有绑定本地文件夹。
      // 后端 `projectRootOf` 对这种 id 回 404 `not_found`（不是 409
      // `project_root_unavailable` —— 那个才是"文件夹被删了"）。判据与 ListProjects 一致。
      activeProject.value =
        project && project.root_path
          ? { id: project.id, name: project.name || project.root_path }
          : null
    } catch (e) {
      if (activeSessionId.value !== sid) return
      // 查询失败**不能**当成"没有项目"：留原文，让面板说明是哪一步坏了。
      activeProjectError.value = describeApiError(e)
    }
  }

  watch(activeSessionId, () => { void refreshActiveProject() })

  return {
    sessions,
    activeSessionId,
    activeProject,
    activeProjectError,
    activeSession,
    refreshActiveProject,
  }
})
