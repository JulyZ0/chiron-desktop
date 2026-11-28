<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Card,
  Button,
  Input,
  
  Slider,
  Select,
  Tabs,
  TabPane,
  List,
  Tag,
  Popconfirm,
  Spin,
  Modal,
  message,
  Badge,
  Alert,
  Tooltip,
  Space,
  Switch,
} from 'ant-design-vue'
import {
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  CommentOutlined,
  SwapOutlined,
} from '@ant-design/icons-vue'
import EmptyState from '../components/common/EmptyState.vue'
import ConflictCard from '../components/memory/ConflictCard.vue'
import { currentProject, setProject } from '../composables/useProject'
import { listProjects } from './sessionmap/projectsApi'
import {
  listMemory,
  upsertMemory,
  updateMemory,
  deleteMemory,
  clearMemory,
  searchMemory,
  moveMemoryScope,
  startOrganize,
  getOrganizeStatus,
  listConflicts,
  listSummaries,
  MEMORY_SLOTS,
  type MemoryEntry,
  type MemoryScope,
  type MemorySearchHit,
  type MemorySlot,
  type MemorySource,
  type OrganizeStatus,
  type MemorySearchSummary,
  type MemoryConflict,
  type MemorySummary,
} from '../api/memory'

import { useI18n } from 'vue-i18n'
import { serverErrorMessage, errorStatus } from '../utils/apiError'

const { t } = useI18n()
const router = useRouter()

const loading = ref(false)
const error = ref(false)
const entries = ref<MemoryEntry[]>([])
const counts = ref<Record<string, number>>({})
const total = ref(0)
const conflicts = ref<MemoryConflict[]>([])
const summaries = ref<MemorySummary[]>([])
const includeArchived = ref(false)

/** 浏览分区：all / 四个槽位 / 待裁决 / 摘要 */
type BrowseTab = 'all' | MemorySlot | 'conflicts' | 'summaries'
const activeTab = ref<BrowseTab>('all')
/** 只有 all 与四个槽位是「条目」分区，另两个渲染各自主角 */
const isEntryTab = computed(
  () => activeTab.value !== 'conflicts' && activeTab.value !== 'summaries',
)
const filteredEntries = computed(() =>
  activeTab.value === 'all' ? entries.value : entries.value.filter((e) => e.slot === activeTab.value),
)

// ── 语义检索 ──
const searchInput = ref('')
const searching = ref(false)
const searchingMode = ref<'semantic' | 'keyword'>('semantic')
// L3（历史摘要）的检索方式，与上面那条**不是一回事**（M22）：两条链路各算各的。
// `''` = 后端还没带这个字段（旧版本）/ 不该显示标签。
const searchingSummaryMode = ref<'semantic' | 'recent' | 'keyword' | 'empty' | ''>('')
const searchResults = ref<MemorySearchHit[] | null>(null)
const searchSummaries = ref<MemorySearchSummary[]>([])

async function handleSearch() {
  const q = searchInput.value.trim()
  if (!q) {
    clearSearch()
    return
  }
  searching.value = true
  try {
    const data = await searchMemory(q, { top_k: 10 })
    searchingMode.value = data.mode
    searchingSummaryMode.value = data.summary_mode ?? ''
    searchResults.value = data.results ?? []
    // L3 区做兜底：后端尚未升级到双区契约时，不该让整个检索崩在 .length 上。
    searchSummaries.value = data.summaries ?? []
    if (searchResults.value.length === 0 && searchSummaries.value.length === 0) {
      message.info(t('errors.no_similar_memory_found'))
    }
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.search_failed')))
  } finally {
    searching.value = false
  }
}

function clearSearch() {
  searchInput.value = ''
  searchResults.value = null
  searchSummaries.value = []
  searchingSummaryMode.value = ''
}

// L3 的检索方式 → 文案。**只显示有信息量的三种**：`empty`（没命中/L3 不可用）不显示 ——
// 一个"无模式"标签只会让人以为检索出问题了。
const summaryModeLabel = computed(() => {
  switch (searchingSummaryMode.value) {
    case 'semantic':
      return t('common.semantic_mode')
    case 'keyword':
      return t('common.keyword_mode')
    case 'recent':
      return t('common.recent_mode')
    default:
      return ''
  }
})

// ── 增删改 ──
const formVisible = ref(false)
const saving = ref(false)
const editing = ref<MemoryEntry | null>(null)
const form = ref<{
  slot: MemorySlot
  key: string
  value: string
  confidence: number
  source: MemorySource
  /** 写入范围：`global` = 所有项目通用；`project` = 当前项目 */
  scope: MemoryScope
}>({
  slot: 'fact',
  key: '',
  value: '',
  confidence: 50,
  source: 'user_confirmed',
  scope: 'project',
})

/**
 * 条目是否为全局。
 *
 * `scope` 缺失时按 `project` 是否为空串推断 —— 但**只认空串**：
 * 两个字段都没有（旧后端）时按"项目条目"处理，宁可多给一个编辑入口，
 * 也不要把整页记忆误锁成只读。
 */
function isGlobal(entry: MemoryEntry): boolean {
  if (entry.scope) return entry.scope === 'global'
  return entry.project === ''
}

/** 当前视图是否就是「全局」（项目名留空）。 */
const isGlobalView = computed(() => !project.value)

/**
 * 项目视图里全局条目**只读**：改它会波及所有项目，入口收在全局视图。
 *
 * 不用 computed 按条目缓存 —— 只是个布尔推导，模板里直接调用更直观。
 */
function isReadOnly(entry: MemoryEntry): boolean {
  return !isGlobalView.value && isGlobal(entry)
}

function openCreate(slot?: MemorySlot) {
  editing.value = null
  form.value = {
    slot: slot ?? 'fact',
    key: '',
    value: '',
    confidence: 50,
    source: 'user_confirmed',
    // 新建默认落在当前视图：在项目里建的默认属于该项目，在全局视图建的默认全局
    scope: isGlobalView.value ? 'global' : 'project',
  }
  formVisible.value = true
}

function openEdit(entry: MemoryEntry) {
  editing.value = entry
  form.value = {
    slot: entry.slot as MemorySlot,
    key: entry.key,
    value: entry.value,
    confidence: entry.confidence,
    source: entry.source as MemorySource,
    scope: isGlobal(entry) ? 'global' : 'project',
  }
  formVisible.value = true
}

async function handleSave() {
  const { slot, key, value, confidence, source, scope } = form.value
  if (!key.trim() || !value.trim()) {
    message.warning(t('common.key_and_content_cannot_be_empty'))
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      // 编辑是"就地改内容"，范围由后端按当前 `?project=` 精确匹配 —— 不在这里换范围
      // （换范围走 moveScope，语义不同：它要跨范围动作）。
      await updateMemory({ id: editing.value.id, key, value, confidence, source })
      message.success(t('memory.memory_updated'))
    } else {
      // 新建时范围可覆盖当前视图：在项目里也能直接建一条"全局"记忆
      const res = await upsertMemory(
        { slot, key, value, confidence, source },
        { project: scope === 'global' ? '' : project.value },
      )
      if (res.duplicate_of) {
        message.warning(t('memory.similar_memory_key_detected_auto_merged_during_cleanup', { key: res.duplicate_of.key }))
      } else {
        message.success(t('memory.memory_saved'))
      }
    }
    formVisible.value = false
    await loadProfile()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.save_failed')))
  } finally {
    saving.value = false
  }
}

/**
 * 把一条**项目专属**记忆提升为全局记忆（`project` → 空）。
 *
 * 界面只给这一个方向：后端 `move_entry_scope` 要求 `from_project` 等于条目当前所属范围
 * （那是权限边界），所以项目视图只能动本项目的条目，全局视图只能动全局条目。
 * 而全局视图里没有"目标项目"的来源 —— 项目名是用户自由输入的，界面上此刻为空；
 * 替用户猜一个项目等于把一条所有项目共用的记忆悄悄绑到其中一个上。
 * 因此「全局 → 项目」不提供入口，需要时在目标项目的视图里新建即可。
 */
async function makeGlobal(entry: MemoryEntry) {
  try {
    await moveMemoryScope(entry.id, '')
    message.success(t('memory.memory_scope_switched'))
    await loadProfile()
  } catch (e) {
    // 409：目标范围已有同 (slot, key) 的条目，后端不会静默覆盖
    if (errorStatus(e) === 409) {
      message.error(t('memory.target_scope_already_has_a_memory_with_the_same_key'))
    } else {
      message.error(serverErrorMessage(e, t('errors.save_failed')))
    }
  }
}

async function handleDelete(id: string) {
  try {
    await deleteMemory(id)
    message.success(t('common.deleted'))
    await loadProfile()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.delete_failed')))
  }
}

async function handleClearAll() {
  try {
    const res = await clearMemory()
    message.success(t('memory.cleared_n_memories', { n: res.deleted }))
    await loadProfile()
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_clear')))
  }
}

// ── 智能整理（异步 + 轮询） ──
const organize = ref<OrganizeStatus>({
  running: false,
  started_at: null,
  finished_at: null,
  result: null,
  error: null,
})
let organizeTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (organizeTimer) {
    clearInterval(organizeTimer)
    organizeTimer = null
  }
}

async function pollOrganize() {
  stopPolling()
  organizeTimer = setInterval(async () => {
    try {
      const st = await getOrganizeStatus()
      if (!st) {
        // 空响应（接口异常 / 降级）不能写进 `organize`：模板按 `organize.running` 渲染，
        // undefined 会直接把整页炸掉。停在"未知"即可，下次操作还会再查。
        stopPolling()
        return
      }
      organize.value = st
      if (!st.running) {
        stopPolling()
        if (st.result) {
          const r = st.result
          const parts: string[] = []
          if (r.merged) parts.push(t('common.merged_n_duplicates', { n: r.merged }))
          if (r.backfilled) parts.push(t('knowledge.backfilled_n_vectors', { n: r.backfilled }))
          if (r.archived) parts.push(t('common.archived_n', { n: r.archived }))
          if (r.evicted) parts.push(t('common.evicted_n', { n: r.evicted }))
          if (parts.length) message.success(t('common.organization_complete') + parts.join('，'))
          else message.success(t('common.organization_complete_no_adjustment_needed'))
          await loadProfile()
        } else if (st.error) {
          message.error(t('errors.organization_failed') + st.error)
        }
      }
    } catch {
      stopPolling()
    }
  }, 1200)
}

async function runOrganize() {
  try {
    const res = await startOrganize()
    organize.value = res.status
    if (res.started) {
      message.info(t('common.smart_organization_started_runs_in_background'))
      pollOrganize()
    } else {
      message.info(t('workflow.an_organization_task_is_already_running'))
      pollOrganize()
    }
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_start_organization')))
  }
}

// ── 项目（C3）──
// 记忆按 `tenant + user + project` 隔离：**空 = 全局**（所有项目通用），具名项目互不可见。
// 切换项目必须**重新加载**：否则界面上留着上一个项目的条目，用户会以为它们还在
// （"隔离"看起来生效而其实串了）。
const project = currentProject

/**
 * 真实项目列表（session-map workspaces）。
 *
 * 记忆的 `project` 维现在统一用 **workspace id**（对话侧由网关按会话归属注入同一个 id），
 * 所以这里必须是选择器而不是自由输入 —— 手填的名字与 id 对不上，隔离看起来"配了却无效"。
 * 旧数据里若存的是项目名，加载时按名字匹配一次迁移到 id；匹配不上就原样保留，
 * 以免把用户的历史范围静默清空。
 */
const projects = ref<{ id: string; name: string; root_path: string }[]>([])

const projectOptions = computed(() => {
  const opts = projects.value.map((p) => ({
    value: p.id,
    label: p.name || p.root_path?.split(/[\\/]/).filter(Boolean).pop() || p.id,
  }))
  // 旧版存的自由串：仍允许查看，但只出现在它自己的选项里
  const stored = (project.value || '').trim()
  if (stored && !opts.some((o) => o.value === stored)) {
    opts.push({ value: stored, label: stored })
  }
  return opts
})

async function loadProjects() {
  try {
    const list = await listProjects()
    projects.value = list.map((p) => ({ id: p.id, name: p.name || '', root_path: p.root_path || '' }))
    const stored = (project.value || '').trim()
    if (stored && !projects.value.some((p) => p.id === stored)) {
      const byName = projects.value.find((p) => p.name === stored)
      if (byName) setProject(byName.id)
    }
  } catch {
    // 项目列表不可用不阻断记忆浏览：保留手头已有的范围
    projects.value = []
  }
}

function onProjectSelect(value: unknown) {
  const next = typeof value === 'string' || typeof value === 'number' ? String(value) : ''
  setProject(next)
  void loadProfile()
  void loadConflicts()
}

// ── 加载 ──
async function loadProfile() {
  loading.value = true
  error.value = false
  try {
    const data = await listMemory(includeArchived.value)
    entries.value = data.entries || []
    counts.value = data.counts || {}
    total.value = data.total
    organize.value = data.organize || organize.value
  } catch (e) {
    error.value = true
    if (errorStatus(e) === 503) {
      message.error(t('errors.memory_service_unavailable_postgresql_required'))
    } else {
      message.error(serverErrorMessage(e, t('errors.failed_to_load_memory')))
    }
  } finally {
    loading.value = false
  }
}

async function loadConflicts() {
  try {
    const data = await listConflicts()
    conflicts.value = data.conflicts || []
  } catch {
    // 冲突列表不可用不该影响记忆条目浏览
    conflicts.value = []
  }
}

async function loadSummaries() {
  try {
    const data = await listSummaries()
    summaries.value = data.summaries || []
  } catch {
    summaries.value = []
  }
}

/** 裁决或忽略后从待办里移除（后端已消化，重复展示会让用户以为没生效） */
function onConflictResolved(conflictId: string) {
  conflicts.value = conflicts.value.filter((c) => c.conflict_id !== conflictId)
}

/**
 * 带着这条记忆的分类跳到对话（`?memory=<slot>`）：对话侧据此收窄注入范围。
 * 单条记忆不单独传 id —— 注入粒度是「分类」，传 id 会让后端需要另一套白名单语义。
 */
function useSlotInChat(slot: string) {
  void router.push({ path: '/chat', query: { memory: slot } })
}

// antd Switch 的 change 回调首个参数是 CheckedType（boolean | string | number）
async function onArchivedChange(value: boolean | string | number) {
  includeArchived.value = Boolean(value)
  await loadProfile()
}

onMounted(() => {
  void loadProjects()
  void loadProfile()
  void loadConflicts()
  void loadSummaries()
})
onUnmounted(stopPolling)

function slotLabel(slot: string): string {
  return MEMORY_SLOTS.find((s) => s.slot === slot)?.label ?? slot
}
function pct(n: number): string {
  return Math.round(n * 100) + '%'
}
</script>

<template>
  <div class="memory-view">
    <div class="page-header">
      <div class="title">
        <DatabaseOutlined />
        <span>{{ $t('memory.long_term_memory') }}</span>
        <Badge
          :count="total"
          :overflow-count="999"
          color="var(--node-indigo)"
        />
        <span class="subtitle">{{ $t('knowledge.cross_session_retention_semantic_retrieval_auto_organization') }}</span>
      </div>
      <Space>
        <Select
          :value="project"
          :options="projectOptions"
          :placeholder="$t('memory.project_placeholder')"
          :title="$t('memory.project_hint')"
          allow-clear
          show-search
          style="width: 220px"
          data-test="project-input"
          @change="onProjectSelect"
        ></Select>
        <Switch
          :checked="includeArchived"
          :checked-children="$t('common.include_archived')"
          :un-checked-children="$t('common.active_only')"
          @change="onArchivedChange"
        />
        <Button
          :loading="organize.running"
          @click="runOrganize"
        >
          <template #icon>
            <ThunderboltOutlined />
          </template>
          {{ $t('common.smart_organization') }}
        </Button>
        <Popconfirm
          :title="$t('memory.confirm_clearing_all_long_term_memory_this_action_cannot_be_undone')"
          :ok-text="$t('common.clear_2')"
          :cancel-text="$t('common.cancel')"
          @confirm="handleClearAll"
        >
          <Button danger>
            {{ $t('memory.clear_memory') }}
          </Button>
        </Popconfirm>
        <Button
          type="primary"
          @click="openCreate()"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('memory.new_memory') }}
        </Button>
      </Space>
    </div>

    <!-- 整理状态 -->
    <Alert
      v-if="organize.running"
      type="info"
      show-icon
      :message="$t('knowledge.cleaning_up_memory_in_the_background_dedupe_archive_backfill_vectors')"
      style="margin-bottom: 16px"
    />
    <Alert
      v-else-if="organize.result"
      type="success"
      show-icon
      style="margin-bottom: 16px"
      :message="$t('common.last_cleanup_merged_merged_backfilled_backfilled_archived_archived_evicted_evicted', { merged: organize.result.merged, backfilled: organize.result.backfilled, archived: organize.result.archived, evicted: organize.result.evicted })"
    />

    <!-- 语义检索 -->
    <Card
      class="search-card"
      :bordered="true"
    >
      <Space
        style="width: 100%"
        wrap
      >
        <Input
          v-model:value="searchInput"
          :placeholder="$t('knowledge.semantic_retrieval_e_g_what_editor_does_the_user_prefer')"
          style="width: 360px"
          allow-clear
          @press-enter="handleSearch"
          @search="handleSearch"
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <Button
          type="primary"
          :loading="searching"
          @click="handleSearch"
        >
          {{ $t('knowledge.smart_retrieval') }}
        </Button>
        <Button
          v-if="searchResults !== null"
          @click="clearSearch"
        >
          {{ $t('common.back_to_list') }}
        </Button>
        <Tag
          v-if="searchResults !== null"
          :color="searchingMode === 'semantic' ? 'blue' : 'orange'"
        >
          {{ searchingMode === 'semantic' ? $t('common.semantic_mode') : $t('common.keyword_mode') }}
        </Tag>
      </Space>
    </Card>

    <!-- 检索结果 -->
    <div
      v-if="searchResults !== null"
      class="results"
    >
      <h3
        v-if="searchResults.length"
        class="result-section-title"
      >
        {{ $t('memory.memory_entries_n', { n: searchResults.length }) }}
      </h3>
      <List
        :data-source="searchResults"
        :locale="{ emptyText: $t('errors.no_similar_memory_found') }"
      >
        <template #renderItem="{ item }">
          <List.Item>
            <Card
              class="entry-card"
              size="small"
              style="width: 100%"
            >
              <div class="entry-head">
                <Tag color="purple">
                  {{ slotLabel(item.slot) }}
                </Tag>
                <span class="entry-key">{{ item.key }}</span>
                <Tooltip :title="$t('common.relevance_rel_rerank_score_score', { rel: pct(item.similarity), score: item.score.toFixed(2) })">
                  <Tag color="green">
                    {{ pct(item.score) }}
                  </Tag>
                </Tooltip>
              </div>
              <div class="entry-value">
                {{ item.value }}
              </div>
            </Card>
          </List.Item>
        </template>
      </List>

      <h3
        v-if="searchSummaries.length"
        class="result-section-title"
      >
        {{ $t('chat.history_n', { n: searchSummaries.length }) }}
        <!-- M22：L3 自己报的检索方式。它与上方那个（L2 的）标签各说各话：
             同一句话可以 L2 走关键词、L3 走向量，用户需要知道"这批摘要是怎么被选出来的"。 -->
        <Tag
          v-if="summaryModeLabel"
          class="summary-mode-tag"
          :color="searchingSummaryMode === 'semantic' ? 'blue' : 'default'"
        >
          {{ summaryModeLabel }}
        </Tag>
      </h3>
      <List
        :data-source="searchSummaries"
        :locale="{ emptyText: $t('errors.no_related_conversation_found') }"
      >
        <template #renderItem="{ item }">
          <List.Item>
            <Card
              class="entry-card"
              size="small"
              style="width: 100%"
            >
              <div class="entry-head">
                <Tag color="cyan">
                  {{ $t('chat.history_conversations') }}
                </Tag>
                <Tag
                  v-if="item.topics && item.topics.length"
                  color="default"
                >
                  {{ item.topics.slice(0, 3).join(' ') }}
                </Tag>
              </div>
              <div class="entry-value">
                {{ item.content }}
              </div>
            </Card>
          </List.Item>
        </template>
      </List>

      <EmptyState
        v-if="!searchResults.length && !searchSummaries.length"
        size="page"
        :icon="markRaw(SearchOutlined)"
        :description="$t('errors.no_similar_memory_found')"
        :hint="$t('memory.try_rephrasing_or_create_memory_above_first')"
      />
    </div>

    <!-- 浏览（按槽位 Tab） -->
    <Spin
      v-else
      :spinning="loading"
    >
      <Tabs v-model:active-key="activeTab">
        <TabPane key="all">
          <template #tab>
            {{ $t('common.all_n', { n: total }) }}
          </template>
        </TabPane>
        <TabPane
          v-for="s in MEMORY_SLOTS"
          :key="s.slot"
        >
          <template #tab>
            {{ s.label }} ({{ counts[s.slot] || 0 }})
          </template>
        </TabPane>
        <TabPane key="conflicts">
          <template #tab>
            {{ $t('common.pending_decision_n', { n: conflicts.length }) }}
          </template>
        </TabPane>
        <TabPane key="summaries">
          <template #tab>
            {{ $t('common.summary_n', { n: summaries.length }) }}
          </template>
        </TabPane>
      </Tabs>

      <!-- 条目浏览（全部 / 分类） -->
      <template v-if="isEntryTab">
        <EmptyState
          v-if="error && !loading"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('errors.failed_to_load')"
          :hint="$t('memory.unable_to_fetch_memory_data_please_retry_later')"
        >
          <Button
            type="primary"
            @click="loadProfile"
          >
            {{ $t('common.retry') }}
          </Button>
        </EmptyState>

        <EmptyState
          v-else-if="filteredEntries.length === 0 && !loading"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('memory.no_memory_in_this_category_yet')"
          :hint="$t('memory.click_new_memory_at_the_top_right_to_record_user_preferences_and_long_term_context')"
        />

        <List
          v-else
          :data-source="filteredEntries"
        >
          <template #renderItem="{ item }">
            <List.Item>
              <Card
                class="entry-card"
                size="small"
                style="width: 100%"
              >
                <div class="entry-head">
                  <Tag color="purple">
                    {{ item.slot_label }}
                  </Tag>
                  <Tag
                    :color="isGlobal(item) ? 'gold' : 'geekblue'"
                    data-test="entry-scope"
                  >
                    {{ isGlobal(item) ? $t('memory.scope_global') : $t('memory.scope_project') }}
                  </Tag>
                  <span class="entry-key">{{ item.key }}</span>
                  <Tag :color="item.source === 'user_confirmed' ? 'green' : 'default'">
                    {{ item.source_label }}
                  </Tag>
                  <Tooltip :title="$t('common.confidence_high_confidence_items_are_kept_first_during_organization')">
                    <Tag color="blue">
                      {{ $t('common.confidence_n', { n: item.confidence }) }}
                    </Tag>
                  </Tooltip>
                  <Space class="entry-actions">
                    <Button
                      size="small"
                      type="text"
                      :title="$t('memory.use_this_type_of_memory_in_the_conversation')"
                      @click="useSlotInChat(item.slot)"
                    >
                      <template #icon>
                        <CommentOutlined />
                      </template>
                    </Button>
                    <!-- 换范围：只对**本项目专属**的条目开放。
                         全局条目在项目视图里只读（后端按 `from_project` 精确匹配，
                         从项目视图发请求会被 404 拒绝），而全局视图下又推不出"改成哪个项目"，
                         所以两个方向里只有「项目 → 全局」有明确入口。 -->
                    <Tooltip
                      v-if="!isGlobalView && !isGlobal(item)"
                      :title="$t('memory.move_to_global')"
                    >
                      <Button
                        size="small"
                        type="text"
                        data-test="toggle-scope"
                        @click="makeGlobal(item)"
                      >
                        <template #icon>
                          <SwapOutlined />
                        </template>
                      </Button>
                    </Tooltip>
                    <Button
                      v-if="!isReadOnly(item)"
                      size="small"
                      type="text"
                      :title="$t('common.edit_2')"
                      @click="openEdit(item)"
                    >
                      <template #icon>
                        <EditOutlined />
                      </template>
                    </Button>
                    <!-- 全局条目在项目视图里只读：给出原因，而不是留一个点了没反应的按钮 -->
                    <Tooltip
                      v-else
                      :title="$t('memory.global_entries_are_read_only_in_project_view_switch_to_global_scope_to_edit')"
                    >
                      <Button
                        size="small"
                        type="text"
                        disabled
                        data-test="edit-disabled"
                      >
                        <template #icon>
                          <EditOutlined />
                        </template>
                      </Button>
                    </Tooltip>
                    <Popconfirm
                      v-if="!isReadOnly(item)"
                      :title="$t('memory.delete_this_memory')"
                      :ok-text="$t('common.delete')"
                      :cancel-text="$t('common.cancel')"
                      @confirm="handleDelete(item.id)"
                    >
                      <Button
                        size="small"
                        type="text"
                        danger
                        :title="$t('common.delete')"
                      >
                        <template #icon>
                          <DeleteOutlined />
                        </template>
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
                <div class="entry-value">
                  {{ item.value }}
                </div>
                <div class="entry-meta">
                  {{ $t('common.accessed_n_times_updated_date', { n: item.access_count, date: item.updated_at?.slice(0, 10) }) }}
                </div>
              </Card>
            </List.Item>
          </template>
        </List>
      </template>

      <!-- 待裁决冲突 -->
      <template v-else-if="activeTab === 'conflicts'">
        <EmptyState
          v-if="conflicts.length === 0"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('errors.no_pending_memory_conflicts_to_resolve')"
          :hint="$t('errors.when_the_content_the_ai_distilled_conflicts_with_info_you_confirmed_it_shows_up_here_for_your_decision')"
        />
        <ConflictCard
          v-for="c in conflicts"
          :key="c.conflict_id"
          :conflict="c"
          @resolved="onConflictResolved"
          @dismissed="onConflictResolved"
        />
      </template>

      <!-- L3 对话摘要 -->
      <template v-else>
        <EmptyState
          v-if="summaries.length === 0"
          size="page"
          :icon="markRaw(DatabaseOutlined)"
          :description="$t('chat.no_conversation_summary_yet')"
          :hint="$t('chat.when_a_conversation_ends_the_dialogue_is_automatically_distilled_into_a_summary_for_cross_session_recall')"
        />
        <List
          v-else
          :data-source="summaries"
        >
          <template #renderItem="{ item }">
            <List.Item>
              <Card
                class="entry-card"
                size="small"
                style="width: 100%"
              >
                <div class="entry-head">
                  <Tag color="cyan">
                    {{ $t('chat.history_conversations') }}
                  </Tag>
                  <Tag
                    v-if="item.topics && item.topics.length"
                    color="default"
                  >
                    {{ item.topics.slice(0, 3).join(' ') }}
                  </Tag>
                </div>
                <div class="entry-value">
                  {{ item.content }}
                </div>
              </Card>
            </List.Item>
          </template>
        </List>
      </template>
    </Spin>

    <!-- 新建 / 编辑弹窗 -->
    <Modal
      v-model:open="formVisible"
      :title="editing ? $t('memory.edit_memory') : $t('memory.new_memory')"
      :confirm-loading="saving"
      :ok-text="$t('common.save')"
      :cancel-text="$t('common.cancel')"
      @ok="handleSave"
    >
      <div class="form-row">
        <label>{{ $t('common.category') }}</label>
        <Select
          v-model:value="form.slot"
          style="width: 100%"
        >
          <Select.Option
            v-for="s in MEMORY_SLOTS"
            :key="s.slot"
            :value="s.slot"
          >
            {{ s.label }}
          </Select.Option>
        </Select>
      </div>
      <div class="form-row">
        <label>{{ $t('common.key_2') }}</label>
        <Input
          v-model:value="form.key"
          :placeholder="$t('common.e_g_timezone_stack_rejected_needs')"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('common.content_value') }}</label>
        <Input.TextArea
          v-model:value="form.value"
          :rows="3"
          :placeholder="$t('memory.memory_content')"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('common.confidence_n_2', { n: form.confidence }) }}</label>
        <Slider
          v-model:value="form.confidence"
          :min="0"
          :max="100"
        />
      </div>
      <div class="form-row">
        <label>{{ $t('common.source') }}</label>
        <Select
          v-model:value="form.source"
          style="width: 100%"
        >
          <Select.Option value="user_confirmed">
            {{ $t('admin.user_confirmation') }}
          </Select.Option>
          <Select.Option value="derived">
            {{ $t('chat.conversation_distillation') }}
          </Select.Option>
          <Select.Option value="tool_written">
            {{ $t('agent.tool_write') }}
          </Select.Option>
        </Select>
      </div>
      <!-- 范围只对「新建」开放：编辑已有条目换范围走卡片上的切换按钮，
           那条路径要跨范围动作、还可能撞上目标范围的同名 key（409）。 -->
      <div
        v-if="!editing"
        class="form-row"
      >
        <label>{{ $t('common.scope') }}</label>
        <Select
          v-model:value="form.scope"
          style="width: 100%"
          data-test="form-scope"
        >
          <Select.Option value="global">
            {{ $t('memory.scope_global') }}
          </Select.Option>
          <Select.Option value="project">
            {{ $t('memory.scope_project') }}
          </Select.Option>
        </Select>
        <div class="config-note">
          {{ $t('memory.project_hint') }}
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.memory-view {
  max-width: 920px;
  margin: 0 auto;
  padding: 28px 24px 60px;
}
.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-3xl);
  font-weight: var(--fw-bold);
  letter-spacing: -0.01em;
}
.title .subtitle {
  font-size: var(--fs-sm);
  font-weight: var(--fw-regular);
  color: var(--text-secondary, #888);
}
.search-card {
  margin-bottom: 16px;
}
.result-section-title {
  margin: 18px 0 8px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--text-secondary, #888);
}
.entry-card {
  margin-bottom: 8px;
}
.entry-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.entry-key {
  font-weight: var(--fw-semibold);
}
.entry-actions {
  margin-inline-start: auto;
}
.entry-value {
  margin: 6px 0;
  white-space: pre-wrap;
  line-height: 1.5;
}
.entry-meta {
  font-size: var(--fs-sm);
  color: var(--text-secondary, #888);
}

/* 移动端：检索框占满、标题区换行 */
@media (max-width: 768px) {
  .memory-view {
    padding: 20px 16px 48px;
  }
  .search-card :deep(.ant-input-affix-wrapper) {
    width: 100% !important;
  }
  .title {
    font-size: var(--fs-2xl);
  }
  .title .subtitle {
    display: none;
  }
  .entry-head {
    row-gap: 4px;
  }
}
</style>
