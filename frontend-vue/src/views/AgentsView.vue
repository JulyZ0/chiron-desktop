<script setup lang="ts">
import { ref, onMounted, onUnmounted, markRaw, computed } from 'vue'
import type { Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Button, Tabs, TabPane, Modal, Input, InputNumber, Switch, Tag,
  Alert, Dropdown, Menu, MenuItem, Select, message,
} from 'ant-design-vue'
import {
  PlusOutlined, PlayCircleOutlined, DeleteOutlined,
  StopOutlined, ClockCircleOutlined, CheckCircleOutlined,
  CloseCircleOutlined, SyncOutlined, RobotOutlined, HistoryOutlined,
  MessageOutlined, TeamOutlined, LockOutlined, SearchOutlined,
  ArrowLeftOutlined, MoreOutlined,
} from '@ant-design/icons-vue'
import {
  listAgents, createAgent, updateAgent, deleteAgent,
  runAgent, listAgentSessions, getAgentSession,
setAgentVisibility,
} from '../api'
import type { Agent, AgentSession, WorkbenchResource } from '../api'
import { listKnowledgeBases, listPlugins, listSkillResources } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import ToolPicker from '../components/agent/ToolPicker.vue'
import TakeoverPanel from '../components/agent/TakeoverPanel.vue'
import SaveToKnowledgeDialog from '../components/chat/SaveToKnowledgeDialog.vue'
import { setChatPrefill } from '../components/chat/chatPrefill'
import { agentResultToMarkdown, parseAgentResult } from '../utils/agentResultMarkdown'

import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { errorDetail } from '../utils/apiError'
import { serverErrorDetail, errorStatus } from '../utils/apiError'
const { t } = useI18n()
// ── 数据 ──
// 后端列表项可能带 visibility（'private'|'tenant'）；缺失视为 private
type AgentRow = Agent & { visibility?: 'private' | 'tenant' }

// ── 属主判定 ──
// 列表接口会同时返回本租户内别人共享（visibility='tenant'）的 Agent，而后端
// update / delete / setAgentVisibility 三处的 SQL 都带 `AND user_id = $N`，
// 非属主点下去必定 403。所以这里前置判断，把必然失败的操作置灰，而不是让用户
// 点完才吃一个报错。
const authStore = useAuthStore()
const currentUserId = computed(() => authStore.user?.id ?? '')

/**
 * 能否编辑/删除这个 Agent。
 *
 * 两个方向都刻意不误锁：
 * ① `a.user_id` 缺失（老数据、或本地模式后端没回填）→ 放行，交给后端裁决；
 * ② 当前用户 id 拿不到 → 同样放行。宁可多一次 403 提示，也不要让所有者
 *    因为拿不到自己的 id 而被锁在门外。
 */
function canManage(a: AgentRow): boolean {
  if (!a.user_id) return true
  if (!currentUserId.value) return true
  return a.user_id === currentUserId.value
}

const agents = ref<AgentRow[]>([])
const loadingAgents = ref(true)
const errorAgents = ref(false)
const sessions = ref<AgentSession[]>([])
const loadingSessions = ref(false)
const errorSessions = ref(false)
const activeTab = ref('agents')
const router = useRouter()
const route = useRoute()

async function loadAgents() {
  loadingAgents.value = true
  errorAgents.value = false
  try {
    agents.value = await listAgents()
  } catch {
    errorAgents.value = true
    message.error(t('errors.failed_to_fetch_agent_list'))
  } finally {
    loadingAgents.value = false
  }
}

async function loadSessions() {
  loadingSessions.value = true
  errorSessions.value = false
  try {
    sessions.value = await listAgentSessions()
  } catch {
    errorSessions.value = true
    message.error(t('errors.failed_to_fetch_run_history'))
  } finally {
    loadingSessions.value = false
  }
}

/**
 * 跨台活动直达：`?session=<id>` 时直接打开那一次运行的结果。
 *
 * 「最近活动」的 route 现在带 session id（internal/api/activities.go），点 Agent
 * 记录会落到这里 —— 以前只到列表页，用户还得自己翻。
 */
async function openSessionFromQuery() {
  const sid = typeof route.query.session === 'string' ? route.query.session.trim() : ''
  if (!sid) return
  try {
    detailSession.value = await getAgentSession(sid)
    detailOpen.value = true
  } catch {
    // 找不到（已删 / 非本人）就留在列表页，不打扰
  }
}

onMounted(() => {
  loadAgents()
  loadSessions()
  void openSessionFromQuery()
})
onUnmounted(() => stopPolling())


// ── 新建 / 编辑 ──
interface EditorState {
  id: string
  name: string
  description: string
  system_prompt: string
  tools_text: string
  model: string
  max_tokens: number
  temperature: number
  max_turns: number
  timeout_seconds: number
  enabled: boolean
}
const editorSaving = ref(false)
const editingId = ref('')
const form = ref<EditorState>({
  id: '', name: '', description: '', system_prompt: '',
  tools_text: '[]', model: 'deepseek-chat', max_tokens: 4096,
  temperature: 0.6, max_turns: 5, timeout_seconds: 120, enabled: true,
})

// ── 工作台绑定：Agent 自带的知识库 / 技能 / 插件（派发时由引擎消费）──
// 单独用 ref 而不塞进 EditorState：它们只在编辑器里维护，不该影响既有字段与校验。
const formKbId = ref<string | undefined>(undefined)
const formSkills = ref<string[]>([])
const formPlugins = ref<string[]>([])
const kbOptions = ref<WorkbenchResource[]>([])
const skillOptions = ref<WorkbenchResource[]>([])
const pluginOptions = ref<WorkbenchResource[]>([])
const bindingLoading = ref(false)

async function loadBindingOptions() {
  if (
    bindingLoading.value
    || (kbOptions.value.length && skillOptions.value.length
      && pluginOptions.value.length)
  ) return
  bindingLoading.value = true
  try {
    // 三个接口互不依赖：任一失败只让那一类为空，不拖垮编辑器
    const [bases, skills, plugins] = await Promise.all([
      listKnowledgeBases().catch(() => [] as WorkbenchResource[]),
      listSkillResources().catch(() => [] as WorkbenchResource[]),
      listPlugins().catch(() => [] as WorkbenchResource[]),
    ])
    kbOptions.value = bases
    skillOptions.value = skills
    pluginOptions.value = plugins
  } finally {
    bindingLoading.value = false
  }
}

function resetBinding() {
  formKbId.value = undefined
  formSkills.value = []
  formPlugins.value = []
}

// ── 两栏工作台的状态 ──
// 左栏筛选 + 列表，右栏详情 + 表单。`creating` 为真时右栏显示空白表单，
// 此时 `selectedAgentId` 为空 —— 两种「右栏有内容」的情形混在一个布尔里判断。
const searchQuery = ref('')
type AgentGroup = 'all' | 'mine' | 'team' | 'disabled'
const groupFilter = ref<AgentGroup>('all')
const selectedAgentId = ref('')
const creating = ref(false)
/** 右栏操作结果的内联反馈（与 message toast 并行：toast 会消失，这里留在详情里） */
const feedback = ref<{ type: 'success' | 'error'; text: string } | null>(null)
/** 最近一次「开始编辑」时的表单快照，用来判断是否有未保存修改 */
const formBaseline = ref('')

const selectedAgent = computed<AgentRow | null>(
  () => agents.value.find(a => a.id === selectedAgentId.value) ?? null,
)
const detailVisible = computed(() => creating.value || selectedAgent.value !== null)

const groupCounts = computed(() => ({
  all: agents.value.length,
  mine: agents.value.filter(canManage).length,
  team: agents.value.filter(a => a.visibility === 'tenant').length,
  disabled: agents.value.filter(a => !a.enabled).length,
}))

const filteredAgents = computed(() => {
  // 「全部」不做启用态过滤：已停用的 Agent 同样列出（行内带「已停用」标记），
  // 否则用户没有重新启用的入口，页面会像"没装过"；只有显式选「已停用」时才只看停用项。
  const q = searchQuery.value.trim().toLowerCase()
  return agents.value.filter((a) => {
    if (q) {
      const hay = `${a.name || ''} ${a.description || ''}`.toLowerCase()
      if (!hay.includes(q)) return false
    }
    switch (groupFilter.value) {
      case 'mine': return canManage(a)
      case 'team': return a.visibility === 'tenant'
      case 'disabled': return !a.enabled
      default: return true
    }
  })
})

function snapshot(): string {
  return JSON.stringify({
    form: form.value,
    kb: formKbId.value ?? '',
    skills: formSkills.value,
    plugins: formPlugins.value,
  })
}

function markClean() {
  formBaseline.value = snapshot()
}

const isDirty = computed(() => formBaseline.value !== '' && snapshot() !== formBaseline.value)

/**
 * 有未保存修改时先确认，再执行切换动作。
 * 用 `Modal.confirm` 而不是拦整屏 —— 它只是一个小确认框，焦点仍由用户控制。
 */
function guardDirty(next: () => void) {
  if (!isDirty.value) { next(); return }
  Modal.confirm({
    title: t('agent.discard_unsaved_title'),
    content: t('agent.discard_unsaved_content'),
    okText: t('common.discard'),
    okButtonProps: { danger: true },
    cancelText: t('common.cancel'),
    onOk: () => { next() },
  })
}

/** 把列表项回填到右栏表单（只读展示与编辑共用同一份状态） */
function applyAgent(a: AgentRow) {
  editingId.value = a.id
  selectedAgentId.value = a.id
  creating.value = false
  const llm = a.llm_config || {}
  form.value = {
    id: a.id,
    name: a.name,
    description: a.description || '',
    system_prompt: a.system_prompt || '',
    tools_text: JSON.stringify(a.tools || [], null, 2),
    model: String(llm.model || 'deepseek-chat'),
    max_tokens: Number(llm.max_tokens || 4096),
    temperature: Number(llm.temperature || 0.6),
    max_turns: a.max_turns || 5,
    timeout_seconds: a.timeout_seconds || 120,
    enabled: a.enabled,
  }
  // 回填工作台绑定（后端未升级时字段为 undefined，视为未绑定）
  formKbId.value = a.kb_id || undefined
  formSkills.value = Array.isArray(a.skills) ? [...a.skills] : []
  formPlugins.value = Array.isArray(a.plugins) ? [...a.plugins] : []
  feedback.value = null
  void loadBindingOptions()
  markClean()
}

/**
 * 空状态/头部入口：去市场装 Agent。
 *
 * 本页不提供「新建」：Agent 的定义（prompt / 工具集 / 绑定）与「装进来」统一由
 * 资源市场承担，这里只保留已有 Agent 的编辑、启停、派发、删除。
 */
function goToMarket() {
  void router.push({ path: '/market', query: { kind: 'agent' } })
}

function openEdit(a: AgentRow) {
  // 重复点当前项不该弹「放弃修改」—— 那只是同一条详情
  if (a.id === selectedAgentId.value && !creating.value) return
  guardDirty(() => applyAgent(a))
}

/** 关闭右栏（窄屏＝返回列表）；有未保存修改时先确认 */
function closeDetail() {
  guardDirty(() => {
    creating.value = false
    selectedAgentId.value = ''
    editingId.value = ''
    feedback.value = null
    formBaseline.value = ''
  })
}

function parseToolsText(): unknown[] | null {
  try {
    const v = JSON.parse(form.value.tools_text)
    return Array.isArray(v) ? v : null
  } catch {
    message.error(t('agent.tools_is_not_a_valid_json_array'))
    return null
  }
}

async function saveEditor() {
  const f = form.value
  if (!f.name.trim()) { message.warning(t('common.please_enter_a_name_2')); return }
  const tools = parseToolsText()
  if (tools === null) return
  const isCreate = !editingId.value
  const body: Partial<Agent> = {
    name: f.name.trim(),
    description: f.description,
    system_prompt: f.system_prompt,
    tools,
    llm_config: { model: f.model || 'deepseek-chat', max_tokens: f.max_tokens, temperature: f.temperature },
    max_turns: f.max_turns,
    timeout_seconds: f.timeout_seconds,
    enabled: f.enabled,
    // 工作台绑定：Agent 自带的知识库 / 技能 / 插件
    kb_id: formKbId.value || '',
    skills: formSkills.value,
    plugins: formPlugins.value,
  }
  editorSaving.value = true
  feedback.value = null
  try {
    const saved = isCreate
      ? await createAgent(body)
      : await updateAgent(editingId.value, body)
    const text = isCreate ? t('common.created') : t('common.saved')
    message.success(text)
    feedback.value = { type: 'success', text }
    await loadAgents()
    // 新建成功后直接落到新 Agent 的详情上；更新则回填服务端的最新值（清掉脏标记）
    const nextId = isCreate ? (saved?.id || '') : editingId.value
    const fresh = agents.value.find(a => a.id === nextId)
    if (fresh) applyAgent(fresh)
    // applyAgent 会清掉 feedback，这里补回操作结果
    feedback.value = { type: 'success', text }
  } catch (e) {
    const text = t('errors.save_failed_error', { error: errorDetail(e, '') })
    message.error(text)
    feedback.value = { type: 'error', text }
  } finally {
    editorSaving.value = false
  }
}

// ── 启停 / 删除 ──
async function toggleEnabled(a: AgentRow) {
  try {
    await updateAgent(a.id, { enabled: !a.enabled })
    a.enabled = !a.enabled
    // 详情正开着且没有别的未保存修改时，让表单里的开关跟着走
    if (selectedAgentId.value === a.id && !isDirty.value) {
      form.value.enabled = a.enabled
      markClean()
    }
  } catch {
    message.error(t('errors.operation_failed'))
  }
}

// ── 共享可见性（团队共享 / 私有；owner-only，非属主 403 提示）──
async function toggleVisibility(a: AgentRow) {
  const next = a.visibility === 'tenant' ? 'private' : 'tenant'
  try {
    await setAgentVisibility(a.id, next)
    message.success(next === 'tenant' ? t('common.shared_with_team') : t('common.set_to_private'))
    await loadAgents()
  } catch (e) {
    const msg = serverErrorDetail(e, '')
    if (errorStatus(e) === 403) {
      message.error(t('agent.can_only_operate_agents_you_created') + (msg ? `：${msg}` : ''))
    } else {
      message.error(t('errors.operation_failed') + (msg ? `：${msg}` : ''))
    }
  }
}

function requestDelete(a: AgentRow) {
  Modal.confirm({
    title: t('agent.delete_agent'),
    content: t('common.delete_name_its_run_history_will_also_be_removed', { name: a.name }),
    okText: t('common.delete'),
    okButtonProps: { danger: true },
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteAgent(a.id)
        message.success(t('common.deleted'))
        // 删掉的正是右栏在看的那个：清空详情，避免留着一份指向已删除实体的表单
        if (selectedAgentId.value === a.id || editingId.value === a.id) {
          creating.value = false
          selectedAgentId.value = ''
          editingId.value = ''
          formBaseline.value = ''
        }
        await loadAgents()
        await loadSessions()
      } catch {
        message.error(t('errors.delete_failed'))
      }
    },
  })
}

// ── 互联互通：在对话中发起会话（携带 Agent 配置上下文）──
function chatWithAgent(a: Agent) {
  router.push({ path: '/chat', query: { agent: a.id } })
}

// ── 运行 + 轮询 ──
const runOpen = ref(false)
const runTarget = ref<Agent | null>(null)
const runTask = ref('')
const runSession = ref<AgentSession | null>(null)
const runSubmitting = ref(false)
let pollTimer: number | undefined

function stopPolling() {
  if (pollTimer !== undefined) { window.clearInterval(pollTimer); pollTimer = undefined }
}

function startPolling(sessionId: string) {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    try {
      const s = await getAgentSession(sessionId)
      runSession.value = s
      // 同步到历史列表（若有）
      const idx = sessions.value.findIndex(x => x.id === s.id)
      if (idx >= 0) sessions.value[idx] = s
      if (s.status === 'completed' || s.status === 'failed') stopPolling()
    } catch {
      stopPolling()
    }
  }, 2000)
}

function openRun(a: Agent) {
  runTarget.value = a
  runTask.value = ''
  runSession.value = null
  runSubmitting.value = false
  stopPolling()
  runOpen.value = true
}

async function submitRun() {
  if (!runTarget.value) return
  const task = runTask.value.trim()
  if (!task) { message.warning(t('workflow.please_enter_a_task')); return }
  runSubmitting.value = true
  try {
    const s = await runAgent(runTarget.value.id, task)
    runSession.value = s
    sessions.value.unshift(s)
    message.success(t('workflow.task_dispatched_executing'))
    startPolling(s.id)
  } catch (e) {
    message.error(t('errors.dispatch_failed_error', { error: errorDetail(e, '') }))
  } finally {
    runSubmitting.value = false
  }
}

function closeRun() {
  stopPolling()
  runOpen.value = false
}

// ── 历史结果查看 ──
const detailOpen = ref(false)
const detailSession = ref<AgentSession | null>(null)

async function openDetail(s: AgentSession) {
  try {
    const full = await getAgentSession(s.id)
    detailSession.value = full
  } catch {
    detailSession.value = s
  }
  detailOpen.value = true
}

// ── 结果展示 ──
// 解析逻辑移到 utils：同一份逻辑要供「存入知识库」复用，且能被单测覆盖
/**
 * 运行结果的**视图模型**：模板直接读这些字段（耗时、工具调用数、token 用量），
 * 而 `ParsedAgentResult` 出于解析容错一律是 `unknown` —— 在边界处一次性收窄。
 */
interface AgentResultView {
  output?: string
  error?: string
  duration?: number
  tool_calls?: unknown[]
  token_usage?: Record<string, unknown>
}

function parseResult(s: AgentSession): AgentResultView {
  return parseAgentResult(s.result) as AgentResultView
}

/** 把这次运行的结果带进对话继续讨论（内容经 sessionStorage 投递，避免超长 URL） */
function continueInChat() {
  const session = detailSession.value
  if (!session) return
  const parsed = parseResult(session)
  const text = parsed.output || parsed.error || session.result || ''
  if (!text) {
    message.warning(t('chat.this_run_has_no_content_to_bring_into_the_conversation'))
    return
  }
  setChatPrefill({
    title: t('common.run_result_name', { name: session.agent_name || 'Agent' }),
    text: String(text),
    source: 'agent',
  })
  detailOpen.value = false
  router.push('/chat')
}

// ── 运行结果存入知识库 ──
// 复用对话那边的组件：Agent 结果同样沉淀成 Markdown 文档，走同一条分片上传链路
const saveToKbOpen = ref(false)
const saveToKbContent = ref('')
const saveToKbTitle = ref('')

function openSaveToKb(session: AgentSession | null) {
  if (!session) return
  const markdown = agentResultToMarkdown(session)
  if (!markdown) {
    message.warning(t('common.this_run_has_no_content_to_preserve'))
    return
  }
  saveToKbContent.value = markdown
  saveToKbTitle.value = t('common.run_result_name', { name: session.agent_name || 'Agent' })
  saveToKbOpen.value = true
}

const statusMeta: Record<string, { label: string; color: string; icon: Component }> = {
  pending: { label: t('common.queuing'), color: 'default', icon: ClockCircleOutlined },
  running: { label: t('common.executing'), color: 'processing', icon: SyncOutlined },
  completed: { label: t('common.completed'), color: 'success', icon: CheckCircleOutlined },
  failed: { label: t('errors.failed'), color: 'error', icon: CloseCircleOutlined },
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}-${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function toolCount(a: Agent): number {
  return Array.isArray(a.tools) ? a.tools.length : 0
}
</script>

<template>
  <div class="agents-page">
    <header class="page-header">
      <div class="page-header-text">
        <h1 class="page-title">
          Agents
        </h1>
        <p class="page-sub">
          {{ $t('agent.create_a_dedicated_agent_define_its_prompt_and_tools_and_dispatch_it_to_actually_run_with_one_click') }}
        </p>
      </div>
      <Button
        type="primary"
        @click="goToMarket"
      >
        <template #icon>
          <PlusOutlined />
        </template>
        {{ $t('agent.browse_market') }}
      </Button>
    </header>

    <Tabs
      v-model:active-key="activeTab"
      class="agents-tabs"
    >
      <!-- ── Tab 1：我的 Agent（两栏工作台）── -->
      <TabPane
        key="agents"
        :tab="$t('agent.my_agents')"
      >
        <div
          class="workspace"
          :class="{ 'has-selection': detailVisible }"
        >
          <!-- 左栏：筛选 + 列表 -->
          <aside
            class="pane pane-list"
            :aria-label="$t('agent.list_pane_title')"
          >
            <div class="pane-head">
              <Input
                v-model:value="searchQuery"
                :placeholder="$t('agent.search_agents')"
                allow-clear
                class="pane-search"
              >
                <template #prefix>
                  <SearchOutlined />
                </template>
              </Input>
              <div
                class="group-nav"
                role="group"
                :aria-label="$t('agent.group_filter_label')"
              >
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: groupFilter === 'all' }"
                  :aria-pressed="groupFilter === 'all'"
                  @click="groupFilter = 'all'"
                >
                  {{ $t('agent.filter_all') }}
                  <span class="group-count">{{ groupCounts.all }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: groupFilter === 'mine' }"
                  :aria-pressed="groupFilter === 'mine'"
                  @click="groupFilter = 'mine'"
                >
                  {{ $t('common.custom') }}
                  <span class="group-count">{{ groupCounts.mine }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: groupFilter === 'team' }"
                  :aria-pressed="groupFilter === 'team'"
                  @click="groupFilter = 'team'"
                >
                  {{ $t('common.team_shared') }}
                  <span class="group-count">{{ groupCounts.team }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: groupFilter === 'disabled' }"
                  :aria-pressed="groupFilter === 'disabled'"
                  @click="groupFilter = 'disabled'"
                >
                  {{ $t('common.disabled') }}
                  <span class="group-count">{{ groupCounts.disabled }}</span>
                </button>
              </div>
            </div>

            <div class="pane-body">
              <PageSkeleton
                v-if="loadingAgents"
                variant="list"
                :rows="6"
                :header="false"
              />
              <Alert
                v-else-if="errorAgents"
                type="error"
                show-icon
                :message="$t('errors.failed_to_load')"
                :description="$t('agent.unable_to_fetch_agent_list_please_retry_later')"
              >
                <template #action>
                  <Button
                    size="small"
                    @click="loadAgents"
                  >
                    {{ $t('common.retry') }}
                  </Button>
                </template>
              </Alert>
              <!--
                空状态分两种，判定口径不能混：
                ① `agents.length === 0` —— 真的一个 Agent 都没有（含"装完了但后端没回"的情况），
                   给的是"去市场装一个"的入口；
                ② `filteredAgents.length === 0` —— 有 Agent，只是当前分组/搜索把它们筛掉了，
                   这时说的是"没有匹配"，不能引导用户去市场重装。
                已停用的 Agent 属于第一种的"非空"：它在「全部」分组里仍然列出（带「已停用」标记），
                否则用户会失去启用入口 —— 页面看起来就像"没装过"。
              -->
              <EmptyState
                v-else-if="agents.length === 0"
                :icon="markRaw(RobotOutlined)"
                :description="$t('agent.no_agents_yet')"
                :hint="$t('agent.install_agents_from_the_market_to_get_started')"
              >
                <Button
                  type="primary"
                  size="small"
                  @click="goToMarket"
                >
                  <template #icon>
                    <PlusOutlined />
                  </template>
                  {{ $t('agent.browse_market') }}
                </Button>
              </EmptyState>
              <EmptyState
                v-else-if="filteredAgents.length === 0"
                :icon="markRaw(SearchOutlined)"
                :description="$t('agent.no_matching_agents')"
                :hint="$t('common.try_adjusting_your_search_keywords')"
              />
              <ul
                v-else
                class="item-list"
                role="listbox"
                :aria-label="$t('agent.list_pane_title')"
              >
                <li
                  v-for="a in filteredAgents"
                  :key="a.id"
                  class="item"
                  :class="{
                    active: selectedAgentId === a.id && !creating,
                    muted: !a.enabled,
                  }"
                  role="option"
                  :aria-selected="selectedAgentId === a.id && !creating"
                  tabindex="0"
                  @click="openEdit(a)"
                  @keydown.enter.prevent="openEdit(a)"
                  @keydown.space.prevent="openEdit(a)"
                >
                  <span class="item-avatar"><RobotOutlined /></span>
                  <span class="item-main">
                    <span class="item-title-line">
                      <span class="item-name">{{ a.name }}</span>
                      <Tag
                        v-if="a.visibility === 'tenant'"
                        color="green"
                        class="item-tag"
                      >
                        {{ $t('common.team_shared') }}
                      </Tag>
                      <Tag
                        v-if="!a.enabled"
                        class="item-tag"
                      >
                        {{ $t('common.disabled') }}
                      </Tag>
                    </span>
                    <span class="item-desc">{{ a.description || $t('common.no_description') }}</span>
                    <span class="item-meta">
                      <span class="item-meta-text">{{ $t('agent.n_tools', { n: toolCount(a) }) }}</span>
                      <span class="item-meta-dot">·</span>
                      <span class="item-meta-text">{{ $t('common.up_to_n_rounds', { n: a.max_turns }) }}</span>
                    </span>
                  </span>
                </li>
              </ul>
            </div>
          </aside>

          <!-- 右栏：详情 + 编辑表单 -->
          <section
            class="pane pane-detail"
            :aria-label="$t('agent.detail_title')"
          >
            <button
              type="button"
              class="back-btn"
              @click="closeDetail"
            >
              <ArrowLeftOutlined />
              {{ $t('common.back_to_list') }}
            </button>

            <div
              v-if="!detailVisible"
              class="detail-placeholder"
            >
              <EmptyState
                size="page"
                :icon="markRaw(RobotOutlined)"
                :description="$t('agent.detail_title')"
                :hint="$t('agent.select_detail_hint')"
                :auto-pad="false"
              >
                <Button
                  type="primary"
                  @click="goToMarket"
                >
                  <template #icon>
                    <PlusOutlined />
                  </template>
                  {{ $t('agent.browse_market') }}
                </Button>
              </EmptyState>
            </div>

            <template v-else>
              <div class="detail-head">
                <span class="detail-avatar"><RobotOutlined /></span>
                <div class="detail-titles">
                  <h2 class="detail-name">
                    {{ selectedAgent?.name || '' }}
                  </h2>
                  <p class="detail-sub">
                    {{ form.description || $t('common.no_description') }}
                  </p>
                </div>
                <div
                  v-if="!creating && selectedAgent"
                  class="detail-actions"
                >
                  <Button
                    type="primary"
                    size="small"
                    :disabled="!selectedAgent.enabled"
                    @click="selectedAgent && openRun(selectedAgent)"
                  >
                    <template #icon>
                      <PlayCircleOutlined />
                    </template>
                    {{ $t('common.run') }}
                  </Button>
                  <Button
                    size="small"
                    :title="$t('chat.start_a_session_in_the_conversation')"
                    @click="selectedAgent && chatWithAgent(selectedAgent)"
                  >
                    <template #icon>
                      <MessageOutlined />
                    </template>
                    {{ $t('chat.start_a_conversation') }}
                  </Button>
                  <Dropdown
                    trigger="click"
                    placement="bottomRight"
                  >
                    <Button
                      type="text"
                      size="small"
                      :title="$t('common.more_actions')"
                    >
                      <template #icon>
                        <MoreOutlined />
                      </template>
                    </Button>
                    <template #overlay>
                      <Menu class="card-menu">
                        <MenuItem
                          key="toggle"
                          @click="selectedAgent && toggleEnabled(selectedAgent)"
                        >
                          <template v-if="selectedAgent?.enabled">
                            <StopOutlined class="menu-icon" />{{ $t('common.disable_2') }}
                          </template>
                          <template v-else>
                            <PlayCircleOutlined class="menu-icon" />{{ $t('common.enable') }}
                          </template>
                        </MenuItem>
                        <MenuItem
                          key="visibility"
                          @click="selectedAgent && toggleVisibility(selectedAgent)"
                        >
                          <template v-if="selectedAgent?.visibility === 'tenant'">
                            <LockOutlined class="menu-icon" />{{ $t('common.set_as_private') }}
                          </template>
                          <template v-else>
                            <TeamOutlined class="menu-icon" />{{ $t('common.shared_with_team_2') }}
                          </template>
                        </MenuItem>
                        <MenuItem
                          key="delete"
                          danger
                          :disabled="!selectedAgent || !canManage(selectedAgent)"
                          :title="selectedAgent && canManage(selectedAgent) ? '' : $t('agent.only_creator_can_edit')"
                          @click="selectedAgent && requestDelete(selectedAgent)"
                        >
                          <DeleteOutlined class="menu-icon" />{{ $t('common.delete') }}
                        </MenuItem>
                      </Menu>
                    </template>
                  </Dropdown>
                </div>
              </div>

              <div class="detail-badges">
                <Tag :color="form.enabled ? 'green' : 'default'">
                  {{ form.enabled ? $t('common.enable') : $t('common.disabled') }}
                </Tag>
                <Tag
                  v-if="selectedAgent?.visibility === 'tenant'"
                  color="green"
                >
                  {{ $t('common.team_shared') }}
                </Tag>
                <Tag>{{ $t('agent.model') }}: {{ form.model || '—' }}</Tag>
                <Tag>{{ $t('agent.n_tools', { n: selectedAgent ? toolCount(selectedAgent) : 0 }) }}</Tag>
                <Tag>{{ $t('agent.n_skills', { n: formSkills.length }) }}</Tag>
                <Tag>{{ $t('common.n_plugins', { n: formPlugins.length }) }}</Tag>
              </div>

              <Alert
                v-if="feedback"
                :type="feedback.type"
                show-icon
                closable
                class="detail-feedback"
                :message="feedback.text"
                @close="feedback = null"
              />

              <div class="editor-form">
                <div class="form-row">
                  <label
                    class="form-label"
                    for="agent-form-name"
                  >{{ $t('common.name_2') }}</label>
                  <Input
                    id="agent-form-name"
                    v-model:value="form.name"
                    :placeholder="$t('common.e_g_data_analyst')"
                    :maxlength="60"
                  />
                </div>
                <div class="form-row">
                  <label
                    class="form-label"
                    for="agent-form-desc"
                  >{{ $t('common.description') }}</label>
                  <Input
                    id="agent-form-desc"
                    v-model:value="form.description"
                    :placeholder="$t('agent.describe_the_agent_s_responsibility_in_one_sentence')"
                    :maxlength="200"
                  />
                </div>
                <div class="form-row">
                  <label
                    class="form-label"
                    for="agent-form-prompt"
                  >{{ $t('agent.system_prompt') }}</label>
                  <Input.TextArea
                    id="agent-form-prompt"
                    v-model:value="form.system_prompt"
                    :rows="5"
                    :placeholder="$t('agent.define_the_agent_s_role_behavior_and_goals')"
                  />
                </div>
                <div class="form-grid">
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-model"
                    >{{ $t('agent.model') }}</label>
                    <Input
                      id="agent-form-model"
                      v-model:value="form.model"
                      placeholder="deepseek-chat"
                    />
                  </div>
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-turns"
                    >{{ $t('common.max_rounds') }}</label>
                    <InputNumber
                      id="agent-form-turns"
                      v-model:value="form.max_turns"
                      :min="1"
                      :max="30"
                      class="w-full"
                    />
                  </div>
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-timeout"
                    >{{ $t('errors.timeout_seconds') }}</label>
                    <InputNumber
                      id="agent-form-timeout"
                      v-model:value="form.timeout_seconds"
                      :min="10"
                      :max="3600"
                      class="w-full"
                    />
                  </div>
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-temp"
                    >Temperature</label>
                    <InputNumber
                      id="agent-form-temp"
                      v-model:value="form.temperature"
                      :min="0"
                      :max="2"
                      :step="0.1"
                      class="w-full"
                    />
                  </div>
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-tokens"
                    >Max Tokens</label>
                    <InputNumber
                      id="agent-form-tokens"
                      v-model:value="form.max_tokens"
                      :min="256"
                      :max="32768"
                      :step="512"
                      class="w-full"
                    />
                  </div>
                  <div class="form-field">
                    <label
                      class="form-label"
                      for="agent-form-enabled"
                    >{{ $t('common.enable') }}</label>
                    <Switch
                      id="agent-form-enabled"
                      v-model:checked="form.enabled"
                    />
                  </div>
                </div>
                <div class="form-row">
                  <label
                    class="form-label"
                    for="agent-form-tools"
                  >{{ $t('agent.tools_json') }}</label>
                  <Input.TextArea
                    id="agent-form-tools"
                    v-model:value="form.tools_text"
                    :rows="4"
                    :placeholder="$t('common.jsonexample', { jsonExample: '[{&quot;name&quot;:&quot;shell_exec&quot;,&quot;description&quot;:&quot;执行命令&quot;,&quot;parameters&quot;:{&quot;type&quot;:&quot;object&quot;,&quot;properties&quot;:{}}}]' })"
                    class="tools-input"
                  />
                  <ToolPicker v-model="form.tools_text" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ $t('knowledge.default_knowledge_base') }}</label>
                  <Select
                    v-model:value="formKbId"
                    :options="kbOptions.map(b => ({ value: b.id, label: b.name }))"
                    :loading="bindingLoading"
                    :placeholder="$t('knowledge.use_it_for_retrieval_when_dispatching_optional')"
                    allow-clear
                    show-search
                    option-filter-prop="label"
                    class="binding-select"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ $t('agent.skill') }}</label>
                  <Select
                    v-model:value="formSkills"
                    mode="multiple"
                    :options="skillOptions.map(s => ({ value: s.name, label: s.name }))"
                    :loading="bindingLoading"
                    :placeholder="$t('agent.only_enable_selected_skills_blank_all_installed')"
                    allow-clear
                    show-search
                    option-filter-prop="label"
                    class="binding-select"
                  />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ $t('common.plugin') }}</label>
                  <Select
                    v-model:value="formPlugins"
                    mode="multiple"
                    :options="pluginOptions.map(p => ({ value: p.name, label: p.name }))"
                    :loading="bindingLoading"
                    :placeholder="$t('agent.only_tools_from_these_plugins_blank_no_filter')"
                    allow-clear
                    show-search
                    option-filter-prop="label"
                    class="binding-select"
                  />
                </div>
              </div>

              <div class="detail-footer">
                <span
                  class="dirty-flag"
                  :class="{ dirty: isDirty }"
                >
                  {{ isDirty ? $t('common.unsaved') : $t('agent.all_changes_saved') }}
                </span>
                <div class="footer-actions">
                  <Button @click="closeDetail">
                    {{ $t('common.cancel') }}
                  </Button>
                  <Button
                    type="primary"
                    :loading="editorSaving"
                    :disabled="!!selectedAgent && !canManage(selectedAgent)"
                    :title="selectedAgent && !canManage(selectedAgent) ? $t('agent.only_creator_can_edit') : ''"
                    @click="saveEditor"
                  >
                    {{ $t('common.save') }}
                  </Button>
                </div>
              </div>
            </template>
          </section>
        </div>
      </TabPane>

      <!-- ── Tab 2：运行记录 ── -->
      <TabPane
        key="sessions"
        :tab="$t('common.run_history')"
      >
        <PageSkeleton
          v-if="loadingSessions"
          variant="list"
          :rows="6"
          :header="false"
        />
        <EmptyState
          v-else-if="errorSessions"
          size="page"
          :icon="markRaw(HistoryOutlined)"
          :description="$t('errors.failed_to_load')"
          :hint="$t('common.unable_to_fetch_run_history_please_retry_later')"
        >
          <Button
            type="primary"
            @click="loadSessions"
          >
            {{ $t('common.retry') }}
          </Button>
        </EmptyState>
        <EmptyState
          v-else-if="sessions.length === 0"
          size="page"
          :icon="markRaw(HistoryOutlined)"
          :description="$t('common.no_run_history_yet')"
          :hint="$t('workflow.after_dispatching_a_task_from_my_agents_run_results_appear_here')"
        />
        <div
          v-else
          class="session-list"
        >
          <div
            v-for="s in sessions"
            :key="s.id"
            class="session-row"
          >
            <span
              class="session-status-icon"
              :class="s.status"
            >
              <component
                :is="statusMeta[s.status]?.icon"
                v-if="statusMeta[s.status]"
              />
            </span>
            <div class="session-main">
              <div class="session-task">
                {{ s.task }}
              </div>
              <div class="session-meta">
                <Tag :color="statusMeta[s.status]?.color">
                  {{ statusMeta[s.status]?.label || s.status }}
                </Tag>
                <span
                  v-if="s.agent_name"
                  class="session-agent"
                >{{ s.agent_name }}</span>
                <span class="session-time">{{ formatTime(s.created_at) }}</span>
              </div>
            </div>
            <Button
              size="small"
              type="text"
              @click="openDetail(s)"
            >
              {{ $t('common.view_result') }}
            </Button>
          </div>
        </div>
      </TabPane>

      <!-- ── Tab 3：Super-agent 接管 ──
           放在最后而不是第一个：接管是「配置一次、长期放着」的东西，
           日常高频操作是前两个 Tab。放在首位会让每次进页面都多一次视觉负担。 -->
      <TabPane
        key="takeover"
        :tab="$t('agent.superagent_title')"
      >
        <TakeoverPanel />
      </TabPane>
    </Tabs>

    <!-- ── 运行 Modal ── -->
    <Modal
      :open="runOpen"
      :title="$t('common.run_name', { name: runTarget?.name || '' })"
      :footer="null"
      :closable="true"
      width="720px"
      @cancel="closeRun"
    >
      <Input.TextArea
        v-model:value="runTask"
        :rows="3"
        :placeholder="$t('workflow.describe_the_task_to_complete')"
        :disabled="!!runSession && runSession.status === 'running'"
      />
      <div class="run-actions">
        <Button
          type="primary"
          :loading="runSubmitting"
          :disabled="!!runSession && (runSession.status === 'running' || runSession.status === 'pending')"
          @click="submitRun"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ $t('workflow.dispatch_task') }}
        </Button>
      </div>

      <div
        v-if="runSession"
        class="run-result"
      >
        <Alert
          v-if="runSession.status === 'running' || runSession.status === 'pending'"
          type="info"
          show-icon
          :message="runSession.status === 'pending' ? $t('workflow.task_queued') : $t('agent.agent_is_running_llm_reasoning_tool_calls')"
        />
        <Alert
          v-else-if="runSession.status === 'failed'"
          type="error"
          show-icon
          :message="$t('errors.execution_failed')"
          :description="parseResult(runSession).error || parseResult(runSession).output || $t('errors.unknown_error')"
        />
        <template v-else-if="runSession.status === 'completed'">
          <Alert
            type="success"
            show-icon
            :message="$t('common.execution_complete')"
          />
          <div class="result-block">
            <div class="result-label">
              {{ $t('common.output_2') }}
            </div>
            <pre class="result-output">{{ parseResult(runSession).output || $t('common.no_output') }}</pre>
          </div>
          <div
            v-if="parseResult(runSession).token_usage || parseResult(runSession).duration || parseResult(runSession).tool_calls?.length"
            class="result-meta"
          >
            <Tag v-if="parseResult(runSession).token_usage">
              tokens: {{ JSON.stringify(parseResult(runSession).token_usage) }}
            </Tag>
            <Tag v-if="parseResult(runSession).duration">
              {{ $t('common.elapsed_n_s', { n: (parseResult(runSession).duration ?? 0).toFixed(1) }) }}
            </Tag>
            <Tag v-if="parseResult(runSession).tool_calls?.length">
              {{ $t('agent.tool_calls_n', { n: (parseResult(runSession).tool_calls ?? []).length }) }}
            </Tag>
          </div>
          <div class="result-actions">
            <Button
              ghost
              @click="openSaveToKb(runSession)"
            >
              {{ $t('knowledge.save_to_knowledge_base') }}
            </Button>
          </div>
        </template>
      </div>
    </Modal>

    <!-- ── 历史结果详情 Modal ── -->
    <Modal
      :open="detailOpen"
      :title="$t('common.run_result_name', { name: detailSession?.agent_name || '' })"
      :footer="null"
      width="720px"
      @cancel="detailOpen = false"
    >
      <div
        v-if="detailSession"
        class="run-result"
      >
        <div class="session-meta">
          <Tag :color="statusMeta[detailSession.status]?.color">
            {{ statusMeta[detailSession.status]?.label || detailSession.status }}
          </Tag>
          <span class="session-time">{{ formatTime(detailSession.created_at) }}</span>
        </div>
        <Alert
          v-if="detailSession.status === 'failed'"
          type="error"
          show-icon
          :message="$t('errors.execution_failed')"
          :description="parseResult(detailSession).error || parseResult(detailSession).output || $t('errors.unknown_error')"
        />
        <div class="result-block">
          <div class="result-label">
            {{ $t('workflow.task_2') }}
          </div>
          <pre class="result-output">{{ detailSession.task }}</pre>
        </div>
        <div
          v-if="parseResult(detailSession).output"
          class="result-block"
        >
          <div class="result-label">
            {{ $t('common.output_2') }}
          </div>
          <pre class="result-output">{{ parseResult(detailSession).output }}</pre>
        </div>
        <div
          v-if="parseResult(detailSession).token_usage || parseResult(detailSession).duration || parseResult(detailSession).tool_calls?.length"
          class="result-meta"
        >
          <Tag v-if="parseResult(detailSession).token_usage">
            tokens: {{ JSON.stringify(parseResult(detailSession).token_usage) }}
          </Tag>
          <Tag v-if="parseResult(detailSession).duration">
            {{ $t('common.elapsed_n_s', { n: (parseResult(detailSession).duration ?? 0).toFixed(1) }) }}
          </Tag>
          <Tag v-if="parseResult(detailSession).tool_calls?.length">
            {{ $t('agent.tool_calls_n', { n: (parseResult(detailSession).tool_calls ?? []).length }) }}
          </Tag>
        </div>
        <div class="result-actions">
          <Button
            ghost
            @click="openSaveToKb(detailSession)"
          >
            {{ $t('knowledge.save_to_knowledge_base') }}
          </Button>
          <Button
            type="primary"
            ghost
            @click="continueInChat"
          >
            {{ $t('chat.continue_in_the_conversation') }}
          </Button>
        </div>
      </div>
    </Modal>

    <!-- ── 存入知识库（复用对话侧的组件与分片上传链路）── -->
    <SaveToKnowledgeDialog
      v-model:open="saveToKbOpen"
      :content="saveToKbContent"
      :default-title="saveToKbTitle"
    />
  </div>
</template>

<style scoped>
.agents-page { max-width: 1200px; margin: 0 auto; padding: 28px 24px 60px; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: var(--fs-sm); }
.agents-tabs :deep(.ant-tabs-nav) { margin-bottom: 16px; }

/* ── 两栏工作台 ── */
.workspace {
  display: grid;
  grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.pane {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-xl);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  min-width: 0;
}
.pane-list { display: flex; flex-direction: column; overflow: hidden; }
.pane-head {
  display: flex; flex-direction: column; gap: 10px;
  padding: 14px;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.pane-search { width: 100%; }
.group-nav { display: flex; flex-wrap: wrap; gap: 6px; }
.group-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 8px;
  border: var(--hairline-width) solid var(--border-subtle);
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background var(--dur-fast) ease, border-color var(--dur-fast) ease, color var(--dur-fast) ease;
}
.group-chip:hover { border-color: var(--primary); color: var(--text-primary); }
.group-chip:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
.group-chip.active {
  background: var(--primary-bg);
  border-color: var(--primary);
  color: var(--primary);
}
.group-count {
  min-width: 18px;
  padding: 0 4px;
  border-radius: var(--radius-full);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  text-align: center;
}
.group-chip.active .group-count { background: var(--bg-card); color: var(--primary); }

.pane-body {
  padding: 8px;
  max-height: min(62vh, 720px);
  overflow-y: auto;
}
.item-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.item {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px;
  border: var(--hairline-width) solid transparent;
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: background var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.item:hover { background: var(--bg-secondary); }
.item:focus-visible { outline: 2px solid var(--primary); outline-offset: -1px; }
.item.active {
  background: var(--primary-bg);
  border-color: color-mix(in srgb, var(--primary) 40%, transparent);
}
.item.muted .item-name { color: var(--text-tertiary); }
.item-avatar {
  flex: none; width: 30px; height: 30px; border-radius: var(--radius-md);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary-bg); color: var(--primary); font-size: var(--fs-md);
}
.item-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.item-title-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.item-name {
  font-size: var(--fs-base); font-weight: var(--fw-medium); color: var(--text-primary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.item-tag { flex: none; margin-inline-end: 0; }
.item-desc {
  font-size: var(--fs-xs); color: var(--text-tertiary); line-height: var(--lh-normal);
  overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.item-meta { display: flex; align-items: center; gap: 4px; font-size: var(--fs-xs); color: var(--text-tertiary); }
.item-meta-dot { opacity: 0.6; }

/* ── 右栏详情 ── */
.pane-detail { padding: 18px 20px 20px; display: flex; flex-direction: column; gap: 14px; }
.back-btn {
  display: none; align-items: center; gap: 6px;
  align-self: flex-start;
  padding: 2px 0;
  border: none; background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-sm); cursor: pointer;
}
.back-btn:hover { color: var(--primary); }
.back-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.detail-placeholder { padding: 24px 0; }
.detail-head { display: flex; align-items: flex-start; gap: 12px; }
.detail-avatar {
  flex: none; width: 42px; height: 42px; border-radius: var(--sig-radius-card);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary-bg); color: var(--primary); font-size: var(--fs-2xl);
}
.detail-titles { flex: 1; min-width: 0; }
.detail-name {
  margin: 0;
  font-size: var(--fs-lg); font-weight: var(--fw-semibold); color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.detail-sub {
  margin: 3px 0 0;
  font-size: var(--fs-sm); color: var(--text-tertiary); line-height: var(--lh-normal);
}
.detail-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.detail-badges { display: flex; flex-wrap: wrap; gap: 4px; }
.detail-feedback { margin: 0; }
.card-menu { min-width: 150px; border-radius: var(--sig-radius-card); padding: 4px; box-shadow: var(--shadow-lg); }
.card-menu :deep(.ant-dropdown-menu-item) { display: flex; align-items: center; gap: 8px; font-size: var(--fs-sm); border-radius: var(--radius-md); }
.menu-icon { font-size: var(--fs-base); }

/* ── 表单 ── */
.editor-form { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; flex-direction: column; gap: 6px; }
.form-label { font-size: var(--fs-xs); font-weight: var(--fw-medium); color: var(--text-secondary); }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.form-field { display: flex; flex-direction: column; gap: 6px; }
.w-full { width: 100%; }
.binding-select { width: 100%; }
.tools-input :deep(textarea) { font-family: var(--font-mono); font-size: var(--fs-sm); }
.detail-footer {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding-top: 12px;
  border-top: var(--hairline-width) solid var(--border-subtle);
}
.dirty-flag { font-size: var(--fs-xs); color: var(--text-tertiary); }
.dirty-flag.dirty { color: var(--warning); }
.footer-actions { display: flex; align-items: center; gap: 8px; }

/* ── 运行记录 ── */
.session-list { display: flex; flex-direction: column; gap: 8px; }
.session-row {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
}
.session-status-icon { flex: none; width: 30px; height: 30px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: var(--fs-base); }
.session-status-icon.pending { background: var(--bg-secondary); color: var(--text-tertiary); }
.session-status-icon.running { background: var(--primary-bg); color: var(--primary); }
.session-status-icon.completed { background: var(--success-bg); color: var(--success); }
.session-status-icon.failed { background: var(--error-bg); color: var(--error); }
.session-main { flex: 1; min-width: 0; }
.session-task { font-size: var(--fs-sm); color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-meta { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.session-agent { font-size: var(--fs-sm); color: var(--text-secondary); }
.session-time { font-size: var(--fs-sm); color: var(--text-tertiary); }

/* ── 运行结果 ── */
.run-actions { display: flex; justify-content: flex-end; margin-top: 12px; }
.run-result { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; }
.result-block { border: var(--hairline-width) solid var(--border-card); border-radius: var(--sig-radius-card); background: var(--bg-secondary); overflow: hidden; }
.result-label { padding: 8px 12px; font-size: var(--fs-sm); color: var(--text-tertiary); border-bottom: var(--hairline-width) solid var(--border-card); }
.result-output { margin: 0; padding: 12px; font-family: var(--font-mono); font-size: var(--fs-sm); line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; }
.result-meta { display: flex; flex-wrap: wrap; gap: 4px; }
.result-actions { margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px; }

/* ── 窄屏：单栏，列表为主；选中后进详情并带返回 ── */
@media (max-width: 900px) {
  .workspace { grid-template-columns: minmax(0, 1fr); }
  .workspace .pane-detail { display: none; }
  .workspace.has-selection .pane-list { display: none; }
  .workspace.has-selection .pane-detail { display: flex; }
  .back-btn { display: inline-flex; }
  .pane-body { max-height: none; }
}

@media (max-width: 640px) {
  .agents-page { padding: 20px 16px 48px; }
  .form-grid { grid-template-columns: 1fr; }
  .detail-actions { justify-content: flex-start; }
}

@media (max-width: 768px) {
  .session-row { flex-wrap: wrap; row-gap: 8px; }
  .session-main { flex-basis: calc(100% - 42px); }
  .session-task { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
}
</style>
