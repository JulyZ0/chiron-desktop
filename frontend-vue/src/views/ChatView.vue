<script setup lang="ts">
import { ref, computed, reactive, shallowRef, inject, onMounted, onUnmounted, nextTick, watch, h } from 'vue'
import { narrowSidebarQuery } from '../composables/layoutBreakpoints'
import { Button, Input, Modal, Checkbox, Alert, Dropdown, Menu, MenuItem, MenuDivider } from 'ant-design-vue'
import { MenuOutlined, CopyOutlined, LinkOutlined, CloseOutlined } from '@ant-design/icons-vue'
import {
  api, createSSEConnection, submitApproval, submitAnswer, steerRun,
  getChatSessionMessages, resolveMediaUrl, listModels,
  createAgent, interruptAgentRun, registerAuthScopedReset, SSE_SILENT_TIMEOUT_MS,
} from '../api'
import type { LlmModel, SSEHandle } from '../api'
import { requestTurnStop, type TurnRef, type TurnStopState } from '../composables/useTurnStop'
import { getSessionMetrics, getSessionRuntime, putSessionRuntime, type SessionRuntime } from '../api/sessionRuntime'
import { getProjectVcs, initProjectVcs } from '../api/projectVcs'
import { listPersonas, type Persona } from '../api/personas'
import FloatingPanel from '../components/common/FloatingPanel.vue'
import MarqueeText from '../components/common/MarqueeText.vue'
import { TOPBAR_SLOTS, type TopbarSlots } from '../components/shell/topbarSlots'
import SubAgentPane from '../components/chat/SubAgentPane.vue'
import {
  openSubagentDock,
  setSubagentDockOpen,
  subagentDockOpen,
  subagentFocusRequest,
  toggleSubagentDock,
} from '../components/chat/useSubAgentDock'
import SessionStatsPanel from '../components/chat/SessionStatsPanel.vue'
import ChatHealthPanel from '../components/chat/ChatHealthPanel.vue'
import { fetchComponentsHealth, isComponentUp, type ComponentsHealth } from '../api/systemHealth'
import ChatReferenceDock from '../components/chat/ChatReferenceDock.vue'
import { useAuthStore } from '../stores/auth'
import { privateKey } from '../utils/privateStorage'
import { playUiSound } from '../utils/uiSounds'
import { planReconnect } from '../utils/streamReattach'
import { notify } from '../utils/notify'
import { consumeQuickAsk, consumeQuickAskText, onQuickAsk, onQuickAskText } from '../utils/quickAsk'
import { useThemeStore } from '../stores/theme'
import { useChatSessionStore } from '../stores/chatSession'
import { useChatRunState } from '../composables/useChatRunState'
import { useChatStreamText } from '../composables/useChatStreamText'
import { createSseDispatcher } from '../composables/useChatSse'
import type { CompactionInfo, VerificationSnapshot } from '../components/chat/sseProjection'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQuery } from 'vue-router'
import ChatSidePanel from '../components/chat/ChatSidePanel.vue'
import type { SubagentEvent } from '../api/subagent'
import MessageList from '../components/chat/MessageList.vue'
import MessageItem from '../components/chat/MessageItem.vue'
import ChatEmptyHero from '../components/chat/ChatEmptyHero.vue'
import ChatInput from '../components/chat/ChatInput.vue'
import WorkbenchPanel from '../components/workbench/WorkbenchPanel.vue'
import { setWorkbenchOpen, toggleWorkbench, workbenchOpen } from '../components/workbench/useWorkbench'
import { collectAgentWrittenPaths, normalizeForCompare, writePathOfToolCall } from '../components/workbench/agentTouched'
import {
  activateProjectTab,
  closeProjectTab,
  ensureProjectTab,
  hasProjectWorkbench,
  lastProjectSession,
  loadProjectWorkspace,
  projectWorkbenchOpen,
  pruneProjectWorkspace,
  rememberProjectSession,
  setProjectWorkbench,
  useProjectWorkspace,
} from '../composables/useProjectWorkspace'
import { listProjects, listProjectSessions } from './sessionmap/projectsApi'
import type { ProjectSummary } from './sessionmap/types'
import SessionMapView from './sessionmap/SessionMapView.vue'
import { pickStreamPreview } from './sessionmap/streamPreview'
import ChatSetupTour from '../components/chat/ChatSetupTour.vue'
import OnboardingGuide from '../components/chat/OnboardingGuide.vue'
import { useNewUserOnboarding, markFirstMessageDone } from '../composables/useNewUserOnboarding'
import { TOUR_ANCHORS, type TourReason } from '../composables/useOnboardingTour'
import { useChatShare } from '../composables/useChatShare'
import { useApprovalEdit, type PendingApproval } from '../composables/useApprovalEdit'
import {
  claimTicket,
  createInteractionScope,
  type ScopedApproval,
  type ScopedQuestion,
} from '../components/chat/interactionScope'
import {
  notifyInteractionsChanged,
  type LiveTicketRef,
} from '../components/chat/pendingReconcile'
import { useTranscriptSearch } from '../composables/useTranscriptSearch'
import { useTodoPanel } from '../composables/useTodoPanel'
import { boardFromHistory } from '../components/chat/todoProjection'
import TodoPanel from '../components/chat/TodoPanel.vue'
import { useSessionMeta } from '../composables/useSessionMeta'
import { usePanelCollapse } from '../composables/usePanelCollapse'
import { useSessionActivity } from '../composables/useSessionActivity'
import { useSubagentStream } from '../composables/useSubagentStream'
import { useComposerBridge } from '../composables/useComposerBridge'
import { useSessionSwitch, projectHistory, HISTORY_PAGE_SIZE } from '../composables/useSessionSwitch'
import { useMessageFailure } from '../composables/useMessageFailure'
import { useKeyboardShortcuts } from '../composables/useKeyboardShortcuts'
import { SHORTCUT_CATALOG, type ShortcutId } from '../config/shortcutCatalog'
import SaveToKnowledgeDialog from '../components/chat/SaveToKnowledgeDialog.vue'
import SaveToMemoryDialog from '../components/chat/SaveToMemoryDialog.vue'
import ShortcutsHelpModal from '../components/chat/ShortcutsHelpModal.vue'
import { CloudUploadOutlined } from '@ant-design/icons-vue'
import { sessionToMarkdown } from '../utils/sessionMarkdown'
import { sessionToAgent, agentBindingsFromContext } from '../utils/sessionAgent'
import ChatStatusBar from '../components/chat/ChatStatusBar.vue'
import ChatDisplaySettings from '../components/chat/ChatDisplaySettings.vue'
import AskCard from '../components/chat/AskCard.vue'
import ChatAttentionTray from '../components/chat/ChatAttentionTray.vue'
import InteractionInbox from '../components/chat/InteractionInbox.vue'
import InteractionBadge from '../components/chat/InteractionBadge.vue'
import CallChainTimeline from '../components/CallChainTimeline.vue'
import { HistoryOutlined, ExportOutlined, BulbOutlined, BulbFilled, MoreOutlined, FontSizeOutlined, SearchOutlined, PartitionOutlined, RobotOutlined, DatabaseOutlined, SwapOutlined, SplitCellsOutlined, FolderOpenOutlined } from '@ant-design/icons-vue'
import { effortLabelOf, normalizeEffort } from '../components/chat/effort'
import { splitThinking, stripUserInputTag, formatClock, formatSize, countItemsAfter, mergeTurnStats } from '../components/chat/chat-types'
import { normalizeMeta } from '../components/chat/chat-history'
import { describeApiError } from '../utils/apiError'
import { getUsageBudget } from '../api/usage'
import { evaluateBudget } from '../utils/budgetWarning'
import { buildWorkbenchContext, chipsFromWorkbenchContext, clampKbWeight, CONTEXT_QUERY_KEYS, DEFAULT_KB_WEIGHT, parseContextQuery, type ContextChip } from '../components/chat/contextChips'
import type { ChatItem, ChatSession, ChatAttachment, TurnStatsItem, TextItem } from '../components/chat/chat-types'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const route = useRoute()
const router = useRouter()

/**
 * 标题栏的两个落点（由 AppLayout provide）。
 *
 * 为空 = 没有布局层（单测直接挂载本组件）：下面的 `<Teleport :disabled>` 会让
 * 控件原位渲染，测试与独立使用都不受影响。
 */
const topbarSlots = inject<TopbarSlots | null>(TOPBAR_SLOTS, null)

// 低频操作（导出/沉淀/显示设置/主题）收进标题栏右端的溢出菜单：
// 那条只有 36px 高，常驻项只留会话导航与两个视图开关。
// 菜单项在渲染时求值，故与 themeStore 的初始化顺序无关。
const toolbarMenuItems = computed(() => [
  { key: 'export', label: t('common.export_as_markdown'), icon: () => h(ExportOutlined), disabled: !items.value.length },
  { key: 'save_kb', label: t('knowledge.save_to_knowledge_base'), icon: () => h(CloudUploadOutlined), disabled: !hasSessionContent.value },
  { key: 'save_memory', label: t('common.remember_this'), icon: () => h(DatabaseOutlined), disabled: !hasSessionContent.value },
  { key: 'save_agent', label: t('agent.save_as_agent'), icon: () => h(RobotOutlined), disabled: !hasSessionContent.value || savingAgent.value },
  { type: 'divider' as const },
  { key: 'display', label: t('settings.display_settings'), icon: () => h(FontSizeOutlined) },
  // 交换布局：侧边栏与分屏左右对调。注意这里引用的 layoutSwapped 定义在本文件靠后处，
  // 但本数组是 computed（getter 惰性求值），执行时它已初始化 —— 与 splitTitle 同理。
  { key: 'swap_layout', label: layoutSwapped.value ? t('common.layout_sidebar_on_right') : t('common.layout_sidebar_on_left'), icon: () => h(SwapOutlined) },
  { key: 'theme', label: themeStore.isDark ? t('common.switch_to_light_mode') : t('common.switch_to_dark_mode'), icon: () => h(themeStore.isDark ? BulbFilled : BulbOutlined) },
])

const displaySettingsOpen = ref(false)
/** 存入知识库：把会话正文沉淀成知识库文档（弹窗里选目标知识库） */
const saveToKbOpen = ref(false)
/** 记住这条：把会话正文沉淀成长期记忆条目（后续对话会自动注入） */
const saveToMemoryOpen = ref(false)
/**
 * 是否存在可沉淀的会话正文。
 *
 * 与 `sessionToMarkdown(...) !== ''` 完全等价（见 utils/sessionMarkdown.ts：只有标题为空
 * 且没有任何非空 text 项时才返回空串），但**不构造字符串** —— 流式输出时 items 每收到一个
 * chunk 就会变，若用 computed 往返序列化整个会话，长对话下每个 token 都要做一次全量拼接，
 * 还会连锁重算 toolbarMenuItems。这里只做短路判定。
 */
const hasSessionContent = computed(
  () =>
    (activeSession.value?.title || '').trim() !== '' ||
    items.value.some(i => i.kind === 'text' && ((i as TextItem).content || '').trim() !== ''),
)
/**
 * 沉淀弹窗的正文：**按需生成** —— 只在对应弹窗打开时序列化。
 * 弹窗打开后 items 基本不再变化，因此这等价于在原位算一次，而不是随流式输出反复算。
 */
const sessionMarkdownForDialog = computed(() =>
  saveToKbOpen.value || saveToMemoryOpen.value
    ? sessionToMarkdown(items.value, activeSession.value?.title || '')
    : '',
)
/**
 * 存为 Agent：把会话正文沉淀成 Agent 的人格（system_prompt）。
 *
 * 名字必须由用户确认 —— `sessionToAgent` 只给一个基于会话标题的默认值，
 * 直接落库会积出一堆叫"对话记录"的条目。
 */
const saveAgentOpen = ref(false)
const savingAgent = ref(false)
const agentDraft = ref({ name: '', description: '', system_prompt: '' })

function openSaveAsAgent() {
  const draft = sessionToAgent(items.value, activeSession.value?.title || '')
  if (!draft) {
    notify.warning(t('chat.this_session_has_no_text_worth_saving'))
    return
  }
  agentDraft.value = draft
  saveAgentOpen.value = true
}

async function saveAsAgent() {
  const draft = agentDraft.value
  const name = draft.name.trim()
  if (!name) {
    notify.warning(t('agent.please_enter_agent_name'))
    return
  }
  savingAgent.value = true
  try {
    // 连带把本对话挂的知识库/技能/插件写进新 Agent（见 utils/sessionAgent.ts 的
    // agentBindingsFromContext）：只沉淀人格会让用户存完还得回各工作台重新装配一遍。
    const bindings = agentBindingsFromContext(buildContext())
    await createAgent({
      name,
      description: draft.description,
      system_prompt: draft.system_prompt,
      enabled: true,
      ...bindings,
    })
    // 明确说出继承了哪些 —— 绑定是"沉默生效"的，不告诉用户就成幽灵行为
    const inherited = [
      bindings.kb_id ? t('knowledge.knowledge_base') : '',
      bindings.skills?.length ? t('agent.n_skills', { n: bindings.skills.length }) : '',
      bindings.plugins?.length ? t('common.n_plugins', { n: bindings.plugins.length }) : '',
    ].filter(Boolean)
    notify.success(
      inherited.length
        ? t('agent.created_agent_name_inheriting_this_conversation_s_list_adjust_on_the_agents_page', {
            name,
            list: inherited.join('、'),
          })
        : t('agent.created_agent_name_adjust_it_on_the_agents_page', { name }),
    )
    saveAgentOpen.value = false
  } catch (e) {
    notify.error(t('errors.failed_to_create_agent_error', { error: describeApiError(e) }))
  } finally {
    savingAgent.value = false
  }
}

/**
 * 布局方向：把右侧的侧边栏与左侧的分屏**左右对调**。
 *
 * 为什么是"本机记忆"而不是服务端偏好：它描述的是**这块屏幕上的阅读习惯**
 * （宽屏上有人习惯把会话列表放在左手边），不随账号走。与字号、主题同一类，
 * 所以落 localStorage，且**读取失败一律回退默认**（隐私模式不该因此崩）。
 */
const LAYOUT_SWAP_KEY = 'chiron.chat.swapLayout'
const layoutSwapped = ref(readLayoutSwap())

function readLayoutSwap(): boolean {
  try {
    return localStorage.getItem(LAYOUT_SWAP_KEY) === '1'
  } catch {
    return false
  }
}

function toggleLayout() {
  layoutSwapped.value = !layoutSwapped.value
  try {
    localStorage.setItem(LAYOUT_SWAP_KEY, layoutSwapped.value ? '1' : '0')
  } catch {
    /* 存不下也不影响本次会话内的切换 */
  }
}

function onToolbarMenu(info: { key: string | number }) {
  if (info.key === 'export') exportMarkdown()
  else if (info.key === 'save_kb') saveToKbOpen.value = true
  else if (info.key === 'save_memory') saveToMemoryOpen.value = true
  else if (info.key === 'save_agent') openSaveAsAgent()
  else if (info.key === 'display') displaySettingsOpen.value = true
  else if (info.key === 'theme') themeStore.toggleTheme()
  else if (info.key === 'swap_layout') toggleLayout()
}

// ── 会话内检索（正文与思考；工具输出不进结果）────────────────────────────
// 实现见 `composables/useTranscriptSearch.ts`。`items` 与 `trajectoryFocus` 在本文件里
// 都声明得更晚，所以两个依赖必须是**惰性**的（getter 与回调），不能传值。
const {
  searchOpen,
  searchQuery,
  searchCursor,
  searchInputRef,
  searchMatches,
  openSearch,
  closeSearch,
  gotoMatch,
} = useTranscriptSearch({
  items: () => items.value,
  focus: (index) => {
    trajectoryFocus.value = index
    trajectoryToken.value++
  },
})

// 状态栏：最近一轮用量（turn_stats 由后端在回合结束时下发）
const lastTurnStats = computed<TurnStatsItem | null>(() => {
  for (let i = items.value.length - 1; i >= 0; i--) {
    const item = items.value[i]
    if (item?.kind === 'turn_stats') return item
  }
  return null
})

// 最近一次自动压缩（问题 4：压缩早已实现，但此前前端完全不可感知 —— 用户只会
// 觉得"上下文好像丢了/回答变短了"）。引擎现在会发 compaction 事件，这里接住并展示。
const lastCompaction = ref<CompactionInfo | null>(null)

/**
 * 观测浮层开关。统计与组件健康仍是浮层（看完即关）；
 * 子 Agent 已改为**布局内的对照栏**（`SubAgentPane` + `useSubAgentDock`），
 * 它跟会话一起长期开着，不再属于"浮出-关闭"这一类。
 */
const statsOpen = ref(false)

/**
 * 分屏 6a：参考栏绑定的会话 id（空 = 不显示）。
 *
 * 先做**只读参考栏** —— 真正"两栏都可写"要把 `activeSessionId` / `items` / 流式连接
 * 从单值改成**按 pane 归属**（ZCode 为此有 `paneLayoutStore` + `SessionPane` 一整套），
 * 那是一次状态归属重构。而"边跑子 Agent 边看另一个会话"这个高频需求只读就够，
 * 且**完全不触碰现有状态模型**（零回归风险）。可写双栏属 6b 之后。
 */
const SPLIT_SESSION_KEY = 'chiron:chat-split-session:v1'

function readSplitSession(): string {
  try {
    return globalThis.localStorage?.getItem(SPLIT_SESSION_KEY) || ''
  } catch {
    return ''
  }
}

const splitSessionId = ref(readSplitSession())
watch(splitSessionId, (id) => {
  try {
    if (id) globalThis.localStorage?.setItem(SPLIT_SESSION_KEY, id)
    else globalThis.localStorage?.removeItem(SPLIT_SESSION_KEY)
  } catch {
    /* 隐私模式：只在本次运行内生效 */
  }
})

// 右侧对照 Dock：参考会话与子 Agent 共用一栏，同时打开时用 tab 切换。
const RIGHT_DOCK_TAB_KEY = 'chiron:chat-right-dock:v1'
function readRightDockTab(): 'reference' | 'subagent' {
  try {
    return globalThis.localStorage?.getItem(RIGHT_DOCK_TAB_KEY) === 'subagent' ? 'subagent' : 'reference'
  } catch {
    return 'reference'
  }
}
const rightDockTab = ref<'reference' | 'subagent'>(readRightDockTab())
watch(rightDockTab, (tab) => {
  try {
    globalThis.localStorage?.setItem(RIGHT_DOCK_TAB_KEY, tab)
  } catch {
    /* 存不下不影响使用 */
  }
})
watch(splitSessionId, (id) => { if (id) rightDockTab.value = 'reference' })
watch(subagentDockOpen, (open) => { if (open) rightDockTab.value = 'subagent' })

/** 工具栏按钮：开/关参考栏 */
function toggleSplit() {
  if (splitSessionId.value) {
    splitSessionId.value = ''
    return
  }
  // 候选 = 会话列表里第一个**不是当前会话**的会话。
  // 这里刻意**不用 watch / 不在模块级读 activeSessionId**：`activeSessionId` 与 `sessions`
  // 在本文件里声明得比本函数更晚，而函数体只在**点击那一刻**求值 ——
  // 于是既能拿到最新值，又不会触发 TDZ（"used before its declaration"）。
  const prev = sessions.value.find(s => s.id !== activeSessionId.value)
  if (!prev) {
    notify.info(t('chat.no_other_session_available_as_reference'))
    return
  }
  splitSessionId.value = prev.id
}

/** 参考栏标题（computed 的 getter 同样惰性求值，渲染时 sessions 已就绪） */
const splitTitle = computed(() => {
  const s = sessions.value.find(x => x.id === splitSessionId.value)
  return s?.title || splitSessionId.value.slice(0, 8)
})

/**
 * 底部预留高度（输入区 + 状态栏）—— 传给浮层当限高基准，
 * 使浮层"向上长高到输入区顶部即止"，从而**不遮挡正在写的草稿**。
 * 取近似常量而非实时测量：多留一些比少留安全，且避免 ResizeObserver 的复杂度。
 */
const floatingBottomInset = 160

// 模式不再有"来源"后缀。它不再是会话/服务端状态，也没有回落到谁的默认值一说 ——
// 界面上的模式就是用户当前的选择（全局单一状态），随本次提交下发。
// 顶部标题栏也不再挂只读的模式胶囊：模式的唯一入口是输入区的「对话设置」。

// 上下文占用环的分母：模型上限来自 /v1/models 的 context_window（拿不到就不显示比例）
const availableModels = ref<LlmModel[]>([])
const contextWindow = computed(() => {
  const name = llmModel.value
  if (!name) return null
  return availableModels.value.find(m => m.name === name)?.context_window || null
})
listModels()
  .then(models => {
    availableModels.value = models
    // 模型默认兜底：列表就绪时若还没有选中任何模型，**自动选第一个可用模型**。
    // 否则 `llmModel` 会一直是空 —— 提交时不携带 model，落到**后端默认模型**，
    // 而默认模型一旦被上游下架就会报 "Upstream request failed: Model is unavailable."。
    // 注意顺序：这必须发生在列表到达之后（切换会话时的第三级回落依赖它）。
    if (!llmModel.value && models.length) {
      llmModel.value = models[0].name
    }
  })
  .catch(() => { availableModels.value = [] })

// ── 会话状态 ──
// 身份与项目归属的唯一事实源在 `stores/chatSession.ts`（含切换时的项目反查、晚到结果丢弃、
// "root_path 为空不算项目"）。`storeToRefs` 解构后，下面 50+ 处 `sessions.value` /
// `activeSessionId.value` 的读写与原来的 ref 完全一致。
const chatSessionStore = useChatSessionStore()
const {
  sessions,
  activeSessionId,
  activeProject,
  activeProjectError,
  activeSession,
} = storeToRefs(chatSessionStore)
const refreshActiveProject = chatSessionStore.refreshActiveProject

// 输入区的装配胶水（引用投递 / 附件通道 / `@` 提及 / 失败暂存队列）
// 实现见 `composables/useComposerBridge.ts`。`chatInputRef` 仍是模板 `ref=` 绑定的那个
// 名字；会话 id、账号、激活芯片、发送路径都晚于这里声明，故一律以惰性 getter 传入。
const {
  chatInputRef,
  onQuoteText,
  onWorkbenchReference,
  onMentionAdd,
  applyChatPrefill,
  outbox,
  outboxPreview,
  retrieveOutbox,
  retryOutbox,
  discardOutbox,
  queueFailedSend,
} = useComposerBridge({
  sessionId: () => activeSessionId.value,
  userId: () => authStore.user?.id,
  contextChips: () => contextChips.value,
  setContextChips: (chips) => { contextChips.value = chips },
  persistRuntime: (patch) => persistRuntime(patch),
  sendMessage: (text, attachments) => sendMessage(text, attachments),
})


// 项目归属的解析（含失败原因、晚到结果丢弃）已抽到 `stores/chatSession.ts`。
// 这里只保留 UI 直接消费的派生 id。
const activeProjectId = computed(() => activeProject.value?.id || '')

// ── N7 v1：项目标签 ────────────────────────────────────────────────────────
// 每个项目记住自己的活跃会话与工作台开合；切换标签 = 恢复现场。
// 状态在 composable 里是模块级的，离开聊天页再回来不丢。
const projectWorkspace = useProjectWorkspace()
const allProjects = ref<ProjectSummary[]>([])
const projectNames = ref<Record<string, string>>({})
/**
 * 待恢复工作台状态的项目 id。
 *
 * 不能在 `switchProjectTab` 里"切完会话就立刻设 workbenchOpen"：`activeProject` 是
 * 异步反查的（`switchSession` 返回时它还指着旧项目），那时赋值会把**旧项目**的开合
 * 状态写回去。等 activeProject 真的落到目标项目，再由 watcher 恢复。
 */
let pendingWorkbenchRestore = ''
/**
 * 启动后是否已经按项目恢复过一次工作台开合。
 *
 * 只做一次：activeProject 会随会话切换反复变化，每次都恢复等于把用户刚关掉的工作台
 * 又打开。`hasProjectWorkbench` 用来区分"从未打开过"与"主动关闭过"。
 */
let initialWorkbenchRestored = false
const projectTabsRef = ref<HTMLElement | null>(null)

/**
 * 当前会话名：统一任务模式显示固定文案，否则用会话标题（新会话还没标题时退回品牌名）。
 * 它会**合并进当前项目标签**（`项目名 · 对话名`），所以不再单独占一个标题元素。
 */
const convTitle = computed(() =>
  unifiedMode.value ? t('workflow.unified_task') : (activeSession.value?.title || 'Chiron'),
)

/** 标签展示用：本地还没拿到项目名时退回 id，绝不显示空白标签。
 *  激活标签额外带上当前会话名（项目 → 对话 的上下文）；其它标签保持项目名，
 *  宽度上限不变，超长部分交给 MarqueeText 滚动播放。 */
const openProjectTabs = computed(() =>
  projectWorkspace.open.map((id) => {
    const name = projectNames.value[id] || id
    const merged = projectWorkspace.active === id && convTitle.value
      ? `${name} · ${convTitle.value}`
      : name
    return { id, name, label: merged }
  }),
)
/** "+" 里只列**还没打开**的项目。 */
const projectPickerOptions = computed(() =>
  allProjects.value.filter(p => !projectWorkspace.open.includes(p.id)),
)

async function refreshProjectCatalogue(): Promise<boolean> {
  try {
    const list = await listProjects()
    allProjects.value = list
    projectNames.value = Object.fromEntries(
      list.map(p => [p.id, p.name || p.root_path || p.id]),
    )
    return true
  } catch {
    /* 项目列表是标签的增强数据：拉不到就沿用本地已知的名字 */
    return false
  }
}

/** 打开/切换到一个项目标签。 */
async function switchProjectTab(projectId: string): Promise<void> {
  if (!projectId) return
  activateProjectTab(projectId)
  const remembered = lastProjectSession(projectId)
  // 工作台开合按项目恢复（默认关）；等 activeProject 落定后再应用，避免写到旧项目上。
  pendingWorkbenchRestore = projectId
  if (remembered && sessions.value.some(s => s.id === remembered)) {
    await switchSession(remembered)
  } else {
    // 本地没记过/记的会话已删：退到服务端列表里的第一个，再不行就新建（归属会写进后端）。
    let target = ''
    try {
      const list = await listProjectSessions(projectId)
      target = list[0]?.id || ''
    } catch {
      target = ''
    }
    if (target) await switchSession(target)
    else await createSession(projectId)
  }
}

/** 关闭标签；关的是当前标签时按浏览器习惯切到右邻/左邻。 */
function closeProjectTabAndMaybeSwitch(projectId: string): void {
  const wasActive = projectWorkspace.active === projectId
  const next = closeProjectTab(projectId)
  if (wasActive && next) void switchProjectTab(next)
}

function onProjectPickerOpenChange(open: boolean): void {
  if (open) void refreshProjectCatalogue()
}

function onProjectPickerClick(info: { key: string | number }): void {
  const id = String(info?.key || '')
  if (!id || id === '__empty') return
  void switchProjectTab(id)
}

/**
 * 标签键盘导航（N7 尾巴）：左右方向键在**打开的项目**之间循环切换，并把焦点带到
 * 新标签上。循环而不是在两端停住 —— 标签是环形列表，停在两端会让"再按一下"变成
 * 无反馈的空操作。
 */
async function focusAdjacentProjectTab(delta: number): Promise<void> {
  const ids = projectWorkspace.open
  if (ids.length < 2) return
  const current = Math.max(0, ids.indexOf(projectWorkspace.active))
  const next = ids[(current + delta + ids.length) % ids.length]
  if (next === projectWorkspace.active) return
  await switchProjectTab(next)
  await nextTick()
  const el = projectTabsRef.value?.querySelector<HTMLElement>(
    `[data-project-id="${next}"]`,
  )
  el?.focus()
  el?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}

// 当前会话属于哪个项目：自动化地保证该项目的标签存在并激活，同时记住会话。
watch(
  () => [activeProject.value?.id || '', activeSessionId.value] as const,
  ([projectId, sessionId]) => {
    if (!projectId) return
    ensureProjectTab(projectId)
    activateProjectTab(projectId)
    if (sessionId) rememberProjectSession(projectId, sessionId)
    if (pendingWorkbenchRestore === projectId) {
      pendingWorkbenchRestore = ''
      workbenchOpen.value = projectWorkbenchOpen(projectId)
    } else if (!initialWorkbenchRestored) {
      // 启动恢复：只在项目**记录过**工作台状态时应用（见 hasProjectWorkbench 的注释）。
      initialWorkbenchRestored = true
      if (hasProjectWorkbench(projectId)) {
        workbenchOpen.value = projectWorkbenchOpen(projectId)
      }
    }
  },
)

// 工作台开合记到当前项目上；切换项目时由 switchProjectTab 恢复。
watch(workbenchOpen, open => {
  if (activeProjectId.value) setProjectWorkbench(activeProjectId.value, open)
})

// ── N2：审阅 → 修复 ────────────────────────────────────────────────────────
// 工作台只负责"选行 + 写意见"，拼会话消息与发送在这里（它才拿得到 sendMessage）。
const workbenchRef = ref<InstanceType<typeof WorkbenchPanel> | null>(null)
/** 发过修复请求后等本回合结束，再把当前 diff 重拉一次。 */
let pendingDiffRefresh = false

function onWorkbenchFixRequest(payload: {
  items: Array<{ path: string; comment: string; lines: string[] }>
}) {
  const items = (payload?.items || []).filter(item => String(item.comment || '').trim())
  if (!items.length) return
  // 多条意见拼成**一条**消息：会话里保持"一次审阅 = 一条输入"，Agent 也拿到完整上下文。
  const prompt = items
    .map(item => t('chat.workbench_review_prompt', {
      path: item.path,
      comment: String(item.comment).trim(),
      // 围栏里出现 ``` 会把提示词的代码块提前闭合；替换掉比"拼一个更长的围栏"简单可靠。
      diff: item.lines.join('\n').replaceAll('```', '`` `'),
    }))
    .join('\n\n')
  pendingDiffRefresh = true
  void sendMessage(prompt)
}

/**
 * 任务清单面板（长任务防跑偏）。
 *
 * **刻意不放进 `items`**：`items` 是消息流投影，切会话/重放历史/`/clear` 都会整体
 * 重建它；清单是跨轮次存活的全局状态，两者生命周期不同。混在一起的后果是任一侧的
 * 重建都会破坏另一侧 —— 具体表现为"流式输出时面板被重建而闪断"，或"切回历史会话
 * 看到上一轮任务残留的面板，而模型那边早已没有这份清单"。
 * 所以清单按会话单独存（`useTodoPanel`），与消息流互不触碰。
 */
const todoPanel = useTodoPanel()
const todoBoard = computed(() => todoPanel.boardOf(activeSessionId.value))

/** 失败暂存队列（按会话隔离、横幅预览、取回编辑）：见 `composables/useComposerBridge.ts`，
 *  已在上方解构。它只与输入框交互，不参与消息流投影。 */
// 多会话运行状态（runtimes / items / loading / withRun / streamState）见
// `composables/useChatRunState.ts`。对外仍是 `items` / `loading`：既有 70+ 处读写不用改。
const {
  runtimes,
  runOf,
  currentRun,
  liveRunSessionIds,
  writingRun,
  withRun,
  loading,
  items,
  streamState,
} = useChatRunState(() => activeSessionId.value)

// N2：修复回合结束（loading true→false）时刷新 diff。失败/取消也算结束：
// 用户至少能看到"改了什么/没改什么"，而不是一个过期的 diff。
// 放在 `loading` 解构之后 —— 组合式函数的返回值不是提升的绑定。
watch(loading, (now, before) => {
  if (before && !now && pendingDiffRefresh) {
    pendingDiffRefresh = false
    void workbenchRef.value?.refreshDiff?.()
  }
})

/**
 * 本次会话里 agent 写过的文件路径 —— 工作台「改动」页签的第二数据源。
 *
 * 为什么由 ChatView 算而不是工作台自己去读会话：工作台只拿到 projectId，没有
 * 时间线；而时间线本来就是这一层的状态。摘取逻辑本身在 `agentTouched.ts` 里做成
 * 纯函数，那里写明了两条数据源为什么要分开。
 */
const agentWrittenPaths = computed(() => collectAgentWrittenPaths(items.value))

/**
 * 本回合 agent 写过的文件（F4）。与 `agentWrittenPaths`（整场会话）分开：
 * 审阅"这一轮改了什么"必须是回合级；整场列表会把上一轮的改动也混进来。
 * 发送新消息时重置；tool_call 帧到达时追加（去重按归一化路径）。
 */
const turnWrittenPaths = ref<string[]>([])
function noteTurnWrite(d: Record<string, unknown>): void {
  const path = writePathOfToolCall(String(d.name ?? ''), d.arguments)
  if (!path) return
  const key = normalizeForCompare(path)
  if (!key) return
  if (turnWrittenPaths.value.some(p => normalizeForCompare(p) === key)) return
  turnWrittenPaths.value = [...turnWrittenPaths.value, path]
}
/**
 * ── SSE 连接**按会话**持有（H59）──
 *
 * 此前是一个模块级 `activeSSE`：多会话并行时它只指得着"最后建的那一条"。两个后果都真实发生过：
 * ① 任何一条流收尾都会去关它 —— 可能关掉的是**另一个会话**仍在跑的流（那个会话就此静默：
 *    不再出字、`loading` 永远挂着）；② 切会话把指针置空，切回正在跑的会话反而停不了它
 *    （旧注释写着"宁可停不了，也不能停错"，那正是这个全局变量的代价）。
 *
 * 现在每条流只属于建它的会话：收尾 / 出错 / 停止都只关**自己那条**（"只有来源流可关闭自身"）。
 */
const sseBySession = reactive(new Map<string, SSEHandle>())
// 每个会话最后收到的 SSE 事件 id（服务端 id: 行 → event.lastEventId）。
// 跨轮重建 SSE 时回传（last_event_id），服务端从缓冲流补发上一轮断线缺口（见 api/index.ts createSSEConnection）
const sseLastIdBySession = new Map<string, string>()
// H68: auth switch must clear the SSE resume cursor; the map lives here,
// so the reset is registered as a hook and released on unmount.
let unregisterAuthScopedReset: (() => void) | undefined
let unsubQuickAsk: (() => void) | undefined
/** 迷你输入框提交（F10 后半）：与本页已挂载时直接发送的通道。 */
let unsubQuickAskText: (() => void) | undefined

/** 该会话是否有一条本页的会话流（子 Agent 通道据此决定同一帧交给谁） */
function hasSessionStream(sid: string): boolean {
  return !!sid && sseBySession.has(sid)
}

/** 关掉**某个会话**的流（幂等；绝不碰别的会话的流） */
/**
 * 给会话挂上 SSE（H60：首轮与断线重连共用同一条装配逻辑）。
 *
 * 为什么要单独一个函数：原来的装配只写在 `sendMessage` 里，于是"断线时有回合在跑"
 * 这种情况根本没法重新挂流 —— 重连后界面停在旧内容，用户以为模型卡住了。
 *
 * `sinceEventId` 会作为 `Last-Event-ID` 交给服务端：它按这个 id 从缓冲流补发缺口，
 * 而不是把整轮重放一遍（重放会造成重复内容与重复计费）。
 */
function attachSessionStream(
  sessionId: string,
  options: { sinceEventId?: string; userItemId?: string } = {},
): void {
  if (!sessionId) return
  closeSessionStream(sessionId)
  const handle = createSSEConnection(
    sessionId,
    (raw, meta) => withRun(runOf(sessionId), () => onSSEMessage(raw, sessionId, meta?.id)),
    () => {
      // 只改**来源会话**的运行态。loading / 计时器是当前视图的代理，A 的流失败
      // 绝不能把正在看的 B 的 spinner 停掉（反之会让 A 永远显示"生成中"）。
      runOf(sessionId).loading = false
      if (activeSessionId.value === sessionId) {
        loading.value = false
        stopTurnTimer()
      }
      connectionLost.value = true
      closeSessionStream(sessionId)
      if (options.userItemId) markMessageFailed(options.userItemId, t('common.connection_lost'))
      playUiSound('disconnected')
    },
    {
      initialLastEventId:
        options.sinceEventId || sseLastIdBySession.get(sessionId) || '',
      autoReconnect: true,
      heartbeatTimeoutMs: SSE_SILENT_TIMEOUT_MS,
      onLastEventId: (id) => sseLastIdBySession.set(sessionId, id),
    },
  )
  sseBySession.set(sessionId, handle)
}

function closeSessionStream(sid: string): void {
  const handle = sid ? sseBySession.get(sid) : undefined
  if (!handle) return
  sseBySession.delete(sid)
  handle.close()
}

/** 关掉全部（卸载 / 认证切换 / 退出统一任务模式） */
function closeAllSessionStreams(): void {
  for (const sid of [...sseBySession.keys()]) closeSessionStream(sid)
}

// ── 失败消息标记与手动修复（实现见 composables/useMessageFailure.ts）──
const { markMessageFailed, describeFailureMeta, repairSession } = useMessageFailure({
  items,
  activeSessionId,
})

// ── Trace ID (当前会话的链路追踪标识) ──
const currentTraceId = ref('')  // SSE done 事件回传的 trace_id

// 安全修复：待确认工具调用（三态栅栏"确认"态）；类型定义在 useApprovalEdit.ts
//
// H59：票据**按会话建图**（见 `components/chat/interactionScope.ts` 文件头）。两个全局 ref
// 会有四种后果：会话 A 的票画在 B 的输入区上方、在 B 里点"允许"却批准到 B、切会话把 A
// 仍在等的票一起清空、后台会话的票静默超时。归属在**到达时**冻结，提交只认票自己的归属。
const interactionScope = createInteractionScope()

/** 当前视图会话的待审批票据（模板 / 倒计时读它；别的会话的票留在自己的桶里） */
const pendingApprovals = computed<readonly ScopedApproval[]>(
  () => interactionScope.approvalsOf(activeSessionId.value),
)
/** 当前视图会话的待答提问 */
const pendingQuestions = computed<readonly ScopedQuestion[]>(
  () => interactionScope.questionsOf(activeSessionId.value),
)
/**
 * 实时票据 → 历史列表（收件箱 / 角标）的对账输入（H61）。
 *
 * 同一张票既在服务端 `pending_interactions` 里、又在实时通道里。两个数据源各有盲区
 * （实时只覆盖当前会话，历史是拉取时的快照），所以**按票对齐**而不是二选一：
 * 收件箱据此不再给同一张票出第二个答复入口，角标取并集计数。见 `pendingReconcile.ts`。
 */
const liveInteractionTickets = computed<LiveTicketRef[]>(() => [
  ...interactionScope.approvalsOf(activeSessionId.value)
    .map(t => ({ id: t.id, kind: 'approval' as const, toolCallId: t.id })),
  ...interactionScope.questionsOf(activeSessionId.value)
    .map(t => ({ id: t.id, kind: 'question' as const, toolCallId: t.id })),
])
/** 全部会话的实时票据 id（角标按并集计数） */
const livePendingIds = computed<string[]>(() => [...interactionScope.liveIds()])

// 审批卡片出现时给一声提示：它会阻塞整个执行链，而用户很可能已经切到别的窗口了。
// 判据用**全部会话**的票数：后台会话的审批同样在等人，只盯当前会话会让它静默超时。
// 只在"新增"时响（长度变大），过期清理导致的减少不该出声。
watch(() => interactionScope.approvalCount(), (n, prev) => {
  if (n > prev) playUiSound('tool-pending')
})
// 审批卡片倒计时：approval 事件到达时记录截止时间（后端 _await_approval 默认 300s，
// 超时按"拒绝"处理），前端展示剩余时间并在过期后移除卡片。
const approvalTick = ref(0)
const APPROVAL_TIMEOUT_MS = 300_000
let approvalTimer: ReturnType<typeof setInterval> | null = null

function approvalRemain(a: PendingApproval): number {
  void approvalTick.value // 依赖 tick 触发重算
  if (!a?.expiresAt) return 0
  return Math.max(0, Math.ceil((a.expiresAt - Date.now()) / 1000))
}

function ensureApprovalTimer() {
  if (approvalTimer) return
  approvalTimer = setInterval(() => {
    approvalTick.value++
    // 过期清理是**跨会话**的：后台会话的票同样在超时（后端 300s 后按拒绝处理），
    // 只清"当前视图"那份会让它们在切回来时仍然显示为可批。
    const remaining = interactionScope.sweepApprovals(Date.now())
    if (remaining === 0 && approvalTimer) {
      clearInterval(approvalTimer)
      approvalTimer = null
    }
  }, 1000)
}

/**
 * 提交审批决定。`editedArguments`（C5）非空 ⇒ "**编辑后批准**"：
 * 回传 `decision: 'edit'` 与编辑后的完整参数，由引擎重跑工具分级
 * （级别升高则不执行）—— 前端**不做**本地放行判断，那会是第二份分级表。
 *
 * H59 + H61 的三条：
 * 1. `session_id` 取**票自己的**归属（到达时冻结），不取 `activeSessionId`；
 * 2. 归属不符 / 属上一次运行时 / 已在提交中 ⇒ 一律**不提交**，票原地保留（`claimTicket`）；
 * 3. **服务端确认前不摘票**：失败把原因写回卡片并允许重试（旧实现 `catch {}` 吞掉后照样
 *    删卡片 —— 用户以为批过了，而引擎一直等到 300s 超时按拒绝处理）。
 */
async function resolveApproval(
  a: PendingApproval,
  approved: boolean,
  editedArguments?: string,
): Promise<boolean> {
  const ticket = a as ScopedApproval
  const ownerSession = String(ticket.sessionId || '')
  const claim = claimTicket(ticket, {
    sessionId: activeSessionId.value,
    gen: ownerSession ? runOf(ownerSession).gen : 0,
  })
  if (!claim.ok) {
    // 拒绝错配提交：这张票不属于当前视图（用户已经切走），或已在等上一次回执。
    // 留一条 warn 便于排查"点了没反应" —— 但它**不该**产生一次真实的放行。
    console.warn('[chat] approval submit rejected', {
      reason: claim.reason,
      id: ticket.id,
      owner: ownerSession,
      view: activeSessionId.value,
    })
    return false
  }
  interactionScope.setBusy('approval', ticket.id, true)
  let acknowledged = false
  try {
    acknowledged = await submitApproval({
      session_id: ownerSession,
      tool_call_id: ticket.id,
      approved: editedArguments ? true : approved,
      decision: editedArguments ? 'edit' : approved ? 'approve' : 'reject',
      ...(editedArguments ? { arguments: editedArguments } : {}),
    })
  } catch (e) {
    interactionScope.markFailed('approval', ticket.id, describeApiError(e))
    notify.error(t('errors.approval_submission_failed_please_retry'))
    return false
  }
  if (!acknowledged) {
    // 网关答了，但引擎说"这个决定不生效"（票已超时 / 该调用已不在等待）。
    // 这不是成功：票留着、原因写在卡上 —— 与 `catch` 同一条纪律（不做假成功）。
    interactionScope.markFailed(
      'approval',
      ticket.id,
      t('errors.approval_not_effective_may_have_timed_out'),
    )
    return false
  }
  // 只有服务端确认（无异常且 ok）才摘票
  interactionScope.settle('approval', ticket.id)
  clearApprovalEdit(ticket.id)
  settleInteractionViews()
  return true
}

/**
 * 一张票落定后的统一收尾（H61）：让**其它观察者**跟上，而不是各自等轮询。
 *
 * 收件箱是历史列表的读视图、角标是跨会话计数 —— 它们都不会自己知道"刚刚答完一张"。
 * 不通知的话就是那个老问题："我答过了，列表里还在、角标还数着。"
 */
function settleInteractionViews() {
  notifyInteractionsChanged()
  void interactionInboxRef.value?.refresh()
}

// ── C5：审批卡片的"编辑参数"折叠区 ────────────────────────────────────────
// 实现见 `composables/useApprovalEdit.ts`。这里只把它接到既有审批通道上：
// 编辑态由 composable 管，"编辑后批准"仍走 `resolveApproval`，不产生第二份放行判断。
const {
  approvalEdit,
  toggleApprovalEdit,
  onApprovalEditInput,
  clearApprovalEdit,
  submitApprovalEdit,
} = useApprovalEdit({ resolveApproval })

// ── 结构化提问（ask_user 工具）────────────────────────────────────────────
// 与审批卡的分工：审批回传布尔（允许/拒绝），提问回传**答案文本**（选项值或自由输入），
// 因此走独立的 /v1/agent/answer 通道而不是复用 approval。
// 票据形状与归属见 `components/chat/interactionScope.ts`（`ScopedQuestion`）：H59 起
// 提问与审批共用同一个按会话建图的仓库，`pendingQuestions` 只是"当前视图会话"的投影。

// 任务预算快照（引擎 `done` 事件带 `budget={轴: {used, limit}}`，`limit=0` 表示该轴不限）。
const budgetSnapshot = ref<Record<string, { used?: number; limit?: number }> | null>(null)
// 会话累计成本（分）。计价在**网关**侧（`/v1/sessions/{id}/metrics`），引擎只产 token。
const sessionCostCents = ref<number | null>(null)
// 上下文快照（引擎 `done` 带 `context={used, limit, compact_ratio}`）。阈值也由引擎给出，
// 前端不写死常量 —— 它可被租户/模式覆盖。
const contextSnapshot = ref<{ used?: number; limit?: number; compact_ratio?: number; breakdown?: Record<string, number>; largest?: Array<{ class?: string; role?: string; tokens?: number }>; estimated?: boolean } | null>(null)
// 前缀缓存诊断（引擎 `done` 带 `cache={prefix_changed: ["system"|"tools"]}`）。空数组 = 逐字未变。
const cacheSnapshot = ref<{ prefix_changed?: string[]; first_turn?: boolean } | null>(null)
/** 本轮验证结论（N6）：工作台 diff 徽标与消息流提示共用。 */
const verificationSnapshot = ref<VerificationSnapshot | null>(null)

// 提问的等待上限，与后端 `runtime.ASK_ANSWER_TTL_SECONDS = 300` 对齐。
// 超时后后端把 `{"error": "user did not answer in time"}` 当**工具结果**回给模型，
// 并不再发 fe 事件，所以前端必须自己判定过期 —— 否则卡片一直可点，用户以为还能答，
// 点了才会失败。与审批卡（`APPROVAL_TIMEOUT_MS` + `ensureApprovalTimer`）同一套模式。
const ASK_TIMEOUT_MS = 300_000
let askTimer: ReturnType<typeof setInterval> | null = null

function stopAskTimer() {
  if (askTimer) {
    clearInterval(askTimer)
    askTimer = null
  }
}

function ensureAskTimer() {
  if (askTimer) return
  askTimer = setInterval(() => {
    // 跨会话判过期：后台会话的提问同样在等超时，只看当前视图那份会让它们永远显示成可答。
    if (!interactionScope.sweepQuestions(Date.now())) stopAskTimer()
  }, 1000)
}

/**
 * 答复一张提问票（H59 + H61）。
 *
 * 与审批同一条纪律：归属只认票自己的 `sessionId`、错配拒绝、**确认前不摘票**。
 * 旧实现在提交**之前**就从列表里删掉，失败只弹一句 toast —— 卡片没了、票还在服务端，
 * 用户除了刷新页面没有任何补救入口。
 */
async function submitQuestionTicket(
  question: ScopedQuestion,
  // 只允许这两个字段：`answer` 是必填的答复正文（拒绝时传哨兵由调用方决定），
  // `declined` 区分"明确拒绝"与"超时/没看见"。用 Record<string, unknown> 会让
  // 类型检查放不过 `answer` 缺失的调用（H61 之后这个方法确实会真的少传）。
  payload: { answer: string; declined?: boolean },
): Promise<boolean> {
  const ownerSession = String(question.sessionId || '')
  const claim = claimTicket(question, {
    sessionId: activeSessionId.value,
    gen: ownerSession ? runOf(ownerSession).gen : 0,
  })
  if (!claim.ok) {
    console.warn('[chat] question submit rejected', {
      reason: claim.reason,
      id: question.id,
      owner: ownerSession,
      view: activeSessionId.value,
    })
    return false
  }
  interactionScope.setBusy('question', question.id, true)
  let acknowledged = false
  try {
    acknowledged = await submitAnswer({
      session_id: ownerSession,
      tool_call_id: question.id,
      ...payload,
    })
  } catch (e) {
    interactionScope.markFailed('question', question.id, describeApiError(e))
    notify.error(t('errors.answer_submission_failed_please_retry'))
    return false
  }
  if (!acknowledged) {
    // 网关答了但引擎不收（票已超时/已被取走）：票留着、原因写在旁边，用户可再答一次
    interactionScope.markFailed(
      'question',
      question.id,
      t('errors.answer_not_delivered_may_have_timed_out'),
    )
    return false
  }
  interactionScope.settle('question', question.id)
  if (interactionScope.questionCount() === 0) stopAskTimer()
  settleInteractionViews()
  return true
}

async function answerQuestion(question: ScopedQuestion, answer: string) {
  await submitQuestionTicket(question, { answer })
}

/** 成本常显：计价在网关侧，引擎只管 token，所以要拉会话级 metrics。 */
async function refreshSessionCost() {
  const sid = activeSessionId.value
  if (!sid) {
    sessionCostCents.value = null
    return
  }
  try {
    const m = await getSessionMetrics(sid)
    sessionCostCents.value = Number(m?.totals?.cost_cents ?? 0)
  } catch {
    // 拉不到就不显示 —— 这是展示信息，不该打断对话
    sessionCostCents.value = null
  }
  void checkBudgetWarning()
}

// ── F12：日费用预算提醒（80% / 100% 各一次，不自动中断） ──
let budgetCheckedAt = 0

function budgetWarnKey(scope: string, level: number): string {
  const day = new Date().toISOString().slice(0, 10)
  return `chiron:budget-warn:${day}:${scope}:${level}`
}

async function checkBudgetWarning(): Promise<void> {
  // 每 5 分钟最多查一次：refreshSessionCost 在每段 `done` 都会触发。
  if (Date.now() - budgetCheckedAt < 5 * 60 * 1000) return
  budgetCheckedAt = Date.now()
  let b
  try {
    b = await getUsageBudget()
  } catch {
    return // 读不到就不提醒；这是辅助信息
  }
  for (const w of evaluateBudget(b)) {
    const key = budgetWarnKey(w.scope, w.level)
    try {
      if (localStorage.getItem(key)) continue
      localStorage.setItem(key, '1')
    } catch {
      // 存不下就每次都提示：宁可重复，也不要静默
    }
    const scopeLabel = t(w.scope === 'user' ? 'billing.budget_scope_user' : 'billing.budget_scope_tenant')
    notify.warning(
      t(w.level >= 100 ? 'billing.budget_reached' : 'billing.budget_near', {
        scope: scopeLabel,
        pct: w.pct,
        used: (w.usedCents / 100).toFixed(2),
        limit: (w.limitCents / 100).toFixed(2),
      }),
    )
  }
}

/** 明确拒绝回答：走同一条通道，但带 `declined` 标志 —— 模型收到的是"用户拒绝"，不是超时错误。 */
async function declineQuestion(question: ScopedQuestion) {
  await submitQuestionTicket(question, { answer: '', declined: true })
}

// ── 人工介入待办收件箱（M7c）──────────────────────────────────────────────
// 实时审批/提问卡片只在 SSE 还开着时出现；`awaiting_input` 收尾、刷新页面或那一轮进程
// 已经没了之后，只剩 pending_interactions 记着"欠用户一个答复"。收件箱补的就是这一段。
const interactionInboxRef = ref<InstanceType<typeof InteractionInbox> | null>(null)
// 同一批待办里连点多个"允许"只唤醒一次：唤醒是一条真实提交，重复发会在历史里堆"继续"。
let wakingFromInbox = false

/**
 * 答复落库后顺手提交一轮，让引擎从暂停处接手并消费这张票。
 *
 * 只在会话空闲（`!loading`）时唤醒；正在跑说明现场已被别的提交接管（票会在下一次续跑时被
 * 消费），此时再发一条只是往历史里塞噪音。唤醒内容本身不被引擎解读 —— 续跑靠 (kind,tool,args)
 * 指纹匹配 grant，不看这条消息说了什么，所以它只需要是句人话。
 *
 * 但"被挡下"**不能就这样丢掉**：一次答复一批待办时，第一条答复的唤醒会让 `loading` 变 true，
 * 其余几条的唤醒就全被这一行挡回。它们停在 `answered`，而续跑只消费与本次调用指纹匹配的票，
 * 于是永久留在列表里。所以挡下时记账（`pendingInboxWake`），本轮收尾时补一次
 * （见 `flushPendingInboxWake`）。
 */
async function wakeFromInbox() {
  if (wakingFromInbox) return
  if (loading.value) {
    currentRun.value.pendingInboxWake = true
    return
  }
  wakingFromInbox = true
  try {
    await sendMessage(t('agent.interaction_continue_message'))
  } finally {
    wakingFromInbox = false
    // 续跑会把这些票从 answered 翻成 consumed（或留作下一次），列表要跟着更新
    void interactionInboxRef.value?.refresh()
  }
}

/** 本轮收尾后补发被挡下的那次唤醒（`done` / 出错 / 停止都要走，否则记账就永远挂着）。
 *  只补一次：再被挡下说明又有一轮在跑，那轮的收尾会接着补。 */
function flushPendingInboxWake() {
  const run = currentRun.value
  if (!run.pendingInboxWake) return
  // 收尾时**仍然**在跑（同一会话刚被又提交了一轮）：记账不能清 —— 清了就再也没人补，
  // 那几张票又会永久停在 answered。留给那一轮的收尾继续补。
  if (loading.value) return
  run.pendingInboxWake = false
  void wakeFromInbox()
}

/** 放弃补发：用户喊停、或这一轮以错误/拦截收尾。
 *
 *  这两种情况**不**自动补唤醒 —— 用户喊停是明确说"别再跑了"；而错误（provider 401、
 *  余额不足…）自动重发大概率撞同一个错，只会把一次失败变成反复失败。票留在 `answered`：
 *  用户看得到"还欠一次继续"，下一次手动发送就会消费它，主动权在他手上。 */
function clearPendingInboxWake() {
  currentRun.value.pendingInboxWake = false
}
// 与下方「对话模式」(mode: normal/minimal) 是两个不同维度：
// 本项控制**工具执行是否需要用户确认**。
//
// 它是**前端的实时状态**（全局单一，切换会话不改变它）：既不写 localStorage，也不存
// 服务端 —— 每次提交经 `llm_config.tools_mode` 随请求下发，引擎在任务开始时采用一次
// （判定在 python-engine/app/agent/guards.py）。
// 所以这里既没有"加载"也没有"保存"：改了就立刻是本次生效值，不存在保存失败。
const toolsMode = ref<'ask' | 'auto' | 'yolo'>('auto')
const toolsModeOptions = [
  { label: t('common.ask'), value: 'ask' },
  { label: t('common.auto'), value: 'auto' },
  { label: t('common.fully_automatic'), value: 'yolo' },
]

function onToolsModeChange(v: unknown) {
  const m = String(v) as 'ask' | 'auto' | 'yolo'
  if (m === toolsMode.value) return
  toolsMode.value = m
  notify.success(
    m === 'yolo'
      ? t('agent.switched_to_fully_automatic_tool_confirmation_skipped_this_leaves_an_audit_trail')
      : t('agent.tool_auth_mode_set_to_mode', { mode: toolsModeOptions.find(o => o.value === m)?.label || m }),
  )
}

// ── 输出控制（差距B）：输出风格 / 语言偏好 ──
//
// 与 toolsMode 同一处置：**请求级实时状态**，经 llm_config 随每条消息下发（服务端不存储，
// 避免"前端选、服务端存储、runtime"三份状态漂移）。取值集合须与
// `python-engine/app/agent/output_controls.py` 的枚举逐字一致。
const outputStyle = ref<string>('default')
const language = ref<string>('auto')
// D2：思考语言 —— 与回复语言分开设置（请求级实时状态，同样随 llm_config 下发）。
const reasoningLanguage = ref<string>('auto')

function onOutputStyleChange(v: unknown) {
  outputStyle.value = String(v) || 'default'
}
function onLanguageChange(v: unknown) {
  language.value = String(v) || 'auto'
}
function onReasoningLanguageChange(v: unknown) {
  reasoningLanguage.value = String(v) || 'auto'
}

// 模式
const modeOptions = [
  { label: t('common.normal'), value: 'normal' },
  { label: t('common.minimal_2'), value: 'minimal' },
  { label: t('common.code_mode'), value: 'code' },
]
/** 默认模式：会话没记（或记了个不认识的值）时回落到它 —— **绝不能沿用上一个会话的模式** */
const DEFAULT_MODE = 'normal'
const mode = ref(DEFAULT_MODE)

// ── 模型路由：会话 llm_config.model（空 = 后端默认路由） ──
const llmModel = ref('')

// ── 对话模式预设：mode → temperature + 用户可见的**一句话定位** ──
// desc 必须与后端 app/agent/modes.py 的模式定义表一一对应（工具集/上下文/压缩差异），
// 否则又会回到"切了看不出区别"的老问题（2026-09 实测暴露）。
//
// **这里刻意不含 max_tokens**：输出上限是**按模型**的属性，由引擎查
// `llm_models.max_output_tokens` 决定（见 app/agent/runtime.py 的 _resolve_max_tokens）。
// 前端一旦下发这个值，引擎会按 min(请求值, 模型上限) 夹取，等于用对话框里的一个
// 常量盖掉"换模型就换上限" —— 那正是本次要修的问题。
const MODE_PRESETS = computed<Record<string, { temperature: number; note?: string; desc: string }>>(() => ({
  normal: { temperature: 0.6, desc: t('agent.general_assistant_12_core_tools_injects_memory_skill_knowledge_base_context') },
  minimal: { temperature: 0.2, note: t('chat.brief_reply'), desc: t('common.minimal_only_read_file_edit_file_shell_exec_no_context_injection_no_compression') },
  code: { temperature: 0.1, desc: t('common.code_mode_desc') },
}))

/** 构建 llm_config：mode + 对应预设 temperature/max_tokens + 模型路由 model（base 已显式携带的字段优先保留） */
/**
 * 思考档位：`disabled / low / high / max`（与后端 `app/providers/effort.py` 的归一化表对齐）。
 * 空串 = 不携带 —— 是否真正发送由后端归一化决定（各家词表不一致，见该文件注释）。
 *
 * 控件在「对话设置」弹层里（ChatInput）；档位顺序/标签与响应归一化由
 * `components/chat/effort.ts` 统一提供，避免两处各写一份后漂移。
 */
const effort = ref('')

/** 思考档位切换：与模型/模式同一套持久化（走 buildLlmConfig → 会话 llm_config） */
function onEffortChange(v: string) {
  effort.value = normalizeEffort(v)
  persistRuntime({})
  notify.info(
    effort.value ? t('chat.reasoning_level_level', { level: effortLabelOf(t, effort.value) }) : t('auth.reasoning_level_reset_to_default'),
  )
}

/** 构建 llm_config：mode + 对应预设 temperature/max_tokens + 模型路由 model + 思考档位 */
/**
 * 请求用 llm_config：把**本次生效的请求级参数**（对话模式 / 工具授权模式 / 模型 / 思考档位）
 * 一并带给后端。
 *
 * 两个模式项都是前端实时状态 —— 只有在这里随请求下发后端才拿得到（它们不再有服务端存储，
 * 见 internal/api/mode.go 与 python-engine/app/agent/guards.py）。
 * **落库请用 buildPersistLlmConfig**：模式不属于会话状态。
 */
function buildLlmConfig(base?: Record<string, unknown>): Record<string, unknown> {
  const cfg: Record<string, unknown> = { mode: mode.value, ...(base || {}) }
  if (effort.value) cfg.effort = effort.value
  // 模型路由：会话选定模型写入 llm_config（空 = 不携带，走后端默认路由）
  if (llmModel.value) cfg.model = llmModel.value
  // 工具授权模式（ask/auto/yolo）：随请求下发，引擎侧 ToolGuard 据此裁决
  cfg.tools_mode = toolsMode.value
  // 差距B：输出控制（风格 / 语言偏好）—— 同样是请求级实时状态，引擎侧会把它拼进 system 段。
  // 默认态（default / auto）不携带 ⇒ 引擎不注入任何片段（system 段逐字不变、前缀缓存不受影响）。
  if (outputStyle.value && outputStyle.value !== 'default') cfg.output_style = outputStyle.value
  if (language.value && language.value !== 'auto') cfg.language = language.value
  // D2：思考语言（同样默认态不携带 ⇒ 引擎不注入任何片段）。
  if (reasoningLanguage.value && reasoningLanguage.value !== 'auto') cfg.reasoning_language = reasoningLanguage.value
  // temperature 仍按模式给（它是"说话风格"，与模型无关）。
  // **max_tokens 不在这里设**：输出上限由引擎按模型查 `llm_models.max_output_tokens`。
  const preset = MODE_PRESETS.value[mode.value]
  if (preset && cfg.temperature === undefined) cfg.temperature = preset.temperature
  return cfg
}

/**
 * 落库用 llm_config：剔除请求级参数（对话模式 / 工具授权模式）。
 *
 * 这两项是"改了就生效、刷新即回默认"的实时状态；写进会话 llm_config 等于又造出一份
 * 服务端状态 —— 刷新后旧值复活、与其他来源打架，正是本次要移除的问题。
 * 模型等"用户希望下次继续沿用"的项照常落库。
 */
function buildPersistLlmConfig(base?: Record<string, unknown>): Record<string, unknown> {
  const cfg = buildLlmConfig(base)
  delete cfg.mode
  delete cfg.tools_mode
  return cfg
}

/**
 * 发送前确保有可用模型 —— **返回 false 就不要发**。
 *
 * 为什么必须在**发送前**做：`llm_config.model` 为空时后端会走"默认路由"，
 * 而默认路由的 provider 一旦密钥无效 / 模型下架，用户看到的就是
 * `provider opencode-go stream failed: AuthenticationError: 401 Invalid API key`
 * 这种既看不懂、也不知道该去哪修的报错（产地：`python-engine/app/gateway/router.py:244`）。
 *
 * 列表就绪时本文件（`:347-357`）已经会自动选第一个模型，但**列表是异步的** ——
 * 用户在它返回之前就发消息时 `llmModel` 仍为空，这正是线上那条 401 的入口。
 * 与 `switchSession` 的第四级回落同一条纪律：**绝不把空值当成"可以用后端默认"来提交**。
 */
function ensureModel(): boolean {
  if (!llmModel.value) llmModel.value = availableModels.value[0]?.name || ''
  if (llmModel.value) return true
  offerSetupTour('no-model', t('chat.no_available_model_configure_a_model_provider_and_api_key_in_settings_first'))
  return false
}

/** 未配置引导的浮层（antd `Tour` 的封装，见 `ChatSetupTour.vue`）；`offer` 返回是否真的弹了 */
const tourRef = ref<InstanceType<typeof ChatSetupTour> | null>(null)

/**
 * 新用户引导（P0-3）的三步清单。与上面那个 `Tour` **职责不同**：
 * 那个是"被拦下时带路"，这个是"注册后首次进入"；触发条件不同，不合并。
 *
 * 解构成顶层 ref 才能在模板里自动解包（`onboarding.open` 这种嵌套引用不会）。
 */
const {
  open: onboardingOpen,
  status: onboardingStatus,
  maybeOpen: maybeOpenOnboarding,
  skipAll: skipOnboarding,
  close: closeOnboarding,
} = useNewUserOnboarding()

/**
 * 弹引导；**弹不出来才退化成 toast** —— 保证"无论如何用户都至少得到一句提示"。
 *
 * 为什么要有兜底：引导依赖 DOM 锚点与已挂载的组件，toast 不依赖任何东西。
 * 两者**互斥**（不叠加）：同一件事出现两个浮层比只有一个更烦。
 */
/**
 * 弹引导；**弹不出来才退化成 toast** —— 保证"无论如何用户都至少得到一句提示"。
 *
 * 为什么要有兜底：引导依赖 DOM 锚点与已挂载的组件，toast 不依赖任何东西。
 * 两者**互斥**（不叠加）：同一件事出现两个浮层比只有一个更烦。
 *
 * `tone` 保留原来的严重级别：认证失败过去是 `error`，不该因为改走引导就悄悄降成 `warning`。
 */
function offerSetupTour(reason: TourReason, fallback: string, tone: 'warning' | 'error' = 'warning'): void {
  // `offer?.(…)`：真实组件一定 `defineExpose({ offer })`，但测试里它会被打桩成空组件
  // ⇒ 这里对"没有 offer 的替身"保持安全，落回 toast（那正是兜底要覆盖的情形）。
  const shown = tourRef.value?.offer?.(reason, false)
  if (shown) return
  if (tone === 'error') notify.error(fallback)
  else notify.warning(fallback)
}

/** 引导最后一步的"去配置"：只有管理员会收到（`/models` 是 `requiresAdmin`，见 router/index.ts:46） */
function goConfigureModel(): void {
  router.push('/models')
}

/**
 * 引导里点「去选模型 / 去发消息」后的落点（P0-3）。
 *
 * 不新开页面：这三步本来就该在当前会话里完成。用语义锚点（`data-tour`）找元素，
 * 与 `TOUR_ANCHORS` 共用一套 —— 蹭样式重构不会把它找断。
 */
function focusOnboardingTarget(target: 'model' | 'input'): void {
  const anchor = target === 'model' ? TOUR_ANCHORS.modelPicker : TOUR_ANCHORS.input
  const el = document.querySelector<HTMLElement>(`[data-tour="${anchor}"]`)
  if (!el) return
  el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  // 输入框要能直接打字；下拉按钮聚焦也会给出可见的焦点环
  el.focus?.()
}

/** provider 层认证失败的特征（与 `actionableStreamError` 共用一处判据，别写两遍） */
function isProviderAuthError(raw: string): boolean {
  return /AuthenticationError|Invalid API key|\b401\b/i.test(raw)
}

/**
 * 把 provider 层的原始报错**加一句可行动的提示**，但**保留原文**。
 *
 * 原文是定位问题的唯一线索（本仓一贯做法，见 `describeApiError` 的注释），所以是
 * "加一句"而不是"换掉"：只给 `provider opencode-go stream failed: AuthenticationError: 401`
 * 这种英文原文，用户既不知道是谁的问题、也不知道该改哪里。
 */
function actionableStreamError(raw: string): string {
  if (isProviderAuthError(raw)) {
    return `${t('chat.model_provider_authentication_failed_check_the_provider_and_api_key_in_settings')}（${raw}）`
  }
  if (/Model is unavailable|\b404\b/i.test(raw)) {
    return `${t('chat.the_selected_model_is_unavailable_pick_another_model')}（${raw}）`
  }
  return raw
}

/**
 * 把**会话级**运行时状态写进单一事实源（Redis 热 + `unified_sessions.runtime` 持久）。
 *
 * 只写 `/v1/sessions/{id}/runtime`：`PUT /v1/conversations/{id}` 只接受
 * `title/pinned/tag/alias`（见 internal/api/conversation.go 的 `Update`），塞进去的
 * `llm_config` 会被忽略 —— 原先那行**只带** `llm_config`，必然 400 且被 `.catch` 静默吞掉。
 */
function persistRuntime(patch: Record<string, unknown>) {
  const sid = activeSessionId.value
  if (!sid) return
  void putSessionRuntime(sid, patch).catch(() => {})
}

/**
 * `@` 提及的落地（去重 / 知识库上限 / 写 `runtime.context`）：见
 * `composables/useComposerBridge.ts` 的 `onMentionAdd`，已在上方解构。
 */

/** 模型切换：更新 llmModel ref + 写入运行时状态（会话级持久） */
function onModelChange(m: string) {
  if (m === llmModel.value) return
  llmModel.value = m
  notify.info(m ? t('agent.model_switched_model_affects_subsequent_messages_only', { model: m }) : t('auth.model_reset_to_default_backend_routing'))
  persistRuntime({ model: m || null })
}

/**
 * 模式切换：只改前端状态（全局单一，不写会话、不写服务端）。
 *
 * 模式是请求级参数，随下一条消息经 llm_config.mode 下发，"仅影响后续消息"因此是天然
 * 语义；刷新页面则回落默认 normal（不缓存）。
 */
function onModeChange(m: string) {
  if (m === mode.value) return
  mode.value = m
  const opt = modeOptions.find(o => o.value === m)
  const preset = MODE_PRESETS.value[m]
  notify.info(
    preset?.desc
      ? t('chat.switched_to_mode_mode_desc_affects_subsequent_messages_only', { mode: opt?.label || m, desc: preset.desc })
      : t('chat.switched_to_mode_mode_affects_subsequent_messages_only', { mode: opt?.label || m }),
  )
}

/** 归一化后的 metadata（可能为 JSON 字符串或对象） */

// ── 互联互通：统一任务模式 + 上下文芯片（与 SSE 流式并列的新路径） ──
// 路由 query 约定（由 WorkstationNav / 各工作台入口发起）：
//   ?task=<sessionId>          统一会话（拉历史 + 继续追问）
//   ?task=&error=xxx           仅错误提示
//   ?kb=<id> / ?agent=<id> / ?skill=<name>   上下文附加
//   同名参数可重复（?kb=a&kb=b）=> 同类可多选
//   ?mode=<auto|agent> 创建时模式
const contextChips = ref<ContextChip[]>([])
// 人格选择：只存 id（提示词由网关在提交时按租户解析并下发）。
// localStorage 记忆的是"用户偏好的默认选择"，换会话也沿用；空 = 跟随后台默认人格。
const CHAT_PERSONA_KEY = 'chiron.chat.persona'
const personas = ref<Persona[]>([])
const personaId = ref<string>((() => {
  try {
    return (localStorage.getItem(CHAT_PERSONA_KEY) || '').trim()
  } catch {
    return ''
  }
})())
const personaOptions = computed(() =>
  personas.value.map(p => ({ label: p.name, value: p.id })),
)
async function loadPersonas() {
  try {
    personas.value = await listPersonas()
    // 选过的人格被删除/停用后静默回到默认，避免发送一个无效 id
    if (personaId.value && !personas.value.some(p => p.id === personaId.value)) {
      personaId.value = ''
      localStorage.removeItem(CHAT_PERSONA_KEY)
    }
  } catch {
    personas.value = []
  }
}
function onPersonaChange(id: string) {
  personaId.value = (id || '').trim()
  try {
    if (personaId.value) localStorage.setItem(CHAT_PERSONA_KEY, personaId.value)
    else localStorage.removeItem(CHAT_PERSONA_KEY)
  } catch {
    /* 隐私模式：仅本次会话生效 */
  }
}
const errorBanner = ref('')          // query.error 提示
const unifiedSessionId = ref('')     // 统一任务会话 id（query.task）
const unifiedSubmitMode = ref('auto') // 会话创建时的 mode（shared_context.mode 优先）
// P1-e：**统一任务模式（TaskRouter 自动编排）已并入常规对话链路**。
// 常规链路（SSE /submit → AgentTask）才是模式/模型/工具集/上下文全部生效的那条；
// 保留两条会让"切换模式/模型"看起来只对其中一条起作用（见 internal/api/session_runtime.go）。
// 恒为 false 即停用分流，unifiedSessionId 等相关代码保留以便回退。
const unifiedMode = computed(() => false)
// 纯展示 flag：任务提交成功时，徽标短暂过渡到"完成"态后复位
const unifiedJustFinished = ref(false)
let unifiedDoneTimer: ReturnType<typeof setTimeout> | null = null
function flashUnifiedDone() {
  unifiedJustFinished.value = true
  if (unifiedDoneTimer) clearTimeout(unifiedDoneTimer)
  unifiedDoneTimer = setTimeout(() => { unifiedJustFinished.value = false }, 1600)
}
let appliedQueryKey = ''

async function applyRouteQuery() {
  const q = route.query
  const key = JSON.stringify(q)
  if (key === appliedQueryKey) return
  appliedQueryKey = key

  const task = typeof q.task === 'string' && q.task.trim() ? q.task.trim() : ''
  // task 变更 / 退出统一模式时重置消息区（避免污染普通 SSE 会话）
  if (task !== unifiedSessionId.value) {
    unifiedSessionId.value = task
    items.value = []
    activeSessionId.value = ''
    currentTraceId.value = ''
    loading.value = false
    stopTurnTimer()
    closeAllSessionStreams()
    if (task) await loadUnifiedSession(task)
  }
  errorBanner.value = typeof q.error === 'string' && q.error ? q.error : ''
  // 跨台活动直达：?session=<id> 时切到那个会话（"最近活动"里点对话记录会带它过来）。
  // 统一任务模式（task）下不切，避免与 loadUnifiedSession 抢消息区。
  const target = typeof q.session === 'string' ? q.session.trim() : ''
  if (target && !task && target !== activeSessionId.value) {
    try {
      await switchSession(target)
    } catch {
      // 会话已删 / 非本人：留在当前会话，不打扰
    }
  }
  await initContextChips(q)
  void applyChatPrefill()
}

async function initContextChips(q: Record<string, unknown>) {
  // URL 约定与多值解析统一在 contextChips 模块里（同名参数可重复 => 可多选）
  const chips = parseContextQuery(q)
  // URL 只表达"选了哪些库"，不表达权重（见 setKbWeight）。重新解析 URL 时把已有的
  // 权重带到新 chip 上，否则任何一次 query 变化都会把用户调好的权重抹回 1.0。
  const knownWeights = new Map(
    contextChips.value
      .filter(c => c.type === 'kb' && c.weight !== undefined)
      .map(c => [c.value, c.weight as number]),
  )
  for (const chip of chips) {
    if (chip.type === 'kb' && knownWeights.has(chip.value)) chip.weight = knownWeights.get(chip.value)
  }
  const kb = chips.find(c => c.type === 'kb')?.value || ''
  const agent = chips.find(c => c.type === 'agent')?.value || ''
  const skill = chips.find(c => c.type === 'skill')?.value || ''
  contextChips.value = chips
  // ── 尽力补全展示用的名称（失败则保留 id 占位；Agent 配置本身由网关按 id 补全）──
  if (kb) {
    try {
      const res = await api.get(`/v1/kb/${encodeURIComponent(kb)}`)
      const d = res.data?.data || res.data
      if (d?.name) {
        const c = contextChips.value.find(x => x.type === 'kb')
        if (c) c.label = t('knowledge.knowledge_base_name', { name: d.name })
      }
    } catch { /* 保留 id 占位 */ }
  }
  if (agent) {
    try {
      const res = await api.get('/v1/agents')
      const list = (Array.isArray(res.data?.data) ? res.data.data : []) as Array<{ id?: string; name?: string }>
      const a = list.find((x) => x.id === agent)
      if (a?.name) {
        const c = contextChips.value.find(x => x.type === 'agent')
        if (c) c.label = `Agent ${a.name}`
      }
    } catch { /* 列表取不到就保留 id 占位 */ }
  }
}

/** 移除单个上下文芯片：本地 context 与路由 query 双源同步（侧栏上下文面板触发） */
function removeContextChip(type: ContextChip['type'], value: string) {
  contextChips.value = contextChips.value.filter(c => !(c.type === type && c.value === value))
  const remaining = contextChips.value.filter(c => c.type === type).map(c => c.value)
  const q: LocationQuery = { ...route.query }
  // 同类还有剩余值时改写该项（多值即数组），否则整项删除
  if (remaining.length === 0) {
    if (q[type] === undefined) return
    delete q[type]
  } else {
    q[type] = remaining.length === 1 ? remaining[0] : remaining
  }
  void router.replace({ path: '/chat', query: q })
  appliedQueryKey = JSON.stringify(q)
}

/**
 * 调整某个知识库的检索权重（P1-8，侧栏「知识库权重」滑块触发）。
 *
 * 权重只存在**会话上下文**里（`runtime.context.kb_ids[].weight`），不进 URL：
 * URL 的 `?kb=` 约定是"哪些库"，权重是更细的意图，塞进 query 会把多值约定搅乱；
 * 而 runtime.context 本来就是"已激活能力"的单一事实源（见 contextChips 的注释），
 * 刷新 / 切会话都能原样还原。
 */
function setKbWeight(value: string, weight: number) {
  const chip = contextChips.value.find(c => c.type === 'kb' && c.value === value)
  if (!chip) return
  const next = clampKbWeight(weight)
  if ((chip.weight ?? DEFAULT_KB_WEIGHT) === next) return
  chip.weight = next
  persistRuntime({ context: buildWorkbenchContext(contextChips.value) })
}

/** 清空全部上下文：本地 context 与路由 query 一并清除（键取 PARAMS 定义，避免新增类型时漏清） */
function clearContext() {
  contextChips.value = []
  const q: LocationQuery = { ...route.query }
  let changed = false
  for (const key of CONTEXT_QUERY_KEYS) {
    if (q[key] !== undefined) { delete q[key]; changed = true }
  }
  if (changed) {
    void router.replace({ path: '/chat', query: q })
    appliedQueryKey = JSON.stringify(q)
  }
}

/** 统一任务模式：清空当前消息区（保留会话与上下文，可继续追问） */
function clearUnifiedMessages() {
  items.value = []
  currentTraceId.value = ''
  notify.info(t('workflow.unified_task_messages_cleared'))
}

/** 统一任务模式：退出（移除 task/error query；路由 watcher 触发 applyRouteQuery 重置消息区） */
async function exitUnifiedMode() {
  const q: LocationQuery = { ...route.query }
  delete q.task
  delete q.error
  await router.replace({ path: '/chat', query: q })
}

/** kb_hits 标签增强：跳转到引用的知识库详情 */
function openKb(kbId: string) {
  if (kbId) void router.push(`/knowledge/${encodeURIComponent(kbId)}`)
}

/** 组装发送时附带的 context（普通 SSE 模式与统一任务模式共用） */
function buildContext(): Record<string, unknown> | undefined {  // 单值字段 + 多值数组的组装规则集中在 contextChips 模块（新旧后端都能工作）。
  //
  // Agent 只发 agent_id，配置由网关按 id 补全（internal/api/agents.go 的
  // resolveAgentContext）。前端此前自己映射一份字段，且只映射了
  // name / system_prompt / model / max_turns —— Agent 自带的 tools / kb_id / skills
  // 全部丢失，用户选了 Agent 却发现"它不会用自己的工具"。把单一事实来源放回后端，
  // 两端就不会各自漂移。
  //
  // C3：项目不再由前端拼装 —— 服务端按会话归属注入 `context.project`
  //（session-map workspace id，记忆隔离的权威键）。前端传的任何值都会被它覆盖/清除，
  // 避免"记忆页手填的项目串"把未归组会话的记忆写进别的项目。
  const ctx = buildWorkbenchContext(contextChips.value) || {}
  // 人格：只传 id，提示词由服务端解析（见 personas.go 的文件头）。
  if (personaId.value) ctx.persona_id = personaId.value
  return Object.keys(ctx).length ? ctx : undefined
}

/** 安全改造：附件签名 URL 解析，/media/ 公开路径转短时效签名 URL；非 /media/ 前缀原样；失败回退原 url */
async function resolveAttachmentUrls(attachments?: ChatAttachment[]): Promise<ChatAttachment[]> {
  if (!attachments?.length) return []
  return Promise.all(attachments.map(async a => {
    if (!a.url || !a.url.startsWith('/media/')) return a
    const url = await resolveMediaUrl({ id: a.id, file_url: a.url })
    return url && url !== a.url ? { ...a, url } : a
  }))
}

/**
 * 消息列表组件句柄。**只用来提交"意图"**（回底），不碰 DOM —— 直接写 `.message-list` 的
 * `scrollTop` 会绕过 `transcriptViewport` 的输入租约与 writer provenance
 * （契约见 MessageList.vue 的 transcriptViewport 单写者约定："别处直接写 scrollTop 一律视为缺陷"）。
 */
const messageListRef = ref<InstanceType<typeof MessageList> | null>(null)

/** 拉取统一会话历史（GET /v1/chat/sessions/{id}/messages） */
async function loadUnifiedSession(sessionId: string) {
  loading.value = true
  try {
    const res = await getChatSessionMessages(sessionId)
    const d = (res?.messages ? res : (res?.data || {})) as {
      messages?: unknown[]
      shared_context?: { mode?: unknown }
    }
    const list = Array.isArray(d.messages) ? d.messages : []
    // 会话创建时的 mode（shared_context 优先，其次 query.mode，兜底 auto）
    const sharedMode = d.shared_context?.mode
    unifiedSubmitMode.value =
      (typeof sharedMode === 'string' && sharedMode) ||
      (typeof route.query.mode === 'string' && route.query.mode) ||
      'auto'
    items.value = buildUnifiedItems(list)
  } catch {
    errorBanner.value = errorBanner.value || t('errors.unified_session_failed_to_load_you_can_still_send_a_message_to_continue')
  } finally {
    loading.value = false
    // 会话加载完成后自动滚到底部：**走单写者**（它自己会先作废租约再跟随）
    await nextTick()
    messageListRef.value?.scrollToBottom()
  }
}

/** 统一会话消息 → 现有 ChatItem（user/assistant 映射现有消息组件；metadata 含 kb 时插知识库引用标签） */
function buildUnifiedItems(list: readonly unknown[]): ChatItem[] {
  const out: ChatItem[] = []
  ;(list || []).forEach((raw, idx) => {
    const m = (raw ?? {}) as {
      role?: unknown; content?: unknown; timestamp?: unknown
      created_at?: unknown; source?: unknown; origin?: unknown; metadata?: unknown
    }
    if (m.role !== 'user' && m.role !== 'assistant') return
    const content = typeof m.content === 'string' ? m.content : ''
    if (!content) return
    const stamp = m.timestamp ?? m.created_at
    const time = formatClock(typeof stamp === 'string' ? stamp : undefined)
    if (m.role === 'user') {
      out.push({
        kind: 'text', role: 'user', content: stripUserInputTag(content), time, id: `uni_u_${idx}`,
        // 来源标记（'subagent_followup' = 子 Agent 自动轮）：带上它，刷新后仍渲染成
        // 系统提示而不是用户气泡（见 internal/api/agent_followup.go）
        ...(typeof m.source === 'string' && m.source ? { source: m.source } : {}),
        // 回合来源（'superagent_takeover' = 接管巡检）：整轮都要能被认出来
        ...(typeof m.origin === 'string' && m.origin ? { origin: m.origin } : {}),
      })
    } else {
      const { reasoning, body } = splitThinking(content, { loose: true })
      if (reasoning) out.push({ kind: 'reasoning', content: reasoning, time, id: `uni_r_${idx}` })
      if (body) {
        out.push({
          kind: 'text', role: 'assistant', content: body, time, id: `uni_a_${idx}`,
          ...(typeof m.origin === 'string' && m.origin ? { origin: m.origin } : {}),
          metadata: normalizeMeta(m.metadata),
        })
        const meta = normalizeMeta(m.metadata) || {}
        const kbId = typeof meta.kb_id === 'string' ? meta.kb_id : ''
        const n = typeof meta.kb_hits === 'number' ? meta.kb_hits : kbId ? 1 : 0
        if (kbId || n > 0) {
          out.push({ kind: 'kb_hits', count: n, kb_id: kbId, id: `uni_k_${idx}` } as unknown as ChatItem)
        }
      }
    }
  })
  return out
}

/** 统一任务模式发送：POST /v1/chat/submit，返回 output 追加为 assistant 消息 */
async function sendUnified(text: string, attachments?: ChatAttachment[]) {
  if (!unifiedSessionId.value) return
  // 统一任务模式同样必须先有模型：空 model 一样会落到后端默认路由（见 ensureModel）
  if (!ensureModel()) return
  loading.value = true
  startTurnTimer()
  appendUserText(text, attachments)
  const userItemId = items.value[items.value.length - 1]?.id
  currentTraceId.value = ''
  try {
    // 安全改造：附件若为 /media/ 公开路径，先解析为签名 URL 再随消息发送（loading 期间发送已禁用）
    const resolvedAtts = await resolveAttachmentUrls(attachments)
    const res = await api.post('/v1/chat/submit', {
      message: text,
      session_id: unifiedSessionId.value,
      mode: unifiedSubmitMode.value || 'auto',
      context: buildContext(),
      // 模型路由：统一任务发送同样携带 llm_config.model（ensureModel 已保证非空）
      llm_config: { model: llmModel.value },
      ...(resolvedAtts.length
        ? { attachments: resolvedAtts.map(a => ({ id: a.id, name: a.name, mime_type: a.mimeType, url: a.url, is_image: a.isImage })) }
        : {}),
    })
    const d = res.data?.data !== undefined ? res.data.data : (res.data || {})
    if (d.success === false) throw new Error(d.error || t('errors.request_failed_2'))
    currentTraceId.value = d.trace_id || ''
    appendAssistantWithKb(d.output || '', d.metadata || {})
    flashUnifiedDone()
  } catch (e) {
    const reason = describeApiError(e)
    markMessageFailed(userItemId, reason, describeFailureMeta(e))
    notify.error(t('errors.send_failed') + reason)
  } finally {
    loading.value = false
    stopTurnTimer()
  }
}

/** 追加 assistant 消息；metadata 含 kb_hits/kb_id 时在其下显示"引用了知识库(×N)"小标签 */
function appendAssistantWithKb(content: string, meta: unknown) {
  // 统一任务模式的 output 同为引擎产出（可含多段 [thinking] 块）→ 用 loose 状态机解析
  const { reasoning, body } = splitThinking(String(content), { loose: true })
  if (reasoning) items.value.push({ kind: 'reasoning', content: reasoning, id: genItemId() })
  if (body) {
    items.value.push({
      kind: 'text', role: 'assistant', content: body, id: genItemId(),
      metadata: normalizeMeta(meta),
    })
    const m = (meta ?? {}) as { kb_hits?: unknown; kb_id?: unknown }
    const kbId = typeof m.kb_id === 'string' ? m.kb_id : ''
    const n = typeof m.kb_hits === 'number' ? m.kb_hits : kbId ? 1 : 0
    if (kbId || n > 0) {
      items.value.push({ kind: 'kb_hits', count: n, kb_id: kbId, id: genItemId() } as unknown as ChatItem)
    }
  }
}

// 统一任务模式：新消息后自动滚到底部
watch(() => items.value.length, async () => {
  if (!unifiedMode.value) return
  await nextTick()
  const el = document.querySelector<HTMLElement>('.unified-list')
  if (el) el.scrollTop = el.scrollHeight
})

// 侧面板（主从时间线：轨迹 / 会话历史）。
//
// 折叠状态统一交给 usePanelCollapse：宽屏默认常驻展开、≤1024px 默认抽屉收起，
// 并且**记住用户的选择**（此前每次刷新都回到按屏幕算出来的默认值，用户把侧栏关掉后
// 一刷新它又自己回来了）。窄屏自动收起不会覆盖宽屏偏好，详见 composable 注释。
const { open: panelOpen } = usePanelCollapse({
  storageKey: 'chiron:chat-side-panel:v1',
  defaultOpen: () => window.matchMedia('(min-width: 1025px)').matches,
  narrowQuery: '(max-width: 1024px)',
})
const PANEL_VIEW_KEY = 'chiron:chat-panel-view:v1'
function readPanelView(): 'trajectory' | 'sessions' {
  try {
    const raw = globalThis.localStorage?.getItem(PANEL_VIEW_KEY)
    return raw === 'sessions' ? 'sessions' : 'trajectory'
  } catch {
    return 'trajectory'
  }
}
const panelView = ref<'trajectory' | 'sessions'>(readPanelView())
watch(panelView, (view) => {
  try {
    globalThis.localStorage?.setItem(PANEL_VIEW_KEY, view)
  } catch {
    /* 存不下不影响使用 */
  }
})

// 窄屏侧栏是抽屉：打开时主区必须 inert，键盘/读屏不能穿透到被遮罩的正文。
const narrowSidePanel = ref(false)
let narrowSideQuery: MediaQueryList | null = null
function syncNarrowSidePanel() {
  narrowSidePanel.value = narrowSideQuery?.matches ?? false
}
/**
 * 会话地图：**独立的整屏大窗格**（不是侧栏里的一个小视图 —— 侧栏太窄，
 * 放不下"空间化排布"这件事本身）。位置稳定性是它的立身之本，
 * 见 internal/session/manager_branch.go。
 */

/**
 * 会话地图浮层开关。
 *
 * 画布现在是 SPA 内的一个视图（`src/views/sessionmap/SessionMapView.vue`），
 * 不再是 `public/sessionmap/` 里的 iframe。旧实现的三个结构性问题是这次重构的直接动因：
 *
 *   1. token 经 iframe query 下发（`?token=…`）—— 现在同源同 SPA，axios 自带鉴权头；
 *   2. `adapter.js` 劫持 `window.fetch` 并把 Promise 转成 postMessage RPC —— 现在直接 await；
 *   3. `app.innerHTML` 全量重渲染 —— 现在由 Vue 的响应式按卡片粒度更新。
 *
 * 旧 iframe 不留回退路径：两套渲染同时存在时，"哪一个才是真相"会长期没有答案。
 */
const synapseMapOpen = ref(false)

/**
 * 会话 id → 正在流式输出的尾部文本（卡片预览）。
 *
 * 只投影 `loading` 为真的会话：`streaming` 条目只在这期间存在，遍历全部会话纯属白算。
 * 用 computed 而不是 watch + 手动维护一份 Map —— 内容本来就在 `runtimes` 里，
 * 多一份缓存就多一处漂移，而漂移后的表现是「地图与对话页像两个模型在说话」。
 */
const mapStreamPreviews = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {}
  for (const [sid, run] of runtimes) {
    // `__none__` 是"未选中会话"的占位键，不是真实会话，不该出现在地图上
    if (sid === '__none__' || !run.loading) continue
    const text = pickStreamPreview(run.items)
    if (text) out[sid] = text
  }
  return out
})
// ── 会话地图（同 SPA 视图，不再是 iframe）──
//
// 旧实现把会话操作编成 postMessage RPC：地图侧 `dshRpc(...)` 发出后**等一条带
// requestId 的回执**（20 秒超时），宿主侧需要一个 host 对象逐条实现，任何一条漏实现
// 都会表现成"地图里点了没反应"。现在这些操作由 `useMapActions` 直接 await 宿主 REST
// （`SessionMapView.vue` 里调用），没有回执、没有超时、错误用 try/catch 就地显示。

const trajectoryFocus = ref<number | null>(null)
const trajectoryToken = ref(0)

/**
 * 子 Agent 实时流式通道（会话级常驻 SSE + 50ms 合批 + 按 run 分组缓冲）。
 *
 * 整块实现搬到了 `composables/useSubagentStream.ts`（P2-1 拆分），这里只负责把
 * 三件事的取值方式接进去：当前会话 id、事件归属的 run 状态（写作用域优先）、
 * 「本轮主 SSE 是否开着」（开着就跳过，避免同一帧被两个连接各送一份）。
 *
 * 建连入口只有一处（composable 内的 `watch(activeSessionId)`）：这里**刻意不取**
 * `ensureSubagentStream`，视图层没有第二条建连路径。单写者由
 * `composables/__tests__/useSubagentStream.spec.ts` 钉住。
 */
const {
  subagentLiveEvents,
  pushSubagentEvent,
  disposeSubagentStream,
} = useSubagentStream({
  sessionId: () => activeSessionId.value,
  resolveRun: () => writingRun.value ?? currentRun.value,
  isTurnStreamOpen: (sid?: string) => hasSessionStream(sid || ''),
})

/**
 * 「这个会话还在动」的三份派生：状态栏角标、会话地图的运行中标记、侧栏心跳点。
 *
 * 规则搬到了 `composables/useSessionActivity.ts`（P2-1 第二刀）；这里只决定两个输入：
 * 子 Agent 实时事件（会话级 SSE 推来的那一份）与主回合仍 `loading` 的会话集合。
 * 取并集的理由在 composable 里 —— 两个信号各有盲区。
 */
const { subagentActiveCount, sidebarRunningIds } = useSessionActivity({
  subagentLiveEvents: () => subagentLiveEvents.value,
  liveRunSessionIds: () => liveRunSessionIds.value,
})

/**
 * 会话地图的实时状态。
 *
 * 旧实现靠 `postMessage` 把宿主的运行态与流式文本推给 iframe 里的画布，改造时
 * 那条通道随 iframe 一起被删掉 —— 于是地图只剩「运行中」脉冲点，看不到正在生成的
 * 文字。现在改为同进程单向 props：
 *   - `mapRunningSessionIds` 给卡片打「运行中」标记
 *   - `mapStreamPreviews` 给卡片提供正在输出的尾部文本
 * 不再有消息协议，也就没有"端口通配 '*'"、`origin === 'null'`、20 秒回执超时这一整类问题。
 */
const mapRunningSessionIds = computed<string[]>(() => [...sidebarRunningIds.value])

/**
 * 子 Agent 卡片「重试」：把失败任务放回输入框，而不是替用户再派发一次。
 *
 * 后端没有"重跑某个 run"的端点（run 是父 Agent 的工具调用产物），前端伪造一次
 * 派发只能编造上下文；把文本交给用户确认，是唯一不撒谎的重试路径。
 */
function onSubagentRetry(payload: { runId: string; text: string }) {
  const text = String(payload?.text || '').trim()
  if (!text) return
  chatInputRef.value?.setDraft?.(text)
}

function onTrajectoryFocus(index: number) {
  trajectoryFocus.value = index
  trajectoryToken.value += 1
}

// 打开面板并直达指定视图；点击已激活的入口则收起
function openPanel(view: 'trajectory' | 'sessions') {
  if (panelOpen.value && panelView.value === view) {
    panelOpen.value = false
    return
  }
  panelView.value = view
  panelOpen.value = true
}

/** ChatInput「上下文」快捷按钮：确保上下文面板展开（抽屉模式下亦然）并直达轨迹视图 */
function openContextPanel() {
  panelView.value = 'trajectory'
  panelOpen.value = true
}

// turn 计时（deepseek turnStatusClock）
const turnElapsed = ref(0)
const connectionLost = ref(false)  // SSE 断线横幅（deepseek ConnectionBanner）
let turnTimer: ReturnType<typeof setInterval> | null = null

function startTurnTimer() {
  turnElapsed.value = 0
  if (turnTimer) clearInterval(turnTimer)
  turnTimer = setInterval(() => { turnElapsed.value += 1 }, 1000)
}

function stopTurnTimer() {
  if (turnTimer) { clearInterval(turnTimer); turnTimer = null }
}

/** 会话缓存键：按账号隔离（localStorage 是浏览器级的，不带账号维度会跨账号残留） */
const sessionsCacheKey = computed(() => privateKey('chat_sessions', authStore.user?.id))
function persistSessions() {
  try { localStorage.setItem(sessionsCacheKey.value, JSON.stringify(sessions.value)) } catch { /* 隐私模式 / 配额满：放弃持久化 */ }
}

// ── 会话元数据：重命名 / 置顶 / 标签 / 排序（实现见 composables/useSessionMeta.ts）──
const {
  sortSessions,
  togglePin,
  setSessionTag,
  renameTarget,
  renameDraft,
  renaming,
  openRename,
  confirmRename,
} = useSessionMeta({ sessions, persistSessions })

// ── 全局快捷键（定义见 config/shortcutCatalog.ts，实现见 composables/useKeyboardShortcuts.ts）──
// 声明顺序即优先级：原 if 链里"先命中先 return"的语义，现在由数组顺序承载。
// Esc 的实际顺序是 **地图 > 停止生成 > 浮窗 > 侧栏** —— 地图是 iframe 浮在最上层，
// 所以最先关；"停止生成"排第二，因为那时用户的意图是停下，而不是关面板。
// 这里只补 handler / when，键位与顺序一律以目录为准（设置页读的是同一份）。
const shortcutsHelpOpen = ref(false)

/**
 * 从目录里取一条定义。
 *
 * 入参类型是 :type:`ShortcutId`（由 `as const` 推出的字面量联合），
 * 所以把 id 写错是**编译错误**而不是"这条快捷键悄悄不生效"。
 */
function specOf(id: ShortcutId) {
  const spec = SHORTCUT_CATALOG.find(s => s.id === id)
  // find 的返回类型是可能 undefined；目录是常量，走到这里说明目录本身写错了。
  // 开发者错误信息用英文：它是给维护者看的，不进 UI，也就不该占 i18n 的账
  if (!spec) throw new Error(`shortcutCatalog is missing "${id}"`)
  return spec
}

useKeyboardShortcuts({
  userId: authStore.user?.id ?? null,
  bindings: [
    {
      ...specOf('close-shortcuts-help'),
      // 速查表自己是最上层浮层：Esc 必须先关它，不能穿透到下面的面板
      when: () => shortcutsHelpOpen.value,
      handler: () => { shortcutsHelpOpen.value = false },
    },
    {
      ...specOf('open-session-list'),
      handler: () => {
        panelOpen.value = true
        panelView.value = 'sessions'
        nextTick(() => {
          const searchInput = document.querySelector('.panel-search .search-input') as HTMLInputElement | null
          searchInput?.focus()
        })
      },
    },
    {
      ...specOf('find-in-transcript'),
      // 有意覆盖浏览器的页面查找：应用内查找才回答得出"我在这次会话里什么时候问过什么"，
      // 而浏览器查找只会匹配**当前已挂载的行** —— 我们用的是窗口化列表，
      // 未渲染的历史段落它根本扫不到（这正是"找不到明明存在的内容"的来源）。
      handler: () => {
        panelOpen.value = true
        panelView.value = 'trajectory'
        nextTick(() => {
          const input = document.querySelector('.panel-search .search-input') as HTMLInputElement | null
          input?.focus()
        })
      },
    },
    {
      ...specOf('show-shortcuts'),
      // 速查表：只在没有打开任何浮层时响应，免得和 Esc 的逐层关闭抢注意力
      when: () => !synapseMapOpen.value && !statsOpen.value,
      handler: () => { shortcutsHelpOpen.value = true },
    },
    {
      ...specOf('close-session-map'),
      when: () => synapseMapOpen.value,
      handler: () => { synapseMapOpen.value = false },
    },
    {
      ...specOf('stop-generation'),
      when: () => loading.value,
      handler: () => stopGeneration(),
    },
    {
      ...specOf('close-overlays'),
      when: () => statsOpen.value || healthOpen.value,
      handler: () => { statsOpen.value = false; healthOpen.value = false },
      // 原实现这两层都不拦默认行为：走到这里说明没有更需要抢占的语义了
      preventDefault: false,
    },
    {
      ...specOf('close-panel'),
      // 所有“辅助面板”共用一个 Esc 语义：子 Agent 栏 > 分屏参考 > 工作台 > 侧栏。
      // 顺序按“打开后离用户当前任务最近”排，而不是按打开时间。
      when: () =>
        panelOpen.value || workbenchOpen.value || !!splitSessionId.value || subagentDockOpen.value,
      handler: () => {
        if (subagentDockOpen.value) setSubagentDockOpen(false)
        else if (splitSessionId.value) splitSessionId.value = ''
        else if (workbenchOpen.value) setWorkbenchOpen(false)
        else panelOpen.value = false
      },
      preventDefault: false,
    },
  ],
})

// ── 会话 CRUD（保留原逻辑） ──
onMounted(async () => {
  unregisterAuthScopedReset?.()
  // N7：标签状态按账号隔离；先载入再做任何项目解析，否则首个会话会写进上一个账号的键。
  loadProjectWorkspace(authStore.user?.id || '')
  // F10：从全局快捷键/其它页面跳来时，挂载后聚焦输入框；已在聊天页时由 onQuickAsk 处理。
  const focusComposer = () => { void nextTick(() => chatInputRef.value?.focus?.()) }
  if (consumeQuickAsk()) focusComposer()
  unsubQuickAsk = onQuickAsk(focusComposer)
  // 迷你输入框提交（F10 后半）：本页已挂载时直接发送；挂载前到达的那条由下面的
  // `consumeQuickAskText()` 在会话恢复之后消费。
  unsubQuickAskText = onQuickAskText(text => { void sendMessage(text) })
  unregisterAuthScopedReset = registerAuthScopedReset(() => {
    // 认证切换：续传游标与全部会话流一起作废（旧身份的事件流不该在新身份下继续被消费）
    sseLastIdBySession.clear()
    closeAllSessionStreams()
    interactionScope.clear()
  })
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    narrowSideQuery = window.matchMedia(narrowSidebarQuery())
    syncNarrowSidePanel()
    narrowSideQuery.addEventListener('change', syncNarrowSidePanel)
  }
  // 互联互通：解析 /chat query（task / error / kb / agent / skill）
  await applyRouteQuery()
  void checkBudgetWarning()
  await loadSessions()
  // 持久化的参考栏目标可能已被删除：列表加载后校验一次，避免恢复出一个空参考栏。
  if (splitSessionId.value && !sessions.value.some(s => s.id === splitSessionId.value)) {
    splitSessionId.value = ''
  }
  void loadPersonas()
  // N7：项目目录用于标签名与失效校正；不阻塞首屏（拉不到就沿用本地记录）。
  void refreshProjectCatalogue().then(ok => {
    if (ok) pruneProjectWorkspace(allProjects.value.map(p => p.id))
  })
  // 统一任务模式不自动切换普通会话；其余保持原有行为。
  // N7：优先恢复当前项目标签上次停留的会话；它已删则退回最近会话。
  const rememberedSession = projectWorkspace.active ? lastProjectSession(projectWorkspace.active) : ''
  if (!unifiedMode.value && rememberedSession && sessions.value.some(s => s.id === rememberedSession)) {
    await switchSession(rememberedSession)
  } else if (!unifiedMode.value && sessions.value.length > 0) {
    await switchSession(sessions.value[0].id)
  }
  // 迷你输入框提交（F10 后半）：**等会话恢复完**再发。提前发会在 activeSessionId
  // 仍为空时新建一个会话，与正在恢复的历史会话并存 —— 用户看到的是"多出一个空会话"。
  const pendingAsk = consumeQuickAskText()
  if (pendingAsk) void sendMessage(pendingAsk)
  // 互联互通：同一路由下 query 变化（如 WorkstationNav 再次跳转）
  watch(() => route.query, () => applyRouteQuery())
  // 监听网络在线/离线状态
  window.addEventListener('online', onOnline)
  window.addEventListener('offline', onOffline)
  // 逐组件健康轮询：状态栏的「已连接」要有后端依据，而不是只看浏览器是否在线
  startHealthPolling()
  // 全局键盘快捷键由 useKeyboardShortcuts 自行注册/注销（见文件上方）
  // 新用户引导（P0-3）：首次进入会话页时按服务端进度决定要不要弹。
  // 不 await：引导是可选的，不该拖慢会话加载；接口失败在 composable 里静默吞掉。
  if (authStore.user?.id) void maybeOpenOnboarding()
})

onUnmounted(() => {
  unsubQuickAsk?.()
  unsubQuickAsk = undefined
  unsubQuickAskText?.()
  unsubQuickAskText = undefined
  unregisterAuthScopedReset?.()
  unregisterAuthScopedReset = undefined
  stopTurnTimer()
  closeAllSessionStreams()
  disposeSubagentStream()
  if (unifiedDoneTimer) { clearTimeout(unifiedDoneTimer); unifiedDoneTimer = null }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  if (healthTimer) { clearInterval(healthTimer); healthTimer = null }
  window.removeEventListener('online', onOnline)
  window.removeEventListener('offline', onOffline)
  narrowSideQuery?.removeEventListener('change', syncNarrowSidePanel)
})

// 离线监听 + 自动重连
const isOnline = ref(navigator.onLine)
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectAttempts = 0

/**
 * 逐组件健康态（pg / redis / engine / gateway）。
 *
 * 为什么不能只信 `navigator.onLine`：浏览器有网 ≠ 后端可用。postgres、redis、
 * 引擎任一挂掉时，旧逻辑仍然显示「已连接」，用户接着提问就一路失败，
 * 却不知道是哪个组件的问题。这里每 30s 拉一次聚合健康态，
 * 状态栏的「已连接」以**全部组件可用**为准，并能点开看明细。
 */
const componentsHealth = ref<ComponentsHealth | null>(null)
const healthOpen = ref(false)
let healthTimer: ReturnType<typeof setInterval> | null = null
/** 面板打开时的刷新周期：用户正盯着看，30s 会让"刚重启完"的结论迟迟不出现。 */
const HEALTH_FAST_INTERVAL_MS = 3000
/** 面板关闭时的刷新周期：状态栏只要"连接/断开"这个结论，没必要高频打网关。 */
const HEALTH_SLOW_INTERVAL_MS = 30000

/** 全部关键组件都可用才算「已连接」。取不到数据（null）时**不**改变原判定，避免误报离线。 */
const componentsUp = computed(() => {
  const c = componentsHealth.value
  if (!c) return true
  return isComponentUp(c.postgres) && isComponentUp(c.redis) && isComponentUp(c.engine)
})

async function loadComponentsHealth() {
  const next = await fetchComponentsHealth()
  // 拉取失败保留上次结果：一次抖动不该让状态栏在「在线/离线」之间闪
  if (next) componentsHealth.value = next
}

/**
 * 按当前面板开合决定刷新节奏。
 *
 * 为什么不是一个固定周期：这两个场景的诉求相反 ——
 *   · 面板**开着**时用户在看数字，30s 的间隔会让「刚点完重启、什么时候恢复」永远慢半拍；
 *   · 面板**关着**时它只喂状态栏一个「已连接/断开」的结论，高频轮询纯属给网关加压
 *     （这条请求每次都要探 PG 体积 + Redis INFO + 拉引擎 /metrics，并不便宜）。
 * 所以开合切换时重挂定时器，而不是开两个定时器互相干扰。
 */
function scheduleHealthPolling() {
  if (healthTimer) {
    clearInterval(healthTimer)
    healthTimer = null
  }
  const interval = healthOpen.value ? HEALTH_FAST_INTERVAL_MS : HEALTH_SLOW_INTERVAL_MS
  healthTimer = setInterval(() => { void loadComponentsHealth() }, interval)
}

function startHealthPolling() {
  void loadComponentsHealth()
  scheduleHealthPolling()
}

/** 打开面板时立刻拉一次（不必等下一个周期），然后切到快节奏。 */
watch(healthOpen, (open) => {
  if (open) void loadComponentsHealth()
  scheduleHealthPolling()
})

function onOffline() {
  isOnline.value = false
  connectionLost.value = true
  // 离线时停止 SSE 重试
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
}

function onOnline() {
  isOnline.value = true
  // 上线后指数退避重连，恢复会话
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectAttempts = 0
  // **所有**仍在跑的会话都要重挂，而不是只恢复当前视图那一条：
  // 断网期间每条流都把重试用尽并 close，后台会话的流不会因为切走就自动回来。
  for (const [sid, run] of runtimes) {
    if (!run.loading || sseBySession.has(sid)) continue
    attachSessionStream(sid, { sinceEventId: sseLastIdBySession.get(sid) || '' })
  }
  attemptReconnect()
}

async function attemptReconnect() {
  if (!isOnline.value) return
  reconnectAttempts++
  // 指数退避：1s, 2s, 4s, 8s, 16s（最大 16s）
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 16000)
  if (reconnectAttempts > 1) {
    await new Promise(r => setTimeout(r, delay))
  }
  if (!isOnline.value) return
  try {
    await api.get('/health', { timeout: 5000 })
    connectionLost.value = false
    reconnectAttempts = 0
    // H60：重连不是"再调一次 switchSession"就完了 —— 那条路径对**同一个 id** 会直接返回，
    // 于是断线期间的历史从未重拉；跑着的流也不会被重新挂上（服务端可能仍在生成）。
    const plan = planReconnect({
      sessionId: activeSessionId.value,
      wasLoading: Boolean(currentRun.value.loading),
      lastEventId: sseLastIdBySession.get(activeSessionId.value) || '',
    })
    if (plan.refreshHistory && activeSessionId.value) {
      await switchSession(activeSessionId.value, { force: true })
    }
    if (plan.reattachStream && activeSessionId.value) {
      attachSessionStream(activeSessionId.value, { sinceEventId: plan.sinceEventId })
    }
  } catch {
    if (reconnectAttempts < 5) {
      reconnectTimer = setTimeout(attemptReconnect, delay)
    }
  }
}

// 导出当前会话为 Markdown 文件
function exportMarkdown() {
  if (!items.value.length) {
    notify.warning(t('chat.no_messages_available_to_export'))
    return
  }
  const session = sessions.value.find(s => s.id === activeSessionId.value)
  const title = session?.title || t('chat.export_conversation')
  const lines: string[] = [`# ${title}`, '']
  for (const it of items.value) {
    if (it.kind !== 'text') continue
    const role = it.role === 'user' ? t('admin.user_3') : t('common.assistant_3')
    lines.push(`## ${role}`, '')
    lines.push(it.content || t('chat.empty_message'))
    if (it.attachments?.length) {
      lines.push('')
      for (const a of it.attachments) {
        if (a.isImage) lines.push(`![${a.name}](${a.url})`)
        else lines.push(`- 📎 [${a.name}](${a.url}) (${formatSize(a.size)})`)
      }
    }
    lines.push('')
  }
  const toolCalls = items.value.filter(i => i.kind === 'tool_call')
  if (toolCalls.length) {
    lines.push('---', '', t('agent.tool_calls'), '')
    for (const tc of toolCalls) {
      if (tc.kind !== 'tool_call') continue
      lines.push(`### ${tc.name || 'tool'}`, '```json', tc.arguments || '{}', '```', '')
    }
  }
  const md = lines.join('\n')
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
  notify.success(t('common.markdown_exported'))
}

// 斜杠命令处理
function onSlashCommand(cmd: string) {
  switch (cmd) {
    case '/clear':
      // 先清面板再清 activeSessionId：反过来的话取不到会话 id，
      // 旧清单会留在 map 里，下次切回这个 id 时"复活"出一个已经清空的清单。
      todoPanel.clear(activeSessionId.value)
      items.value = []
      activeSessionId.value = ''
      notify.info(t('chat.current_conversation_cleared'))
      break
    case '/export':
      exportMarkdown()
      break
    case '/new':
      todoPanel.clear(activeSessionId.value)
      items.value = []
      activeSessionId.value = ''
      panelOpen.value = false
      notify.info(t('chat.session_created'))
      break
    case '/theme':
      themeStore.toggleTheme()
      notify.success(themeStore.isDark ? t('common.switched_to_dark_mode') : t('common.switched_to_light_mode'))
      break
    case '/stop':
      stopGeneration()
      break
    // 引擎侧命令（app/commands/）：前端不重复实现，作为消息发出，由引擎在进推理循环前拦截。
    // 只列真正的引擎命令 —— 笔误仍会落到 default 拿到"未知命令"提示，不会被静默当普通提问。
    case '/undo':
    case '/compact':
    case '/context':
    case '/cost':
    case '/help':
    case '/think':
    case '/act':
    case '/retry':
      void sendMessage(cmd)
      break
    // 带参命令：**只填入输入框、不发送** —— 缺参调用对引擎没有意义（`/model` 不知道该切哪个模型）。
    case '/model':
    case '/temperature':
    case '/skill':
    case '/memory':
      chatInputRef.value?.setCommandDraft(cmd)
      break
    default:
      notify.warning(t('common.unknown_command_cmd', { cmd }))
  }
}

// 全局键盘快捷键
async function loadSessions() {
  try {
    const res = await api.get('/v1/conversations')
    const apiSessions = res.data?.data || res.data || []
    // 服务端成功响应即为**权威答案**（空列表 = 当前账号确实没有会话）：绝不能用本地缓存兜底。
    // 那份缓存不区分账号，换账号后会把上一个账号的会话带进来，前端随即自动切到「别人的会话」，
    // 于是会话详情 / 模式读写全部 403（表现为"加载失败"）。
    sessions.value = Array.isArray(apiSessions) ? apiSessions : []
    persistSessions()
  } catch {
    // 仅**请求失败**时回退本账号缓存，保证离线/后端异常时仍看得到列表
    const raw = localStorage.getItem(sessionsCacheKey.value)
    if (raw) { try { sessions.value = JSON.parse(raw) } catch { sessions.value = [] } }
  }
  sortSessions()
}

async function createSession(projectId?: string) {
  let session: ChatSession | null = null
  try {
    const res = await api.post('/v1/conversations', { title: t('chat.new_conversation'), llm_config: buildPersistLlmConfig() })
    const data = res.data?.data || res.data
    if (data?.id) session = { id: data.id, title: data.title || t('chat.new_conversation'), created_at: data.created_at, updated_at: data.updated_at }
  } catch { /* fallback */ }
  // 后端没建成时下面会退回本地 id：那个会话在服务端并不存在，归属自然也写不进去。
  // 记下这个区别，否则用户会把"会话创建失败"看成"归入项目失败"。
  const persisted = Boolean(session)
  if (!session) {
    const id = crypto.randomUUID()
    session = { id, title: t('chat.new_conversation'), created_at: new Date().toISOString(), updated_at: new Date().toISOString() }
  }
  // 从项目行里新建：先把归属写进后端，再切过去 ——
  // `switchSession` 会触发 activeProject 的查询，顺序反了就会先拉到"无项目"，
  // 工作台与 `@` 引用都指向错误目录，直到下一次刷新才纠正。
  // 归属失败不中断：会话已经建好，只是落进了「未归入」，那比丢掉会话好。
  if (projectId && persisted) {
    try {
      const { bindSession } = await import('../views/sessionmap/projectsApi')
      await bindSession(projectId, session.id)
    } catch {
      notify.error(t('chat.projects_bind_failed'))
    }
  }
  sessions.value.unshift(session); persistSessions()
  panelView.value = 'trajectory'
  try { await switchSession(session.id) } catch { /* ignore */ }
}

/**
 * 会话地图请求切换到某个会话。
 *
 * 地图可能在宿主列表还没这个会话时就请求切换（它刚新建/分叉完）。此时先拉一次列表，
 * 否则侧栏里找不到它 —— 旧 iframe 版靠一条 postMessage 回推当前会话、再做一次
 * 隐式 placeSession，是同一件事的绕路写法。
 *
 * 切完就关浮层：地图是"换一个角度看会话"，选定之后回到对话页才是主线。
 */
async function onMapSwitchSession(sessionId: string) {
  if (!sessionId) return
  if (!sessions.value.some(s => s.id === sessionId)) await loadSessions()
  synapseMapOpen.value = false
  await switchSession(sessionId)
}

async function switchSession(id: string, options: { force?: boolean } = {}) {
  // 互联互通：从统一任务模式切到普通会话时，移除 task/error（保留 kb/agent/skill 上下文）
  if (unifiedMode.value && unifiedSessionId.value) {
    const q = { ...route.query }
    delete q.task
    delete q.error
    await router.replace({ path: '/chat', query: q })
    appliedQueryKey = JSON.stringify(q)
    unifiedSessionId.value = ''
  }
  // H60：`force` 供**断线重连**使用。默认的"同 ID 直接返回"是为避免无意义重复拉取，
  // 但重连恰恰需要重拉：断线期间的消息/工具结果只在服务端，界面还停在旧内容上。
  if (id === activeSessionId.value && !options.force) return
  // 竞态守卫 + 分页状态复位（见 composables/useSessionSwitch.ts）
  const mySeq = beginSwitch()
  // 多会话运行：切换会话**只换视图** —— 不再清空 items、也不再无条件置 loading。
  // 后台会话继续跑，它的 items/loading 留在自己的 run state 里；
  // 切回来时读到的就是它自己的（可能正在增长的）列表。
  // 顺序要紧：先设 activeSessionId，后面的 currentRun 才指向新会话。
  activeSessionId.value = id
  // 票据**不清**（H59）：票属于**它到达时**的会话（`pendingApprovals` 已是"当前会话"的投影），
  // 切走只是不再显示它 —— 旧实现把 A 的票一起清空，而服务端仍在等 A 的答复，
  // 用户切回去时卡片再也不会出现。后台会话的提问也继续按自己的到期时间判定。
  ensureAskTimer()
  // 本会话没在跑却还挂着一条流 ⇒ 那是陈旧句柄（上一轮已收尾），收掉它。
  // 正在跑的会话**保流**：切回来才续得上（旧实现这里会把指针置空，于是切回来就停不了它）。
  if (!currentRun.value.loading) closeSessionStream(id)
  // 模式（对话模式 + 工具授权模式）不在此同步：它们是前端全局实时状态，
  // 切换会话既不改变它们，也不从会话状态读回（见 buildLlmConfig 的说明）。
  let serverRunning = false
  try {
    const res = await api.get(`/v1/conversations/${id}?limit=${HISTORY_PAGE_SIZE}`)
    if (isStale(mySeq)) return
    const data = res.data?.data || res.data
    serverRunning = !!data?.running
    if (data?.messages) {
      // 该会话正在生成（或流里已累积过内容）时，**不要用拉回的历史覆盖它** ——
      // 否则"切走再切回"的瞬间会把这段时间里增长的内容冲掉。
      const projected = projectHistory(currentRun.value, data.messages, data.tool_calls)
      if (projected) items.value = projected
      // 刷新后恢复任务清单：`todo_updated` 是实时事件、不会重放，不从这里补一次，
      // 用户一刷新就看不到自己在跑的长任务（而模型那边仍有清单，因为它读的是 tool 消息）。
      //
      // 只在**当前没有板子**时投影：已有板子说明实时事件已经更新过它，历史里那份更旧 ——
      // 用旧数据覆盖新数据会让面板一刷新就倒退。
      if (!todoPanel.hasTodos(id)) {
        const restored = boardFromHistory(data.tool_calls || [])
        // 用 setBoard 而不是 applyEvent：restored 已经投影过（rows），
        // 再喂给 applyEvent 会按原始条目形状重新解析一遍，把 dependsOn/owner 丢掉。
        todoPanel.setBoard(id, restored, 0)
      }
      applyPageMeta(data)
    }
    // 会话级运行时状态（model / provider / 已激活上下文）：Redis 热 + unified_sessions.runtime
    // 持久，切回会话时回填。**模式不在其中** —— 对话模式与工具授权模式是前端全局实时状态，
    // 不随会话切换而改变（见 buildLlmConfig 的说明）。
    let cfg: unknown = (data as { llm_config?: unknown } | undefined)?.llm_config
    if (typeof cfg === 'string') { try { cfg = JSON.parse(cfg) } catch { cfg = undefined } }
    let rt: SessionRuntime | null = null
    try {
      rt = (await getSessionRuntime(id)).runtime
    } catch { /* 不可用时静默回落 llm_config */ }
    // 模型兜底（**这一条就是 "Model is unavailable" 的直接原因**）：
    // 会话没记模型时，过去会留空 → 提交不带 model → 落到**后端默认模型**；
    // 而一旦后端默认模型被上游下架（日志：opencode-go 的模型数 37→33），
    // 就会直接报 "Upstream request failed: Model is unavailable."。
    // 所以按「会话记录 → 会话 llm_config → 可用模型列表第一个 → 空」四级回落，
    // 绝不把空值当成"可以用后端默认"来提交。
    const cfgModel = (cfg && typeof cfg === 'object' && 'model' in cfg)
      ? (cfg as { model?: unknown }).model
      : undefined
    llmModel.value =
      (typeof rt?.model === 'string' && rt.model)
      || (typeof cfgModel === 'string' && cfgModel)
      || availableModels.value[0]?.name
      || ''
    // 已激活能力（问题 3）：runtime.context 是单一事实源 —— 把 URL 未带入、
    // 但会话上已保存的激活项（知识库/Agent/技能/插件/记忆分类）补进侧栏展示，
    // 否则刷新后界面上就看不到"这次对话激活了什么"。
    const fromRuntime = chipsFromWorkbenchContext(rt?.context)
    if (fromRuntime.length) {
      const merged = [...contextChips.value]
      for (const chip of fromRuntime) {
        const existing = merged.find(c => c.type === chip.type && c.value === chip.value)
        if (!existing) {
          merged.push(chip)
        } else if (
          // URL 只带 `?kb=id`（没有权重）：把会话上保存的权重补回已有 chip，
          // 否则"刷新后权重悄悄回到 1.0"，用户会以为滑块坏了。
          chip.type === 'kb' && chip.weight !== undefined && existing.weight === undefined
        ) {
          existing.weight = chip.weight
        }
      }
      contextChips.value = merged
    }
  } catch (e) {
    // 历史拉取失败时不要凭空判定"已结束"：保留该会话原有的运行态，
    // 否则一次网络抖动会把后台仍跑着的会话伪装成空闲。
    serverRunning = currentRun.value.loading
    const code = (e as { response?: { status?: number } })?.response?.status
    if (code === 404) {
      notify.info(t('errors.not_found'))
      sessions.value = sessions.value.filter(s => s.id !== id)
    } else if (code === 403) {
      notify.error(t('errors.forbidden'))
    }
  }
  if (!isStale(mySeq)) {
    // 用服务端的 run 归属对账：刷新/切回时若还有 run 在跑，必须重新挂流；
    // 已经结束则收掉陈旧流。**不再**无条件写 loading —— 那会把运行中的会话
    // 伪装成已结束（用户再按回车就是并发开第二轮）。
    if (serverRunning) {
      currentRun.value.loading = true
      if (!sseBySession.has(id)) attachSessionStream(id)
    } else {
      currentRun.value.loading = false
      closeSessionStream(id)
    }
    endSwitch(mySeq)
    // 会话加载完成后自动滚到底部：**走单写者**（契约 §1.1；此处原先直接写 DOM）
    await nextTick()
    messageListRef.value?.scrollToBottom()
  }
}

// ── 会话切换的历史回放 / 分页 ──
// 实现见 `composables/useSessionSwitch.ts`：分页游标、竞态守卫（`beginSwitch` / `isStale`）、
// 触顶翻页、`stream_gap` 后的整段重拉都在那里。`switchSession` 仍在本文件（它还要管审批卡、
// 流句柄、模型回填），只把"历史怎么回放"这一段交出去。
const {
  initialLoading,
  hasMore,
  loadingEarlier,
  earlierLoadFailed,
  beginSwitch,
  isStale,
  endSwitch,
  applyPageMeta,
  loadEarlier,
  scheduleGapResync,
  flushGapResync,
} = useSessionSwitch({
  sessionId: () => activeSessionId.value,
  items: () => items.value,
  setItems: (next) => { items.value = next },
  runOf: (sid: string) => runOf(sid),
  isActiveSession: (sid) => sid === activeSessionId.value,
  scrollToBottom: () => messageListRef.value?.scrollToBottom(),
})

async function deleteSession(id: string) {
  try { await api.delete(`/v1/conversations/${id}`) } catch { /* 保留本地删除 */ }
  sessions.value = sessions.value.filter(s => s.id !== id); persistSessions()
  // 会话没了，它的清单也不该留着：否则重建同名 id（或回退到同 id 的历史）时面板会凭空出现。
  todoPanel.clear(id)
  if (activeSessionId.value === id) {
    activeSessionId.value = ''; items.value = []
    if (sessions.value.length > 0) await switchSession(sessions.value[0].id)
  }
}

function requestDelete(id: string) {
  const s = sessions.value.find(x => x.id === id)
  Modal.confirm({
    title: t('chat.delete_conversation'),
    content: t('common.delete_title_this_cannot_be_undone', { title: s?.title || t('chat.new_conversation') }),
    okText: t('common.delete'),
    okButtonProps: { danger: true },
    cancelText: t('common.cancel'),
    onOk: () => deleteSession(id),
  })
}

// ── 重命名 / 置顶 / 标签 / 排序 ──
// 实现见 `composables/useSessionMeta.ts`（已在文件上方解构）。这里不再保留副本：
// 元数据操作只改 `sessions` 数组，与消息流无关，属于可独立成簇的边界。


// ── 分享（chat.deepseek.com/share/{id} 风格：选消息 → 生成链接 → 可取消） ──
// 状态与操作已抽到 `useChatShare`（L3-4 拆簇）；模板引用的名字不变。
const {
  shareOpen, shareTarget, shareInfo, shareLoading, shareRevoking, shareError,
  shareMessageIds, isGuest, shareCandidates, shareLinkUnavailable,
  openShare, toggleShareMessage, generateShare, revokeCurrentShare, shareUrl, copyShareLink,
} = useChatShare({ items, sessions, activeSessionId, switchSession })

// ── SSE 编排：事件 → ChatItem ──
// 流式缓冲：累加 assistant 原始文本后整体重算（让 chunk 中的 [thinking] 标签正确配对）。
//
// ⚠️ 这些字段**按会话**存在 `SessionRunState` 里（见上方 streamBuf 等字段的注释）：
// 多会话并行时，后台会话的事件仍会写进它自己的切片，不会污染当前视图的累积缓冲。
// 读取一律经 `streamState()`：SSE 回调整段跑在 `withRun(...)` 的同步作用域内，
// 因此它返回的正是"这条事件所属会话"的那一份。

// 流式正文/思考的累积与落条（[thinking] 拆分、独立 reasoning、收口）见
// `composables/useChatStreamText.ts`；这里只负责按事件类型调用。
const {
  genItemId,
  resetStreamState,
  appendUserText,
  onThinkingChunk,
  onTextChunk,
  flushStreamingFlags,
} = useChatStreamText({ items, streamState })

// SSE 事件分发见 `composables/useChatSse.ts`（P0-1 收尾）：依赖以显式端口注入，
// 事件分支不再散落在视图里；这里只负责把视图的 action / ref 接上。
const onSSEMessage = createSseDispatcher({
  items,
  streamState,
  genItemId,
  runOf,
  onTextChunk,
  onThinkingChunk,
  onToolCall: noteTurnWrite,
  flushStreamingFlags,
  applyTodos: (sid, todos, revision) => {
    todoPanel.applyEvent(sid, todos as never, revision as never)
  },
  mergeStats: mergeTurnStats,
  loading,
  stopTurnTimer,
  closeSessionStream,
  refreshInbox: () => { void interactionInboxRef.value?.refresh() },
  flushPendingInboxWake,
  clearPendingInboxWake,
  scheduleGapResync,
  flushGapResync,
  refreshSessionCost: () => { void refreshSessionCost() },
  interactionScope: {
    addApproval: ticket => interactionScope.addApproval(ticket as never),
    addQuestion: ticket => interactionScope.addQuestion(ticket as never),
  },
  ensureApprovalTimer,
  ensureAskTimer,
  approvalTimeoutMs: APPROVAL_TIMEOUT_MS,
  askTimeoutMs: ASK_TIMEOUT_MS,
  currentTraceId,
  turnElapsed,
  lastCompaction,
  budgetSnapshot,
  contextSnapshot,
  cacheSnapshot,
  verificationSnapshot,
  pushSubagentEvent,
  notify,
  offerSetupTour,
  actionableStreamError,
  isProviderAuthError,
  t,
})

/**
 * 首次向某个项目提问前，确认它已纳入版本控制。
 *
 * **为什么必须在这里**：还原点建立在 git 对象库上。项目目录不是 git 工作树时，
 * 引擎每一轮快照第一步就报 `fatal: not a git repository`，于是还原点全是
 * `state='failed'` —— 列表里看得见、点回滚必然失败。用户看到的正是
 * 「还原点功能从不正确工作」。等到他去翻还原点页才发现，保护已经空了整场对话。
 *
 * **为什么只问一次，且问过就不再问**：这是"要不要在你目录里建 .git"的授权。
 * 每次发消息都问会把对话变成问卷；而拒绝也不该被反复劝。记在 localStorage 里
 * （跨刷新有效），用户之后想开可以在还原点页随时点。
 *
 * **读不出状态就放行**：检测本身失败（网关重启、网络断）不该拦下用户的消息 ——
 * 还原点是辅助能力，为它挡住对话是更严重的损失。
 */
const VCS_PROMPT_KEY = 'chiron.vcs.prompted'

/**
 * localStorage 不可用时的兜底（隐私模式 / 存储被清）。
 *
 * 没有它，"不再主动打扰"这个承诺在其中一条路径上是空的：`localStorage` 抛异常时
 * 每次读都得到新的空集合，于是**每一条**消息都会再弹一次同一个确认框 ——
 * 那是问卷，不是询问。
 */
const vcsPromptedMemory = new Set<string>()

function vcsPrompted(): Set<string> {
  try {
    const raw = localStorage.getItem(VCS_PROMPT_KEY)
    const list = raw ? (JSON.parse(raw) as unknown) : []
    const stored = Array.isArray(list) ? list.filter((x): x is string => typeof x === 'string') : []
    return new Set([...stored, ...vcsPromptedMemory])
  } catch {
    // 隐私模式 / 损坏的旧值：退化成内存态（本次会话内不重复问）
    return new Set(vcsPromptedMemory)
  }
}

function markVcsPrompted(projectId: string) {
  // 内存态先写：即使下面落盘失败，本次会话内也不会再问第二遍。
  vcsPromptedMemory.add(projectId)
  try {
    localStorage.setItem(VCS_PROMPT_KEY, JSON.stringify([...vcsPrompted()]))
  } catch {
    // 落盘失败不影响本次判断：内存态已经兜住了。
  }
}

/** 并发的两次发送共享同一次询问，避免弹出两个一模一样的确认框 */
let vcsCheckInFlight: Promise<void> | null = null

function ensureProjectVcs(): Promise<void> {
  if (vcsCheckInFlight) return vcsCheckInFlight
  vcsCheckInFlight = checkProjectVcs().finally(() => {
    vcsCheckInFlight = null
  })
  return vcsCheckInFlight
}

async function checkProjectVcs(): Promise<void> {
  const projectId = activeProjectId.value
  if (!projectId) return
  if (vcsPrompted().has(projectId)) return
  let state
  try {
    state = await getProjectVcs(projectId)
  } catch {
    return
  }
  if (state.repo) return
  // 没装 git：装不装不是这里能解决的问题，问了也没用
  if (state.git_missing) {
    markVcsPrompted(projectId)
    return
  }
  // 先标记再问：点掉弹窗的那一刻就决定了"不再主动打扰"，
  // 与用户之后是否真去还原点页启用无关。
  markVcsPrompted(projectId)
  const agreed = await new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: t('chat.checkpoint_enable_vcs_title'),
      content: t('chat.checkpoint_vcs_ask_confirm'),
      okText: t('chat.checkpoint_enable_vcs_ok'),
      cancelText: t('chat.checkpoint_vcs_later'),
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
  if (!agreed) return
  try {
    const next = await initProjectVcs(projectId)
    if (next.repo) notify.success(t('chat.checkpoint_enable_vcs_done'))
    else notify.error(t('chat.checkpoint_enable_vcs_failed'))
  } catch (e) {
    // 启用失败**不拦消息**：用户的提问比还原点重要。
    notify.error(describeApiError(e))
  }
}

/** 发送一条消息。**返回值 = 是否已送达服务端** —— 未送达的会被暂存到本地队列
 *  （见 failedOutbox.ts），Composer 顶部横幅据此提供重发。
 *  既有调用方全部忽略返回值，所以把返回类型改成 boolean 不会影响它们。 */
async function sendMessage(
  text: string,
  attachments?: ChatAttachment[],
  opts?: { clientMsgId?: string },
): Promise<boolean> {
  // 首次提问前确认项目已纳入版本控制（见 checkProjectVcs）。**不 await**：
  // 它要先查一次网关（网关慢/重启时那是秒级），把它放在这里会让"点发送"到
  // "loading 亮起"之间空等一个往返 —— 用户看到的是"发出去没反应"。
  // 还原点是辅助能力，代价只能是"这一轮的 pre_turn 可能仍失败"，不能是对话被拖住。
  // 尾随 catch 是必须的：`void` 一个会 reject 的 Promise 等于留下一条未处理拒绝。
  void ensureProjectVcs().catch(() => {})
  // 互联互通：统一任务模式 → POST /v1/chat/submit（与 SSE 流式并列的新路径）
  if (unifiedMode.value && unifiedSessionId.value) {
    await sendUnified(text, attachments)
    // 统一模式有自己的错误呈现；这里乐观视为已受理，不入本地队列（避免两套重试叠加）
    return true
  }
  // ① 没有会话就**先建出来**（必须在 appendUserText 之前）：否则消息会落进"无会话"切片
  //    `__none__`，而 activeSessionId 直到提交成功才写回 ⇒ 一旦失败，这条消息与这个会话
  //    就都成了孤魂（侧栏里没有它、切走就再也回不来）。用户看到的现象正是"消息发出去就没了"。
  if (!activeSessionId.value) {
    if (!ensureModel()) return false
    await createSession()
    if (!activeSessionId.value) {
      notify.error(t('chat.failed_to_create_the_conversation_please_retry'))
      return false
    }
  } else if (!ensureModel()) {
    // ② 已有会话同样要确保有模型（空值 ⇒ 后端默认路由 ⇒ 401，见 ensureModel 的注释）
    return false
  }
  loading.value = true
  startTurnTimer()
  resetStreamState()
  verificationSnapshot.value = null
  connectionLost.value = false
  appendUserText(text, attachments)
  const userItemId = items.value[items.value.length - 1]?.id
  // 会话已在上面确保存在 ⇒ 这里**不再自己编 UUID**（编出来的 id 不属于任何侧栏会话）
  const sessionId = activeSessionId.value
  // H69：本轮的回合序号。gap 重拉提交前拿它做 CAS —— 即使新一轮**已经跑完**
  // （loading 回到 false），这个数也变了，旧快照不会把这一轮的产出盖掉。
  runOf(sessionId).turnSeq += 1
  // 新一轮开始：本回合改动从空开始（上一轮已审阅完，保留只会混淆）。
  turnWrittenPaths.value = []
  currentTraceId.value = ''
  try {
    // H59/H60：与断线重连共用**同一套**装配（attachSessionStream），否则两处
    // 各写一份 onError，必然漂移 —— 一处改来源会话、另一处改当前视图。
    attachSessionStream(sessionId, { userItemId })
    // 重发（outbox）复用**同一条** client_msg_id：服务端 SETNX 才能把
    // "上次其实已受理、只是客户端超时"识别为同一条，而不是执行两次。
    const clientMsgId = opts?.clientMsgId || crypto.randomUUID()
    const body: Record<string, unknown> = {
      content: text,
      session_id: sessionId,
      // 发送侧幂等：同一条消息最多被引擎执行一次（服务端用 Redis SETNX 做 5 分钟去重）。
      // 必须由**客户端**生成 —— 只有客户端知道"这两次提交是同一条消息"：
      // 网络重试或用户重发时这个 ID 不变，服务端据此判定"已在处理"而不再重复触发引擎。
      llm_config: { ...buildLlmConfig(), client_msg_id: clientMsgId },
    }
    // H58：记下"这一轮"的现场。停止必须打到这个会话，而不是用户按下停止时
    // 恰好停在的那个会话（切会话不该改变取消的目标）。
    runOf(sessionId).turn = { sessionId, clientMsgId }
    runOf(sessionId).stop = undefined
    const ctx = buildContext()
    if (ctx) body.context = ctx
    const resolvedAtts = await resolveAttachmentUrls(attachments)
    if (resolvedAtts.length) {
      body.attachments = resolvedAtts.map(a => ({ id: a.id, name: a.name, mime_type: a.mimeType, url: a.url, is_image: a.isImage }))
    }
    await api.post('/submit', body, { __retainAcrossNavigation: true } as never)
    // 新用户引导第三步：消息真的被服务端接收了才算完成（P0-3）。
    // 放在这之后而不是之前：POST 失败会走 catch，那时"发出第一轮对话"并没有发生。
    void markFirstMessageDone()
    return true
  } catch (e) {
    closeSessionStream(sessionId)
    loading.value = false
    stopTurnTimer()
    flushStreamingFlags()
    // 这一轮根本没送达：清掉现场，别让"停止"打到一个不存在的 run 上。
    runOf(sessionId).turn = undefined
    runOf(sessionId).stop = undefined
    const reason = describeApiError(e)
    // meta 必须一起带上：少了 code/retry_after_seconds，错误块就退化成"只有一句重试"，
    // 余额不足拿不到"去充值"、限流拿不到"等 N 秒"—— 正是 P0-2 要修的那条裂缝。
    markMessageFailed(userItemId, reason, describeFailureMeta(e))
    notify.error(t('errors.send_failed') + reason)
    // 落盘暂存：这条消息**没有**被服务端接收（POST 失败），刷新后 transcript 里不会有它。
    // 不回填输入框而是入队，是为了不打断"继续写草稿"的动作。
    queueFailedSend(sessionId, text, attachments, opts?.clientMsgId)
    return false
  }
}

/** 重发 / 丢弃暂存队列：见 `composables/useComposerBridge.ts`（已在上方解构）。
 *  重发逐条走**正常发送路径**，成功一条就从本地删一条；失败的只累加 retryCount。 */

// ── 失败消息标记 / 手动修复 ──
// 实现见 `composables/useMessageFailure.ts`（已在文件上方解构）。这三个函数只写那条
// 消息自身的错误字段与收口卡住的会话，不碰流状态，因此可以独立成簇。


// ── 消息重试/重新生成 ──
/** 删除指定 itemId 及其后所有消息，返回被删除的用户消息文本（如有） */
function truncateFrom(itemId: string): { text?: string; attachments?: ChatAttachment[] } {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return {}
  const removed = items.value.slice(idx)
  items.value = items.value.slice(0, idx)
  const userMsg = removed.find((i): i is TextItem => i.kind === 'text' && i.role === 'user')
  return userMsg ? { text: userMsg.content, attachments: userMsg.attachments } : {}
}

/** 指定消息之后还有多少条（不含自身）：重发会连带删除，先让用户知道代价 */
function messagesAfter(itemId: string): number {
  return countItemsAfter(items.value, itemId)
}

/**
 * 删除类操作前的确认。
 *
 * `truncateFrom` 是**不可逆**的（后续消息直接从前端状态里消失，后端也没有回滚接口），
 * 而「重发 / 重新生成」是消息操作栏里的高频按钮 —— 误点一次就丢掉整段后续内容。
 * 代价为 0 时（消息已在末尾）不打扰。
 */
function confirmDestructive(removeCount: number, action: string, run: () => void) {
  if (removeCount <= 0) {
    run()
    return
  }
  Modal.confirm({
    title: t('chat.this_will_delete_the_following_n_messages', { n: removeCount }),
    content: t('chat.action_will_truncate_at_this_message_the_following_n_messages_including_assistant_replies_will_be_deleted_and_cannot_be_recovered', { action, n: removeCount }),
    okText: t('common.delete_and_continue'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: () => { run() },
  })
}

/** 用户消息编辑后重发：删除该消息及之后所有，用新文本重发 */
function retryFromUserMessage(itemId: string, newText: string) {
  confirmDestructive(messagesAfter(itemId), t('common.resend'), () => {
    truncateFrom(itemId)
    sendMessage(newText)
  })
}

/** 助手消息重新生成：删除该消息及之后所有，取上一条用户消息重发 */
function regenerateAssistant(itemId: string) {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return
  let userMsg: TextItem | null = null
  for (let i = idx - 1; i >= 0; i--) {
    const it = items.value[i]
    if (it.kind === 'text' && it.role === 'user') { userMsg = it; break }
  }
  if (!userMsg) {
    notify.warning(t('errors.no_matching_user_message_found_cannot_regenerate'))
    return
  }
  confirmDestructive(messagesAfter(itemId), t('common.regenerate'), () => {
    truncateFrom(itemId)
    sendMessage(userMsg.content, userMsg.attachments)
  })
}

/** 失败消息重试：清除错误状态，用原文本重发 */
function retryFailedMessage(itemId: string) {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return
  const it = items.value[idx]
  if (it.kind !== 'text') return
  const text = it.content
  const attachments = it.attachments
  confirmDestructive(messagesAfter(itemId), t('common.retry'), () => {
    truncateFrom(itemId)
    sendMessage(text, attachments)
  })
}

/**
 * 运行中补充指令（P0-3）：把文本注入当前正在跑的回合，**不打断**。
 *
 * 三处刻意的选择：
 * 1. **成功不插行**：引擎会在轮次边界发一条 SSE `steer` 事件，由 `onSSEMessage` 统一落
 *    notice（正文已作为 user 消息进历史）。这里再插一条就是重复。
 * 2. **失败把原文还回输入框**：这条通道的拒绝多是临时的 —— 429 限流、409 那一轮刚好在收尾
 *    （`steerStatus` 把引擎的 `ok:false` 翻成了状态码）。用户多半想重试一次；
 *    当成普通发送清空就等于让他重打一遍。
 * 3. **失败用 notify 而不是消息流**：它说的是一次**投递**的结果，不属于对话内容。
 */
async function onSteer(text: string): Promise<void> {
  const sid = activeSessionId.value
  if (!sid || !text.trim()) return
  try {
    const ok = await steerRun({ session_id: sid, text: text.trim() })
    if (!ok) {
      chatInputRef.value?.setDraft?.(text)
      notify.warning(t('chat.steer_failed', { error: t('errors.request_failed') }))
    }
  } catch (e) {
    const reason = describeApiError(e)
    chatInputRef.value?.setDraft?.(text)
    notify.warning(t('chat.steer_failed', { error: reason }))
  }
}

function stopGeneration() {
  return requestTurnStop({
    activeSessionId: () => activeSessionId.value,
    turnOf: (sid) => runOf(sid).turn,
    stopOf: (sid) => runOf(sid).stop,
    setStop: (sid, stop) => { runOf(sid).stop = stop },
    clearTurn: (sid) => { runOf(sid).turn = undefined },
    // H58：取消真正打到后端（网关按 session 从 run 亲和路由里补 run_token，
    // 见 internal/api/submit_handler.go::SubmitInterrupt）。返回 true = 服务端确认。
    interrupt: (sessionId) => interruptAgentRun(sessionId),
    onStopped: (sid) => {
      const run = runOf(sid)
      stopTurnTimer()
      // 只关**这个会话**的流：用户可能已经切到别的会话上（那边不该被牵连）。
      // H59 起每个会话各持一条，所以这里按 sid 精确关闭，不再需要"等于当前视图"这层判断。
      closeSessionStream(sid)
      run.loading = false
      withRun(run, () => flushStreamingFlags())
      // 用户喊停是明确说"别再跑了"：这里**不能**补发待办唤醒，否则按下停止后
      // 又会自动发一条「请继续」，看起来像按钮失灵。
      run.pendingInboxWake = false
      const last = run.items[run.items.length - 1]
      if (last && last.kind === 'text' && last.role === 'assistant') {
        last.stopped = true
      }
    },
    // 未确认：保持 loading / 不给消息打 stopped —— run 可能还在跑。
    // 用户再点一次停止就是重试（`failed` 状态允许重入）。
    onFailed: () => {
      notify.warning(t('errors.request_failed'))
    },
  })
}

// 继续生成（停止后）
function continueGeneration() {
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'text' && last.role === 'assistant' && last.stopped) {
    regenerateAssistant(last.id!)
  }
}
</script>

<template>
  <div
    class="chat-layout"
    :class="{ 'is-swapped': layoutSwapped }"
  >
    <!-- 分屏 6a：**只读参考栏**（左侧）。
         布局本身是 flex，所以加一栏不需要改任何 CSS；它自己滚动，
         不会把主会话的滚动位置带跑（与主列表的滚动锚定互不干扰）。 -->
    <!-- 工作台（项目文件树 + 只读预览）。默认在最左：它看的是"这个项目里有什么"，
         是比会话内容更外围的信息，放在主会话右侧会与侧面板抢注意力。
         与分屏参考栏可以同时开 —— 两者宽度上限各取容器一半。

         交换布局（侧边栏进左）时它**必须跟着移到最右**：否则左侧同时挤着
         侧边栏与工作台，而 `is-swapped` 本来就是为了把主会话还给中间。
         移动只改 `order` 与 `handle-side` —— DOM 位置不动，手柄仍贴着真正的窗口边缘。 -->
    <!-- 折叠/展开 200ms 过渡：只动 opacity + transform（合成层属性），
         不动 width/flex-basis —— 后者每一帧都要重排整条 flex 行，长会话的
         transcript 测量账本会被反复触发（见 transcriptMeasurementLedger）。
         因此这里刻意用位移+淡入代替"宽度伸缩"。 -->
    <Transition name="panel-slide">
      <WorkbenchPanel
        v-if="workbenchOpen"
        id="workbench-panel"
        ref="workbenchRef"
        :project-id="activeProjectId"
        :project-name="activeProject?.name || ''"
        :project-error="activeProjectError"
        :agent-written-paths="agentWrittenPaths"
        :turn-written-paths="turnWrittenPaths"
        :verification="verificationSnapshot"
        :handle-side="layoutSwapped ? 'left' : 'right'"
        @close="toggleWorkbench()"
        @reference="onWorkbenchReference"
        @fix-request="onWorkbenchFixRequest"
        @retry-project="refreshActiveProject"
      />
    </Transition>
    <ChatReferenceDock
      v-if="splitSessionId || subagentDockOpen"
      v-model:active="rightDockTab"
      :reference-open="!!splitSessionId"
      :subagent-open="subagentDockOpen"
      :session-id="activeSessionId"
      :reference-session-id="splitSessionId"
      :reference-title="splitTitle"
      :sessions="sessions"
      :exclude-session-id="activeSessionId"
      :handle-side="layoutSwapped ? 'left' : 'right'"
      :live-events="subagentLiveEvents"
      :focus-run-id="subagentFocusRequest?.runId || ''"
      :focus-seq="subagentFocusRequest?.seq || 0"
      @update:session-id="splitSessionId = $event"
      @close-reference="splitSessionId = ''"
      @close-subagent="setSubagentDockOpen(false)"
      @retry-run="onSubagentRetry"
    />
    <div
      class="chat-main"
      :inert="panelOpen && narrowSidePanel"
    >
      <!-- N7 v1：项目标签 + 当前会话身份，Teleport 到标题栏左插槽（停靠坞右侧）。
           只换 DOM 落点：每个标签记住的活跃会话 / 工作台开合、键盘左右切换、
           关闭与"＋"选项目仍由本组件持有，逻辑未动。
           激活标签把会话名合并进标签文本（`项目 · 对话`），标签宽度上限不变，
           超长由 MarqueeText 滚动播放。 -->
      <Teleport
        :to="topbarSlots?.left?.value || 'body'"
        :disabled="!topbarSlots?.left?.value"
      >
        <div class="topbar-cluster">
          <div
            v-if="openProjectTabs.length"
            ref="projectTabsRef"
            class="project-tabs"
            role="tablist"
            :aria-label="$t('chat.project_tabs')"
          >
            <div
              v-for="tab in openProjectTabs"
              :key="tab.id"
              class="project-tab"
              :class="{ active: projectWorkspace.active === tab.id }"
              role="tab"
              :data-project-id="tab.id"
              :aria-selected="projectWorkspace.active === tab.id"
              :title="tab.label"
              @click="switchProjectTab(tab.id)"
              @keydown.enter="switchProjectTab(tab.id)"
              @keydown.left.prevent="focusAdjacentProjectTab(-1)"
              @keydown.right.prevent="focusAdjacentProjectTab(1)"
              :tabindex="projectWorkspace.active === tab.id ? 0 : -1"
            >
              <MarqueeText
                class="project-tab-name"
                :text="tab.label"
              />
              <button
                type="button"
                class="project-tab-close"
                :aria-label="$t('chat.close_project_tab')"
                @click.stop="closeProjectTabAndMaybeSwitch(tab.id)"
              >×</button>
            </div>
            <Dropdown
              trigger="click"
              placement="bottomLeft"
              @open-change="onProjectPickerOpenChange"
            >
              <button
                type="button"
                class="project-tab-add"
                :aria-label="$t('chat.open_project_tab')"
                :title="$t('chat.open_project_tab')"
              >
                ＋
              </button>
              <template #overlay>
                <Menu @click="onProjectPickerClick">
                  <MenuItem
                    v-for="p in projectPickerOptions"
                    :key="p.id"
                  >
                    {{ p.name || p.root_path }}
                  </MenuItem>
                  <MenuItem
                    v-if="!projectPickerOptions.length"
                    key="__empty"
                    disabled
                  >
                    {{ $t('chat.no_project_to_open') }}
                  </MenuItem>
                </Menu>
              </template>
            </Dropdown>
          </div>
          <!-- 会话不属于任何项目时没有标签可合并 —— 至少让对话名在标题栏可见，
               否则新会话（未归入项目）会连"我在哪个对话"都看不出来。 -->
          <span
            v-else
            class="project-tab-solo"
            :title="convTitle"
          >
            <MarqueeText :text="convTitle" />
          </span>
        </div>
      </Teleport>
      <div
        v-if="connectionLost"
        class="connection-banner"
      >
        {{ isOnline ? $t('common.disconnected_from_server_attempting_to_reconnect') : $t('common.network_disconnected_please_check_your_connection') }}
      </div>
      <div class="chat-body">
        <!-- 会话 / 轨迹 / 地图 / 搜索 + 分屏 / 项目文件，Teleport 到标题栏右插槽
             （窗口三键之前）。始终纯图标：标题栏只有 36px，文字标签会把项目标签与
             会话名挤没；可发现性由 title tooltip 与 aria-label 承担。 -->
        <Teleport
          :to="topbarSlots?.right?.value || 'body'"
          :disabled="!topbarSlots?.right?.value"
        >
          <div
            class="topbar-chat-actions"
            role="toolbar"
            :aria-label="$t('chat.session_actions')"
          >
            <!-- M21：跨会话待办角标 —— 任何页面都能看到"我还欠哪些答复"，点一下跳过去。
                 M7 的收件箱只在打开那个会话时可见，这条补的正是"不知道哪个会话欠答复"。 -->
            <InteractionBadge
              :live-ids="livePendingIds"
              @select="switchSession"
            />
            <!-- 分屏 6a：并排参考另一个会话（只读）。与"项目文件"同属视图开关，
                 所以排在会话导航之前。 -->
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: !!splitSessionId }"
              :title="$t('chat.side_by_side_reference_to_another_session_read_only')"
              :aria-label="splitSessionId ? $t('common.collapse_reference') : $t('common.split_view')"
              :aria-pressed="!!splitSessionId"
              @click="toggleSplit()"
            >
              <template #icon>
                <SplitCellsOutlined />
              </template>
            </Button>
            <!-- 工作台（项目文件）：`aria-expanded` + `aria-controls` —— 折叠态由
                 CSS/Transition 表达，读屏只能从这里知道"现在是开还是关"。 -->
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: workbenchOpen }"
              :title="$t('chat.workbench_title')"
              :aria-label="$t('chat.workbench_title')"
              :aria-expanded="workbenchOpen"
              aria-controls="workbench-panel"
              @click="toggleWorkbench()"
            >
              <template #icon>
                <FolderOpenOutlined />
              </template>
            </Button>
            <span
              class="topbar-actions-divider"
              aria-hidden="true"
            />
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: panelOpen && panelView === 'sessions' }"
              :title="panelOpen && panelView === 'sessions' ? $t('chat.collapse_session_list') : $t('chat.conversation_history')"
              :aria-label="panelOpen && panelView === 'sessions' ? $t('chat.collapse_session_list') : $t('chat.conversation_history')"
              :aria-expanded="panelOpen && panelView === 'sessions'"
              aria-controls="chat-side-panel"
              @click="openPanel('sessions')"
            >
              <template #icon>
                <MenuOutlined />
              </template>
            </Button>
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: panelOpen && panelView === 'trajectory' }"
              :title="panelOpen && panelView === 'trajectory' ? $t('common.collapse_trace') : $t('common.view_past_questions')"
              :aria-label="panelOpen && panelView === 'trajectory' ? $t('common.collapse_trace') : $t('common.view_past_questions')"
              :aria-expanded="panelOpen && panelView === 'trajectory'"
              aria-controls="chat-side-panel"
              @click="openPanel('trajectory')"
            >
              <template #icon>
                <HistoryOutlined />
              </template>
            </Button>
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: synapseMapOpen }"
              :title="$t('chat.open_session_map')"
              :aria-label="$t('chat.open_session_map')"
              @click="synapseMapOpen = true"
            >
              <template #icon>
                <PartitionOutlined />
              </template>
            </Button>
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: searchOpen }"
              :title="searchOpen ? $t('common.close_search') : $t('chat.search_in_this_session_text_and_reasoning')"
              :aria-label="searchOpen ? $t('common.close_search') : $t('chat.search_in_this_session_text_and_reasoning')"
              @click="searchOpen ? closeSearch() : openSearch()"
            >
              <template #icon>
                <SearchOutlined />
              </template>
            </Button>
            <!-- 用 #overlay + <Menu>，与 ChatSidePanel 里那个**确实可用**的会话菜单保持一致。
                 此前这里用 :menu="{ items, onClick }"，表现是**点击完全不弹菜单**：
                 Console 无报错、命中的确实是按钮本身 ⇒ 既不是遮挡也不是渲染异常。
                 对齐到已验证可用的写法（trigger 用字符串、Button 加 @click.stop、菜单走 #overlay）。 -->
            <Dropdown
              trigger="click"
              placement="bottomRight"
            >
              <Button
                type="text"
                size="small"
                class="toolbar-btn"
                :title="$t('common.more_actions')"
                :aria-label="$t('common.more_actions')"
                @click.stop
              >
                <template #icon>
                  <MoreOutlined />
                </template>
              </Button>
              <template #overlay>
                <Menu class="toolbar-menu">
                  <template
                    v-for="it in (toolbarMenuItems as any[])"
                    :key="String(it.key)"
                  >
                    <MenuDivider v-if="it.type === 'divider'" />
                    <MenuItem
                      v-else
                      :disabled="it.disabled"
                      @click="onToolbarMenu({ key: String(it.key) })"
                    >
                      <component :is="it.icon" />
                      <span class="menu-label">{{ it.label }}</span>
                    </MenuItem>
                  </template>
                </Menu>
              </template>
            </Dropdown>
          </div>
        </Teleport>

        <!-- 会话内检索条：Enter/↓ 下一个、Shift+Enter/↑ 上一个、Esc 关闭 -->
        <div
          v-if="searchOpen"
          class="chat-search"
        >
          <SearchOutlined class="chat-search-icon" />
          <input
            ref="searchInputRef"
            v-model="searchQuery"
            class="chat-search-input"
            type="text"
            :placeholder="$t('chat.search_body_and_reasoning_in_this_session_enter_for_next_esc_to_close')"
            :aria-label="$t('chat.search_body_and_reasoning_in_this_session_enter_for_next_esc_to_close')"
            @keydown.enter.exact.prevent="gotoMatch(1)"
            @keydown.enter.shift.prevent="gotoMatch(-1)"
            @keydown.esc.prevent="closeSearch"
          >
          <span class="chat-search-count">
            {{ searchQuery.trim() ? (searchMatches.length ? `${searchCursor + 1} / ${searchMatches.length}` : $t('common.no_match')) : '' }}
          </span>
          <div class="chat-search-actions">
            <button
              class="chat-search-btn btn-xs"
              type="button"
              :title="$t('common.previous')"
              :disabled="!searchMatches.length"
              @click="gotoMatch(-1)"
            >
              ↑
            </button>
            <button
              class="chat-search-btn btn-xs"
              type="button"
              :title="$t('common.next')"
              :disabled="!searchMatches.length"
              @click="gotoMatch(1)"
            >
              ↓
            </button>
            <button
              class="chat-search-btn btn-xs"
              type="button"
              :title="$t('common.close')"
              @click="closeSearch"
            >
              ✕
            </button>
          </div>
        </div>

        <!-- 互联互通：错误提示条（query.error） -->
        <div
          v-if="errorBanner"
          class="unified-error-banner"
        >
          <span class="ueb-text">{{ errorBanner }}</span>
          <CloseOutlined
            class="ueb-close"
            :title="$t('common.close')"
            @click="errorBanner = ''"
          />
        </div>

        <!-- 互联互通：统一任务模式 -->
        <template v-if="unifiedMode">
          <div class="unified-bar">
            <span
              class="ub-badge"
              :class="{ running: loading, done: unifiedJustFinished }"
            >
              {{ loading ? $t('workflow.orchestrating') : unifiedJustFinished ? $t('common.done_3') : $t('workflow.unified_task') }}
            </span>
            <span class="ub-mode">{{ unifiedSubmitMode || 'auto' }}</span>
            <span class="ub-spacer" />
            <button
              type="button"
              class="ub-btn btn-xs"
              :disabled="!items.length"
              @click="clearUnifiedMessages"
            >
              {{ $t('common.clear_2') }}
            </button>
            <button
              type="button"
              class="ub-btn exit"
              :title="$t('auth.exit_unified_task_mode')"
              @click="exitUnifiedMode"
            >
              {{ $t('auth.exit') }}
            </button>
          </div>
          <div
            v-if="loading"
            class="unified-exec-hint"
          >
            <span class="ueh-dot" />{{ $t('workflow.orchestrating_executing_subtasks') }}<template v-if="turnElapsed >= 2">
              &nbsp;·&nbsp;{{ turnElapsed }}s
            </template>
          </div>
          <div
            v-if="!items.length && !loading"
            class="unified-empty"
          >
            {{ $t('workflow.the_unified_task_session_is_ready_just_send_a_message_to_continue_asking') }}
          </div>
          <div class="unified-list">
            <template
              v-for="(it, i) in items"
              :key="it.id ?? i"
            >
              <MessageItem
                v-if="it.kind === 'text' || it.kind === 'reasoning'"
                :item="it"
                @retry-from="retryFromUserMessage"
                @regenerate="regenerateAssistant"
                @continue="continueGeneration"
                @retry-failed="retryFailedMessage"
                @repair-session="repairSession"
              />
              <div
                v-else-if="(it as any).kind === 'kb_hits'"
                class="kb-hits-tag"
              >
                <span class="kb-hits-text">{{ $t('knowledge.referenced_knowledge_base_n', { n: (it as any).count || 1 }) }}</span>
                <a
                  v-if="(it as any).kb_id"
                  class="kb-hits-link"
                  href="#"
                  :title="$t('knowledge.view_referenced_knowledge_base')"
                  @click.prevent="openKb((it as any).kb_id)"
                >{{ $t('knowledge.view_knowledge_base') }}</a>
              </div>
            </template>
          </div>
          <CallChainTimeline
            v-if="currentTraceId && !loading"
            :trace-id="currentTraceId"
            :tenant-id="authStore.user?.tenant_id || ''"
          />
        </template>

        <!-- 原有 SSE 流式模式 -->
        <ChatEmptyHero
          v-else-if="items.length === 0 && !loading"
          @suggest="sendMessage"
        />
        <template v-else>
          <div
            v-if="loading"
            class="turn-status"
          >
            {{ $t('chat.reasoning_2') }}<template v-if="turnElapsed >= 2">
              &nbsp;·&nbsp;<span class="tnum-fixed">{{ turnElapsed }}s</span>
            </template>
          </div>
          <MessageList
            ref="messageListRef"
            :items="items"
            :loading="loading"
            :initial-loading="initialLoading"
            :focus-index="trajectoryFocus"
            :focus-token="trajectoryToken"
            :has-more="hasMore"
            :loading-earlier="loadingEarlier"
            :earlier-load-failed="earlierLoadFailed"
            :session-key="activeSessionId"
            :project-id="activeProjectId"
            can-open-subagent
            @load-earlier="loadEarlier"
            @quote-text="onQuoteText"
            @retry-from="retryFromUserMessage"
            @regenerate="regenerateAssistant"
            @continue="continueGeneration"
            @retry-failed="retryFailedMessage"
            @open-subagent="openSubagentDock"
          />
          <CallChainTimeline
            v-if="currentTraceId && !loading"
            :trace-id="currentTraceId"
            :tenant-id="authStore.user?.tenant_id || ''"
          />
        </template>
      </div>

      <!-- 工具授权模式（ask/auto/yolo）：已移至对话框底部（见 ChatInput 之后的一行下拉）——
           它控制的是"工具执行是否需要确认"，与「对话模式」是两个维度，
           但占用工具栏一整行会让视觉权重过高、且离输入区太远。 -->

      <ChatAttentionTray
        :approvals="pendingApprovals"
        :questions="pendingQuestions"
        :remaining-of="approvalRemain"
        :edit-of="approvalEdit"
        @approve="(a) => resolveApproval(a, true)"
        @reject="(a) => resolveApproval(a, false)"
        @toggle-edit="toggleApprovalEdit"
        @edit-input="onApprovalEditInput"
        @submit-edit="submitApprovalEdit"
        @answer="answerQuestion"
        @decline="declineQuestion"
        @drafting="(q, active) => { q.drafting = active }"
      />

      <!-- 人工介入待办：只在有跨进程遗留的待答项时出现（列表为空即整块不渲染）。
           答复成功即发 `wake`，由 wakeFromInbox 顺手提交一轮让引擎从暂停处接手。 -->
      <InteractionInbox
        ref="interactionInboxRef"
        :session-id="activeSessionId"
        :live-tickets="liveInteractionTickets"
        @wake="wakeFromInbox"
      />

      <!-- 任务清单：长任务的锚点。挂在输入区正上方（不塞进消息流），
           这样无论滚到哪里都看得见"还剩几项、卡在哪"。
           数据来自 useTodoPanel（按会话独立持有），**不读 items**。 -->
      <TodoPanel
        v-if="todoBoard.rows.length"
        class="chat-todo-panel"
        :board="todoBoard"
        :collapsed="todoPanel.collapsed.value"
        @toggle="todoPanel.toggleCollapsed()"
      />

      <ChatInput
        ref="chatInputRef"
        :loading="loading"
        :mode="mode"
        :mode-options="modeOptions"
        :model="llmModel"
        :persona="personaId"
        :persona-options="personaOptions"
        :session-id="unifiedMode ? unifiedSessionId : activeSessionId"
        :tools-mode="toolsMode"
        :output-style="outputStyle"
        :language="language"
        :reasoning-language="reasoningLanguage"
        :effort="effort"
        :context-chips="contextChips"
        :project-id="activeProjectId"
        @send="sendMessage"
        @stop="stopGeneration"
        @steer="onSteer"
        @update:mode="onModeChange"
        @model-change="onModelChange"
        @persona-change="onPersonaChange"
        @command="onSlashCommand"
        @open-panel="openContextPanel"
        @mention-add="onMentionAdd"
        @tools-mode-change="onToolsModeChange"
        @update:outputStyle="onOutputStyleChange"
        @update:language="onLanguageChange"
        @update:reasoningLanguage="onReasoningLanguageChange"
        @update:effort="onEffortChange"
        @remove-context-chip="(c: ContextChip) => removeContextChip(c.type, c.value)"
        :failed-queue-count="outbox.length"
        :outbox-items="outboxPreview"
        @retry-outbox="retryOutbox"
        @discard-outbox="discardOutbox"
        @retrieve-outbox="retrieveOutbox"
      />

      <ChatStatusBar
        :model="llmModel"
        :stats="lastTurnStats"
        :context-used="contextSnapshot?.used ?? lastTurnStats?.inputTokens ?? null"
        :context-limit="contextWindow"
        :compact-ratio="contextSnapshot?.compact_ratio ?? null"
        :context-estimated="contextSnapshot?.estimated ?? false"
        :breakdown="contextSnapshot?.breakdown ?? null"
        :largest="contextSnapshot?.largest ?? null"
        :cache="cacheSnapshot"
        :compaction="lastCompaction"
        :budget="budgetSnapshot"
        :cost-cents="sessionCostCents"
        :subagent-active-count="subagentActiveCount"
        :online="isOnline && !connectionLost && componentsUp"
        @toggle-subagents="toggleSubagentDock()"
        @toggle-stats="statsOpen = !statsOpen"
        @toggle-health="healthOpen = !healthOpen"
      />

      <!-- 观测浮层：
           向上弹出 + 右对齐；限高到输入区顶部，因此**不遮挡正在写的草稿**。
           子 Agent 已改为布局内的对照栏（见 `.chat-layout` 里的 `SubAgentPane`），
           不再占用浮层。 -->
      <FloatingPanel
        :open="statsOpen"
        :title="$t('chat.session_stats')"
        :width="320"
        :bottom-inset="floatingBottomInset"
        @close="statsOpen = false"
      >
        <SessionStatsPanel :session-id="activeSessionId" />
      </FloatingPanel>

      <!-- 组件健康 / 内存明细：点状态栏的「已连接」展开。
           回答"到底哪个组件挂了、内存去哪了"，而状态栏 26px 只放得下结论。 -->
      <FloatingPanel
        :open="healthOpen"
        :title="$t('common.component_health_and_memory')"
        :width="380"
        :bottom-inset="floatingBottomInset"
        @close="healthOpen = false"
      >
        <ChatHealthPanel :components="componentsHealth" />
      </FloatingPanel>

      <ChatDisplaySettings v-model:open="displaySettingsOpen" />
      <SaveToKnowledgeDialog
        v-model:open="saveToKbOpen"
        :content="sessionMarkdownForDialog"
        :default-title="activeSession?.title || $t('chat.share.defaultTitle')"
      />
      <ChatSetupTour
        ref="tourRef"
        @go-configure="goConfigureModel"
      />
      <!-- 新用户引导（P0-3）：注册后首次进入的三步清单；存量账号后端返回 eligible=false，不会弹 -->
      <OnboardingGuide
        v-model:open="onboardingOpen"
        :status="onboardingStatus"
        @skip-all="skipOnboarding"
        @focus="focusOnboardingTarget"
      />
      <SaveToMemoryDialog
        v-model:open="saveToMemoryOpen"
        :content="sessionMarkdownForDialog"
        :default-key="activeSession?.title || ''"
      />

      <!-- 存为 Agent：名字由用户确认（会话正文作为人格 system_prompt） -->
      <Modal
        :open="saveAgentOpen"
        :title="$t('agent.save_as_agent')"
        :confirm-loading="savingAgent"
        :ok-text="$t('common.create')"
        :cancel-text="$t('common.cancel')"
        @ok="saveAsAgent"
        @cancel="saveAgentOpen = false"
      >
        <div class="save-agent-form">
          <label class="save-agent-label">{{ $t('common.name') }}</label>
          <Input
            v-model:value="agentDraft.name"
            :maxlength="60"
            :placeholder="$t('common.e_g_tech_review')"
          />
          <label class="save-agent-label">{{ $t('common.description') }}</label>
          <Input
            v-model:value="agentDraft.description"
            :maxlength="200"
            :placeholder="$t('common.describe_the_responsibility_in_one_sentence_optional')"
          />
          <p class="save-agent-hint">
            {{ $t('chat.agent.createHint', { count: agentDraft.system_prompt.length }) }}
          </p>
        </div>
      </Modal>

    </div>

    <!-- 侧面板 -->
    <Transition name="overlay-fade">
      <div
        v-if="panelOpen"
        class="panel-overlay"
        aria-hidden="true"
        @click="panelOpen = false"
      />
    </Transition>
    <ChatSidePanel
      id="chat-side-panel"
      :open="panelOpen"
      :view="panelView"
      :items="items"
      :selected-index="trajectoryFocus"
      :sessions="sessions"
      :active-session-id="activeSessionId"
      :context-chips="contextChips"
      :running-session-ids="sidebarRunningIds"
      @update:view="(v: 'trajectory' | 'sessions') => (panelView = v)"
      @open-map="synapseMapOpen = true"
      @focus="onTrajectoryFocus"
      @close="panelOpen = false"
      @create="createSession"
      @switch="switchSession"
      @delete="requestDelete"
      @rename="openRename"
      @pin="togglePin"
      @share="openShare"
      @tag="setSessionTag"
      @remove-context="removeContextChip"
      @kb-weight="setKbWeight"
      @clear-context="clearContext"
      @binding-changed="refreshActiveProject"
    />

    <!-- 会话地图：同一 SPA 内的一个视图（不再是 public/sessionmap/ 的 iframe）。
         会话的主人始终是这里 —— 地图通过 emit 请求切换/刷新，不自己维护第二份列表。 -->
    <div
      v-if="synapseMapOpen"
      class="session-map-overlay"
    >
      <SessionMapView
        :sessions="sessions"
        :active-session-id="activeSessionId"
        :running-session-ids="mapRunningSessionIds"
        :stream-previews="mapStreamPreviews"
        :build-llm-config="buildLlmConfig"
        @close="synapseMapOpen = false"
        @switch-session="onMapSwitchSession"
        @refresh-sessions="loadSessions"
      />
    </div>

    <!-- 快捷键速查表（? 打开） -->
    <ShortcutsHelpModal
      :open="shortcutsHelpOpen"
      @update:open="shortcutsHelpOpen = $event"
    />

    <!-- 重命名对话框 -->
    <Modal
      :open="!!renameTarget"
      :title="$t('chat.rename_conversation')"
      :confirm-loading="renaming"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="confirmRename"
      @cancel="renameTarget = null"
    >
      <Input
        v-model:value="renameDraft"
        :placeholder="$t('chat.enter_a_new_conversation_name')"
        :maxlength="120"
        @press-enter="confirmRename"
      />
    </Modal>

    <!-- 分享对话框 -->
    <Modal
      :open="shareOpen"
      :title="$t('chat.share.title', { name: shareTarget?.title || $t('chat.share.newSession') })"
      :footer="null"
      width="520px"
      @cancel="shareOpen = false"
    >
      <Alert
        type="warning"
        show-icon
        class="share-risk"
        :message="$t('chat.share.visibilityHint')"
        :description="$t('errors.do_not_share_content_containing_sensitive_or_private_information_you_can_cancel_sharing_at_any_time_the_link_becomes_invalid_immediately')"
      />

      <template v-if="isGuest">
        <div class="share-guest-tip">
          {{ $t('auth.sign_in_to_generate_a_share_link') }}
        </div>
      </template>

      <template v-else-if="shareInfo">
        <div class="share-link-row">
          <Input
            :model-value="shareUrl()"
            :placeholder="shareLinkUnavailable ? $t('errors.share_link_unavailable_please_configure_public_base_url') : ''"
            readonly
            class="share-link-input"
          >
            <template #prefix>
              <LinkOutlined />
            </template>
          </Input>
          <Button
            type="primary"
            :disabled="shareLinkUnavailable"
            @click="copyShareLink"
          >
            <template #icon>
              <CopyOutlined />
            </template>
            {{ $t('common.copy_link') }}
          </Button>
        </div>
        <!-- 链接主机是部署事实（PUBLIC_BASE_URL），前端猜不出来：说清去哪配，
             而不是给一条打不开的相对路径。 -->
        <div v-if="shareLinkUnavailable" class="share-manage-hint">
          {{ $t('errors.share_link_unavailable_please_configure_public_base_url') }}
        </div>
        <div class="share-manage">
          <span class="share-manage-hint">{{ $t('common.link_is_public_anyone_with_the_link_can_view_it') }}</span>
          <Button
            danger
            :loading="shareRevoking"
            @click="revokeCurrentShare"
          >
            {{ $t('common.cancel_sharing') }}
          </Button>
        </div>
      </template>

      <template v-else>
        <div class="share-select-title">
          {{ $t('chat.share.selectMessages', { selected: shareMessageIds.length, total: shareCandidates.length }) }}
        </div>
        <div class="share-select-list">
          <label
            v-for="c in shareCandidates"
            :key="c.id"
            class="share-select-item"
            @click.prevent="toggleShareMessage(c.id)"
          >
            <Checkbox
              :checked="shareMessageIds.includes(c.id)"
              @click.stop
            />
            <span
              class="share-select-role"
              :class="c.role"
            >{{ c.role === 'user' ? $t('chat.share.roleUser') : 'AI' }}</span>
            <span class="share-select-preview">{{ c.preview || $t('chat.share.emptyMessage') }}</span>
          </label>
        </div>
        <div
          v-if="shareError"
          class="share-error"
        >
          {{ shareError }}
        </div>
        <div class="share-actions">
          <Button
            type="primary"
            :loading="shareLoading"
            @click="generateShare"
          >
            {{ $t('common.generate_share_link') }}
          </Button>
        </div>
      </template>
    </Modal>

  </div>
</template>

<style scoped>
.chat-layout { position: relative; display: flex; height: 100%; background: var(--bg-page); overflow: hidden; }
.chat-main { position: relative; flex: 1; display: flex; flex-direction: column; min-width: 0; }
/* N7 v1：项目标签 —— 现在住在标题栏左插槽（停靠坞右侧）。
   横向滚动而不是换行：标签数取决于用户打开过多少项目，换行会把标题栏撑高。
   `min-width: 0` 让它能在窄窗口里让位给右侧导航，而不是把右组顶出可视区。 */
.project-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  max-width: min(46vw, 560px);
  overflow-x: auto;
  scrollbar-width: none;
  flex: 0 1 auto;
}
.project-tabs::-webkit-scrollbar { display: none; }
.project-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  height: 24px;
  max-width: 200px;
  padding: 0 4px 0 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  /* 落在标题栏拖拽区里：不退出拖拽的话按下会被当成拖动窗口，标签永远点不到 */
  -webkit-app-region: no-drag;
}
.project-tab:hover { color: var(--text-primary); background: var(--bg-surface-hover); }
.project-tab:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}
.project-tab.active {
  color: var(--primary);
  font-weight: var(--fw-semibold);
  border-color: var(--primary);
  background: var(--primary-bg);
}
/* 跑马灯自己负责溢出：不要再给 ellipsis，否则两份文本的轨道量不出真实宽度 */
.project-tab-name { flex: 0 1 auto; min-width: 0; }
.project-tab-close {
  flex: none;
  border: 0;
  background: transparent;
  color: inherit;
  opacity: 0;
  /* 不可见时同时不可点：否则标签右端会藏着一个"看不见的关闭键"，误点即关 */
  pointer-events: none;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
  font-size: var(--fs-sm);
  transition: opacity var(--dur-fast) ease;
}
/* 关闭键只在 hover / 激活 / 键盘焦点时出现：标签栏在标题栏里，宽度是最稀缺资源 */
.project-tab:hover .project-tab-close,
.project-tab.active .project-tab-close,
.project-tab:focus-within .project-tab-close { opacity: 0.65; pointer-events: auto; }
.project-tab-close:hover { opacity: 1; }
.project-tab-close:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}
.project-tab-add {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  margin-inline-start: 2px;
  border: var(--hairline-width) dashed var(--border-default);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  line-height: 1;
  -webkit-app-region: no-drag;
}
.project-tab-add:hover { color: var(--text-primary); border-color: var(--primary); }
/* 没有项目标签时的兜底：单枚不可交互的"对话名"标签，保证当前对话在标题栏可见 */
.project-tab-solo {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  max-width: min(32vw, 320px);
  height: 24px;
  padding-inline: 8px;
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  white-space: nowrap;
}
.topbar-cluster { display: flex; align-items: center; gap: 8px; min-width: 0; }

/* 标题栏右插槽里的会话导航：始终纯图标，26px 见方，靠 title/aria-label 说明 */
.topbar-chat-actions { display: flex; align-items: center; gap: 2px; }
.topbar-actions-divider {
  flex: none;
  width: var(--hairline-width);
  height: 16px;
  margin-inline: 4px;
  background: var(--border);
}
.toolbar-btn.ant-btn {
  width: 26px;
  min-width: 26px;
  height: 26px;
  padding: 0;
  color: var(--text-secondary);
  border-radius: var(--radius-md);
}
.toolbar-btn:hover { color: var(--text-primary) !important; background: var(--bg-hover) !important; }
.toolbar-btn.active { color: var(--primary); background: var(--primary-bg); }
.toolbar-btn:not(:disabled):active { transform: scale(0.94); }
/* 交换布局：侧边栏移到左侧、分屏移到右侧。
   用 flex `order` 而不是改 DOM —— 组件的挂载位置不动，只是视觉顺序变化，
   所以不影响任何依赖 DOM 顺序的逻辑（焦点流、SSR、既有测试）。
   .side-panel 默认在 DOM 末尾（order 0 → 靠右），给它 -1 就排到最左；
   默认左边依次是 workbench、preview-pane、主会话。交换后要镜像成
   「侧边栏 | 主会话 | 分屏 | 工作台」，所以 workbench 取 2、preview-pane 取 1 ——
   少了这一组，左侧会同时挤着侧边栏与工作台，而 `is-swapped` 要的正是把
   主会话让回中间。 */
/* ⚠️ 必须用 :deep() —— 这是这个按钮一开始"点了没反应"的原因。
   .side-panel 与 .preview-pane 都是**子组件的根元素**，带的是它们自己的
   data-v 作用域 id。父组件 scoped 里直写 `.chat-layout.is-swapped .side-panel`
   会被编译成 `.chat-layout.is-swapped .side-panel[data-v-父]`，**永不匹配**。
   :deep() 让作用域 id 落在 .chat-layout（父组件自己的元素）上，从而真正命中。 */
.chat-layout.is-swapped :deep(.side-panel) { order: -1; }
.chat-layout.is-swapped :deep(.workbench-panel) { order: 2; }
.chat-layout.is-swapped :deep(.reference-dock) { order: 1; }
.chat-body { position: relative; flex: 1; display: flex; flex-direction: column; min-height: 0; }
/* 任务清单挂在输入区正上方：与输入框留一点呼吸，但不占满整行宽度 */
.chat-todo-panel { flex: none; margin: 0 16px 6px; }

/* 会话内检索条：标题栏下沉到页内，不占用消息区高度（无结果时只显示输入框） */
.chat-search {
  flex: none;
  display: flex; align-items: center; gap: 8px;
  margin: 8px 16px 0; padding: 5px 10px;
  border: var(--hairline-width) solid var(--border); border-radius: var(--radius-lg);
  background: var(--bg-card);
}
.chat-search-icon { flex: none; font-size: var(--fs-sm); color: var(--text-tertiary); }
.chat-search-input { flex: 1; min-width: 0; border: none; background: none; outline: none; color: var(--text-primary); font-size: var(--fs-sm); }
/* 输入框自己 outline: none，焦点提示交给整条检索栏（它的按钮已各自有 :focus-visible） */
.chat-search:focus-within { box-shadow: 0 0 0 var(--focus-ring-width) var(--focus-ring-glow); }
.chat-search-input::placeholder { color: var(--text-quaternary); }
.chat-search-count { flex: none; font-size: var(--fs-xs); color: var(--text-tertiary); font-variant-numeric: tabular-nums; white-space: nowrap; }
.chat-search-actions { flex: none; display: flex; gap: 2px; }
.chat-search-btn { border: none; background: none; color: var(--text-secondary); }
.chat-search-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.chat-search-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.chat-search-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (max-width: 576px) { .chat-search { margin: 6px 12px 0; } .chat-search-input { font-size: var(--fs-sm); } }
/* 标题栏空间按优先级让位：先收窄项目标签，再缩短兜底会话名；右侧永远是纯图标，
   不需要在这里做折叠 —— 它们本来就只有 26px 一个按钮的宽度。 */
@media (max-width: 1280px) {
  .project-tabs { max-width: min(40vw, 420px); }
  .project-tab { max-width: 160px; }
  .project-tab-solo { max-width: 220px; }
}
@media (max-width: 1024px) {
  .project-tabs { max-width: 32vw; }
  .project-tab { max-width: 132px; }
  .project-tab-solo { max-width: 160px; }
}
@media (max-width: 768px) {
  .project-tabs { max-width: 28vw; }
  .project-tab { max-width: 112px; }
  .project-tab-solo { max-width: 132px; }
}
@media (max-width: 576px) {
  .project-tab-solo { display: none; }

  .turn-status { margin: 8px auto 0; padding: 0 12px; }
}
/* 结构化提问卡片区：与审批卡同样贴输入区上方 */
.connection-banner {
  /* 让开自绘标题栏：它是 fixed 且层级更高（--z-page-bar > --z-drawer），
     贴 top:0 会把最小化/关闭那一栏一起盖住，窗口按钮直接点不到。 */
  position: fixed; top: var(--app-titlebar-h); left: 0; right: 0; z-index: var(--z-page-bar);
  padding: 4px 12px; text-align: center;
  font-size: var(--fs-sm); line-height: 18px;
  background: var(--error); color: var(--on-solid);
}
.share-risk { margin-bottom: 14px; }
.share-guest-tip { padding: 20px 0; text-align: center; color: var(--text-secondary); font-size: var(--fs-base); }
.share-link-row { display: flex; gap: 10px; margin-top: 14px; }
.share-link-input { flex: 1; }
.share-manage { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.share-manage-hint { font-size: var(--fs-sm); color: var(--text-tertiary); }
.share-select-title { font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--text-primary); margin: 14px 0 8px; }
.share-select-list { display: flex; flex-direction: column; gap: 2px; max-height: 260px; overflow-y: auto; width: 100%; }
.share-select-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 8px; border-radius: var(--radius-lg); cursor: pointer;
  transition: background var(--dur-fast) ease;
}
.share-select-item:hover { background: var(--bg-hover); }
.share-select-role { flex: none; font-size: var(--fs-xs); font-weight: var(--fw-semibold); padding: 1px 7px; border-radius: var(--sig-radius-button); }
.share-select-role.user { color: var(--primary); background: var(--primary-bg); }
.share-select-role.assistant { color: var(--text-secondary); background: var(--bg-secondary); }
.share-select-preview { flex: 1; min-width: 0; font-size: var(--fs-sm); color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.share-error { margin-top: 10px; font-size: var(--fs-sm); color: var(--error); }
.share-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
.turn-status {
  align-self: flex-start; margin: 10px auto 0; max-width: min(var(--chat-content-width), 92%); padding: 0 24px;
  height: 26px; display: inline-flex; align-items: center;
  font-size: var(--fs-sm); font-weight: var(--fw-semibold); white-space: nowrap;
  background: linear-gradient(90deg, var(--primary) 0%, var(--primary) 40%, var(--accent) 50%, var(--primary) 60%, var(--primary) 100%);
  background-position: 100% 0; background-size: 250% 100%; background-clip: text; -webkit-background-clip: text;
  color: transparent; -webkit-text-fill-color: transparent;
  animation: turnStatusShimmer var(--dur-pulse) linear infinite;
  font-variant-numeric: tabular-nums;
}
@keyframes turnStatusShimmer { to { background-position: 0 0; } }
@media (prefers-reduced-motion: reduce) {
  .turn-status { background-position: 0 0; background-size: 100% 100%; animation: none; }
}
.panel-overlay {
  position: fixed; inset: 0; z-index: var(--z-page-overlay);
  background: var(--bg-overlay);
}
@media (min-width: 1025px) { .panel-overlay { display: none; } }
.overlay-fade-enter-active, .overlay-fade-leave-active { transition: opacity var(--dur-normal) ease; }
.overlay-fade-enter-from, .overlay-fade-leave-to { opacity: 0; }

.unified-error-banner {
  flex: none; display: flex; align-items: center; gap: 8px;
  margin: 8px 20px 0; padding: 8px 12px;
  background: var(--error-bg); border: var(--hairline-width) solid var(--error);
  border-radius: var(--sig-radius-card); color: var(--danger, var(--error)); font-size: var(--fs-sm);
}
.ueb-text { flex: 1; min-width: 0; line-height: 1.5; }
.ueb-close {
 flex: none; cursor: pointer; opacity: 0.7; font-size: var(--fs-sm);
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.ueb-close:hover { opacity: 1; }

.unified-bar {
  flex: none; display: flex; align-items: center; gap: 8px;
  margin: 8px 20px 0; padding: 6px 10px;
  border: var(--hairline-width) solid var(--border); border-radius: var(--sig-radius-card);
  background: var(--bg-card);
}
.ub-badge {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  padding: 1px 10px; border-radius: var(--sig-radius-button);
  background: var(--primary); color: var(--on-solid);
  font-size: var(--fs-xs); font-weight: var(--fw-semibold); line-height: 18px;
  transition: background var(--dur-normal) ease;
}
.ub-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--on-viewer-strong); }
.ub-badge.running::before {
  animation: uehPulse var(--dur-pulse) ease-in-out infinite;
  box-shadow: 0 0 0 0 var(--on-viewer-soft);
}
.ub-badge.running { animation: uehPulse var(--dur-pulse) ease-in-out infinite; }
.ub-badge.done { background: var(--success); }
.ub-badge.done::before { animation: none; }
.ub-mode {
  flex: none; font-size: var(--fs-xs); color: var(--text-secondary);
  background: var(--bg-secondary); padding: 1px 8px; border-radius: var(--sig-radius-button); line-height: 18px;
}
.ub-spacer { flex: 1; }
.ub-btn {
  flex: none;
  background: var(--bg-card); color: var(--text-secondary);
  box-shadow: inset 0 0 0 var(--hairline-width) var(--border);
  transition: box-shadow var(--dur-fast), color var(--dur-fast);
}
.ub-btn:focus-visible,
.ueb-close:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}
.ueb-close:focus-visible { border-radius: var(--radius-sm); }
.ub-btn:hover:not(:disabled) { box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); color: var(--primary); }
.ub-btn.exit:hover:not(:disabled) { box-shadow: inset 0 0 0 var(--hairline-width) var(--danger, var(--error)); color: var(--danger, var(--error)); }
.ub-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.unified-exec-hint {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  align-self: center; margin: 10px auto 0;
  font-size: var(--fs-sm); color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.ueh-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--primary);
  animation: uehPulse var(--dur-pulse) ease-in-out infinite;
}
@keyframes uehPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .ueh-dot { animation: none; } }
@media (prefers-reduced-motion: reduce) {
  .ub-badge.running,
  .ub-badge.running::before { animation: none; }
  .ub-badge { transition: none; }
}

.unified-list { flex: 1; overflow-y: auto; padding: 12px 0 24px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.unified-empty { padding: 40px 20px; text-align: center; color: var(--text-muted); font-size: var(--fs-sm); }
.kb-hits-tag {
  display: flex; align-items: center;
  max-width: min(var(--chat-content-width), 92%); margin: 2px auto 6px;
  padding: 2px 10px; border-radius: var(--sig-radius-button);
  background: var(--primary-bg); color: var(--primary);
  font-size: var(--fs-xs); line-height: 18px;
}
.kb-hits-text { flex: 1; min-width: 0; }
.kb-hits-link {
  flex: none; margin-inline-start: auto; padding-inline-start: 12px;
  color: var(--primary); font-weight: var(--fw-semibold); white-space: nowrap;
  text-decoration: none;
}
.kb-hits-link:hover { text-decoration: underline; }
@media (max-width: 576px) {
  .unified-error-banner { margin: 6px 12px 0; }
  .unified-bar { margin: 6px 12px 0; }
  .kb-hits-tag { max-width: 88%; }
}

/* ── 存为 Agent 弹窗 ── */
.save-agent-form { display: flex; flex-direction: column; gap: 6px; }
.save-agent-label { font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--text-secondary); }
.save-agent-hint { margin: 8px 0 0; font-size: var(--fs-sm); line-height: 1.6; color: var(--text-secondary); }

@media (max-width: 768px) {
  .map-overlay {
    padding: 0;
  }

  .map-frame {
    border: none;
    border-radius: 0;
  }

  .map-sub {
    display: none;
  }
}

/* 会话地图浮层：整屏承载 SPA 内的 SessionMapView。
   画布自身的样式在 src/views/sessionmap/ 内，这里只负责铺满与层级 ——
   所以全部走 Chiron 的 token，不引入硬编码值。 */
.session-map-overlay {
  position: fixed;
  /* 让开自绘标题栏：导航壳那条 `.titlebar` 是 fixed + 高 36px，**整条**
     `-webkit-app-region: drag`。浮层若贴 top:0，它自己的工具栏（`.map-view-bar`
     是第一个子元素，按钮居中在 y≈8..30）正好落在拖拽区里 —— 按钮看着在，
     按下去变成拖窗口，表现就是"功能按钮全部被遮挡/点不动"。
     与上面的 `.connection-banner` 同一处置：偏移读 `--app-titlebar-h`
     （桌面壳 36/28px，浏览器预览 0），不写死像素。 */
  inset-block-start: var(--app-titlebar-h);
  inset-inline: 0;
  inset-block-end: 0;
  z-index: var(--z-viewer);
  background: var(--bg-page);
}
</style>
<!-- 非 scoped：Transition 的类名挂在**子组件根元素**上（.workbench-panel / .side-panel），
     带 ChatView 的 data-v 作用域选择器永远匹配不到。与 CollapseTransition.vue 同样的
     处置（那里也是非 scoped，理由写在组件注释里）。 -->
<style>
/* 工作台折叠/展开：200ms，只动 opacity + transform。
   为什么不动 width：工作台是 flex 行里的一个子项，宽度动画会让整条 flex 行每帧重排，
   而聊天区正在跑 transcript 的测量账本 —— 重排会把它的高度读数冲掉，表现为滚动位置
   抖动。位移+淡入同样给出"它是从侧边推进来/收回去"的方向感。 */
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: opacity var(--dur-normal) ease, transform var(--dur-normal) ease;
  will-change: opacity, transform;
}

.panel-slide-enter-from,
.panel-slide-leave-to {
  opacity: 0;
  transform: translateX(-12px);
}

.panel-slide-enter-to,
.panel-slide-leave-from {
  opacity: 1;
  transform: translateX(0);
}

@media (prefers-reduced-motion: reduce) {
  .panel-slide-enter-active,
  .panel-slide-leave-active {
    transition: none;
  }
}
</style>
