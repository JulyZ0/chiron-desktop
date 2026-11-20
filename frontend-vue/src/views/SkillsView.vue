<script setup lang="ts">
import { ref, computed, onMounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import {
  Button, Input, Tabs, TabPane, Switch, Tag, Modal, InputNumber,
  Select, Alert, Dropdown, Menu, MenuItem, message,
} from 'ant-design-vue'
import {
  ThunderboltOutlined, DownloadOutlined, DeleteOutlined, PlayCircleOutlined,
  SearchOutlined, CodeOutlined, MessageOutlined, ShopOutlined, RobotOutlined,
  ArrowLeftOutlined, MoreOutlined,
} from '@ant-design/icons-vue'
import { api, listAgents, discoverSkills, type DiscoveredSkill, type SkillDiscovery } from '../api'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import AttachToAgentDialog from '../components/common/AttachToAgentDialog.vue'
import SkillDocPreview from '../components/skills/SkillDocPreview.vue'
import { collectSkillUsage, usageOf, type SkillUsage } from '../utils/skillUsage'

import { useI18n } from 'vue-i18n'
import { serverErrorDetail, errorMessageText } from '../utils/apiError'
const { t: tr } = useI18n()
interface SkillParam {
  name: string
  type?: string
  description?: string
  required?: boolean
  default?: unknown
  enum?: string[]
}

interface Skill {
  name: string
  description: string
  version: string
  author?: string
  tags?: string[]
  exec: { type: string; source?: string }
  parameters: SkillParam[]
  installed_at?: number
  enabled?: boolean
  /**
   * 技能所在层（后端 `SkillStore` 的 scope）：user / tenant / shared / builtin。
   * 老版本后端可能不带这个字段 —— 详情里的来源徽标对缺失值按"自定义"处理。
   */
  source?: string
  /**
   * P1-2 触发策略：是否每轮注入 system 上下文（后端缺省 true）。
   * 关掉之后模型不会在技能目录里看到它，但仍可被显式 @ 或工具读取。
   */
  auto_inject?: boolean
  /**
   * P1-2 来源（与 `source`/scope 正交）：`market:<source>/<id>` / `local` / `generated` /
   * `builtin`；老数据为空串，展示与分组都按「本地」处理。
   */
  origin?: string
}

const skills = ref<Skill[]>([])
const loading = ref(true)
const error = ref(false)
const activeTab = ref('list')
const searchQuery = ref('')
const typeFilter = ref('all')

const execColors: Record<string, string> = {
  python: 'purple',
  shell: 'green',
  http: 'gold',
  prompt: 'blue',
}
const router = useRouter()

// ── 加载 ──
async function loadSkills() {
  loading.value = true
  error.value = false
  try {
    const response = await api.get('/v1/skills')
    skills.value = response.data?.data?.skills || []
  } catch {
    error.value = true
    message.error(tr('errors.failed_to_fetch_skill_list_please_check_your_network_connection'))
  } finally {
    loading.value = false
  }
}

// ── 反向引用：这些技能被哪些 Agent / 工作流在用 ──
// 两个来源互不依赖（Agent 的 skills 列 / 工作流图里的 skill 节点），
// 任一拉取失败只让那一半为空，不拖垮技能列表本身。
const skillUsage = ref<Map<string, SkillUsage>>(new Map())

async function loadSkillUsage() {
  const [agents, graphs] = await Promise.all([
    listAgents().catch(() => []),
    api.get('/v1/graphs')
      .then(r => (r.data?.data ?? []) as { name: string; graph_json?: unknown }[])
      .catch(() => [] as { name: string; graph_json?: unknown }[]),
  ])
  skillUsage.value = collectSkillUsage({ agents, graphs })
}

/** 卡片上的用量摘要；没有任何引用时返回空串（不显示空标记） */
function usageLabel(name: string): string {
  const { agents, workflows } = usageOf(skillUsage.value, name)
  const parts: string[] = []
  if (agents.length) parts.push(tr('agent.n_agents', { n: agents.length }))
  if (workflows.length) parts.push(tr('workflow.n_workflows', { n: workflows.length }))
  return parts.join(' · ')
}

/** 悬浮提示：具体是谁在用 */
function usageTitle(name: string): string {
  const { agents, workflows } = usageOf(skillUsage.value, name)
  const lines: string[] = []
  if (agents.length) lines.push(`Agent：${agents.join('、')}`)
  if (workflows.length) lines.push(tr('workflow.workflows_list', { list: workflows.join('、') }))
  return lines.join('\n')
}

onMounted(() => {
  loadSkills()
  loadSkillUsage()
})


const execTypes = computed(() => {
  const set = new Set<string>()
  for (const s of skills.value) if (s.exec?.type) set.add(s.exec.type)
  return Array.from(set)
})

// ── 两栏工作台：左栏筛选 + 列表，右栏详情 ──
/** 来源分组：内置（builtin/shared 只读层）/ 自定义（user/root 私有层）/ 团队（tenant 共享层） */
type SkillGroup = 'all' | 'builtin' | 'custom' | 'team'
const groupFilter = ref<SkillGroup>('all')
const selectedSkillName = ref('')

function sourceKey(s: Skill): Exclude<SkillGroup, 'all'> {
  const src = (s.source || '').toLowerCase()
  if (src === 'tenant') return 'team'
  if (src === 'shared' || src === 'builtin') return 'builtin'
  // user / root / 老后端缺字段 —— 都归到"自定义"（用户可写的那一层）
  return 'custom'
}

/**
 * P1-2 的第二个维度：**来源**（`origin`）。与上面的 scope 维度**二选一**显示 ——
 * 两套芯片同时摆出来会让人分不清"团队共享"和"市场安装"到底哪个是主筛选。
 * 这里选择 origin：它回答的是"这个技能怎么来的"，正好对上市场/本地/生成三种处置方式。
 */
type OriginGroup = 'all' | 'market' | 'local' | 'generated'
const originFilter = ref<OriginGroup>('all')

function originKey(s: Skill): Exclude<OriginGroup, 'all'> {
  const origin = (s.origin || '').trim().toLowerCase()
  if (origin.startsWith('market:')) return 'market'
  if (origin === 'generated') return 'generated'
  // local / builtin / 老数据空串 —— 都算"本地"（内置技能也是随包就有的本地资产）
  return 'local'
}

function originLabel(s: Skill): string {
  switch (originKey(s)) {
    case 'market': return tr('agent.origin_market')
    case 'generated': return tr('agent.origin_generated')
    default: return tr('agent.origin_local')
  }
}

/** 只有市场来源的技能才谈得上"升级"（本地/生成的没有上游） */
function isMarketSkill(s: Skill): boolean {
  return originKey(s) === 'market'
}

function sourceLabel(s: Skill): string {
  switch (sourceKey(s)) {
    case 'team': return tr('common.team_shared')
    case 'builtin': return (s.source || '').toLowerCase() === 'builtin'
      ? tr('common.built_in')
      : tr('agent.source_shared')
    default: return tr('common.custom')
  }
}

const selectedSkill = computed<Skill | null>(
  () => skills.value.find(s => s.name === selectedSkillName.value) ?? null,
)
const detailVisible = computed(() => selectedSkillName.value !== '')

const groupCounts = computed(() => {
  const counts = { all: skills.value.length, market: 0, local: 0, generated: 0 }
  for (const s of skills.value) counts[originKey(s)] += 1
  return counts
})

const filteredSkills = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  return skills.value.filter((s) => {
    if (originFilter.value !== 'all' && originKey(s) !== originFilter.value) return false
    if (typeFilter.value !== 'all' && s.exec?.type !== typeFilter.value) return false
    if (!q) return true
    return (s.name || '').toLowerCase().includes(q)
      || (s.description || '').toLowerCase().includes(q)
      || (s.tags || []).some(t => t.toLowerCase().includes(q))
  })
})

/** SKILL.md 正文：prompt 型技能的 exec.source 就是正文；脚本型技能的 source 是代码 */
const docBody = computed(() => {
  const s = selectedSkill.value
  if (!s || s.exec?.type !== 'prompt') return ''
  return s.exec?.source || ''
})

/** 非 prompt 型技能的执行内容（脚本 / 端点），用等宽块展示而不是当 Markdown 渲染 */
const execSourceCode = computed(() => {
  const s = selectedSkill.value
  if (!s || s.exec?.type === 'prompt') return ''
  return s.exec?.source || ''
})

function selectSkill(s: Skill) {
  selectedSkillName.value = s.name
  // 换了技能就清掉上一条升级提示：否则"另一个技能已是最新"会显示在新技能详情里
  upgradeNotice.value = null
}

function closeDetail() {
  selectedSkillName.value = ''
  upgradeNotice.value = null
}

// ── 启停 ──
async function toggleEnabled(s: Skill, v: boolean) {
  try {
    await api.put(`/v1/skills/${encodeURIComponent(s.name)}`, { enabled: v })
    s.enabled = v
    if (v) message.success(tr('common.name_is_enabled', { name: s.name }))
    else message.success(tr('common.name_is_disabled', { name: s.name }))
  } catch (e) {
    message.error(tr('errors.operation_failed_error', { error: serverErrorDetail(e, '') }))
  }
}

// ── P1-2：触发策略（auto_inject）──
//
// 关掉之后该技能**不进每轮 system 上下文**（后端 skill catalog 过滤），但仍可被
// 显式 @ 或工具读取。写的是**元数据**（PATCH /v1/skills/{name}/meta），不是完整的
// 技能定义 —— 用 PUT 会把正文一起回写，生成型技能的正文可能因此被旧副本覆盖。
const autoInjectSaving = ref('')

async function toggleAutoInject(s: Skill, v: boolean) {
  autoInjectSaving.value = s.name
  try {
    await api.patch(`/v1/skills/${encodeURIComponent(s.name)}/meta`, { auto_inject: v })
    s.auto_inject = v
    message.success(
      v
        ? tr('agent.auto_inject_on', { name: s.name })
        : tr('agent.auto_inject_off', { name: s.name }),
    )
  } catch (e) {
    message.error(tr('errors.operation_failed_error', { error: serverErrorDetail(e, '') }))
  } finally {
    autoInjectSaving.value = ''
  }
}

// ── P1-2：检查更新 / 升级（仅市场来源）──
const upgradeLoading = ref(false)
/** 内联提示：升级结果就地显示，不再弹一个"成功"的 toast 然后什么也看不到 */
const upgradeNotice = ref<{ type: 'success' | 'info' | 'warning' | 'error'; text: string } | null>(null)

async function runUpgrade(s: Skill) {
  upgradeLoading.value = true
  upgradeNotice.value = null
  try {
    const resp = await api.post(`/v1/skills/${encodeURIComponent(s.name)}/upgrade`, {})
    const d = (resp.data?.data ?? resp.data ?? {}) as {
      upgraded?: boolean
      reason?: string
      from_version?: string
      to_version?: string
      latest_version?: string
    }
    if (d.upgraded) {
      upgradeNotice.value = {
        type: 'success',
        text: tr('agent.upgraded_to', { from: d.from_version || s.version, to: d.to_version || '' }),
      }
      await loadSkills()
      if (d.to_version) s.version = d.to_version
    } else {
      upgradeNotice.value = {
        type: 'info',
        text: tr('agent.already_latest_version', { version: d.latest_version || s.version }),
      }
    }
  } catch (e) {
    // 409 = 没有可用来源（非市场来源 / 没记下安装地址）：这是**稳定结论**，不是故障
    const status = (e as { response?: { status?: number } })?.response?.status
    upgradeNotice.value = {
      type: status === 409 ? 'warning' : 'error',
      text: status === 409
        ? tr('agent.upgrade_no_source')
        : tr('errors.operation_failed_error', { error: serverErrorDetail(e, '') }),
    }
  } finally {
    upgradeLoading.value = false
  }
}

// ── 删除 ──
function requestDelete(s: Skill) {
  Modal.confirm({
    title: tr('agent.delete_skill'),
    content: tr('common.delete_name', { name: s.name }),
    okText: tr('common.delete'),
    okButtonProps: { danger: true },
    cancelText: tr('common.cancel'),
    onOk: async () => {
      try {
        await api.delete(`/v1/skills/${encodeURIComponent(s.name)}`)
        message.success(tr('common.deleted'))
        // 删掉的正是右栏在看的那个：清空详情，别留一份指向已删除技能的界面
        if (selectedSkillName.value === s.name) selectedSkillName.value = ''
        await loadSkills()
      } catch {
        message.error(tr('errors.delete_failed'))
      }
    },
  })
}

// ── 互联互通：在对话中使用该技能 ──
function useInChat(s: Skill) {
  router.push({ path: '/chat', query: { skill: s.name } })
}

// ── 互联互通：装配到 Agent ──
// 与"在对话中使用"的区别：这里写回 Agent 的 skills 列，是**持久**绑定 ——
// 之后每次派发该 Agent 都会带上它。
const attachOpen = ref(false)
const attachSkill = ref('')

function openAttach(s: Skill) {
  attachSkill.value = s.name
  attachOpen.value = true
}

// ── 运行 ──
const runOpen = ref(false)
const runTarget = ref<Skill | null>(null)
const runValues = ref<Record<string, unknown>>({})
const runSubmitting = ref(false)
const runResult = ref<unknown>(null)

/**
 * 运行表单取值：Select / InputNumber / Input 只接受 `string | number`
 * （布尔型参数由 Switch 单独绑定 `=== true`），这里做一次窄化。
 */
function paramText(name: string): string | number {
  const v = runValues.value[name]
  return typeof v === 'string' || typeof v === 'number' ? v : ''
}

function openRun(s: Skill) {
  runTarget.value = s
  runValues.value = {}
  for (const p of s.parameters || []) {
    if (p.default !== undefined) runValues.value[p.name] = p.default
  }
  runResult.value = null
  runOpen.value = true
}

async function submitRun() {
  if (!runTarget.value) return
  const params: Record<string, unknown> = {}
  for (const p of runTarget.value.parameters || []) {
    const v = runValues.value[p.name]
    if (v === undefined || v === null || v === '') {
      if (p.required) { message.warning(tr('agent.please_fill_in_parameter_name', { name: p.name })); return }
      continue
    }
    params[p.name] = v
  }
  runSubmitting.value = true
  runResult.value = null
  try {
    const resp = await api.post(`/v1/skills/${encodeURIComponent(runTarget.value.name)}/run`, { params })
    runResult.value = resp.data?.data || resp.data
    message.success(tr('agent.skill_execution_complete'))
  } catch (e) {
    message.error(tr('errors.execution_failed_error', { error: serverErrorDetail(e, '') }))
  } finally {
    runSubmitting.value = false
  }
}

function renderResult(res: unknown): string {
  const r = (res ?? {}) as { output?: unknown }
  if (r.output) return typeof r.output === 'string' ? r.output : JSON.stringify(r.output, null, 2)
  return JSON.stringify(res, null, 2)
}

// ── 安装 ──
const installURL = ref('')
const installInline = ref('')
const installLoading = ref(false)

async function handleInstall() {
  const body: Record<string, unknown> = {}
  if (installURL.value) body.url = installURL.value
  else if (installInline.value) body.inline = installInline.value
  else { message.error(tr('common.please_enter_a_url_or_inline_json')); return }
  installLoading.value = true
  try {
    await api.post('/v1/skills/install', body)
    message.success(tr('agent.skill_installed'))
    installURL.value = ''
    installInline.value = ''
    await loadSkills()
    activeTab.value = 'list'
  } catch (e) {
    message.error(tr('errors.install_failed_error', { error: serverErrorDetail(e, '') }))
  } finally {
    installLoading.value = false
  }
}

// ── 生成 ──
const genDesc = ref('')
/** POST /v1/skills/generate 返回的 skill 摘要（模板直接读 name/description） */
interface GeneratedSkill {
  name?: string
  description?: string
}
const genResult = ref<GeneratedSkill | null>(null)
const genLoading = ref(false)

async function handleGenerate() {
  if (!genDesc.value.trim()) { message.error(tr('common.please_enter_a_description')); return }
  genLoading.value = true
  genResult.value = null
  try {
    const response = await api.post('/v1/skills/generate', {
      description: genDesc.value,
      auto_install: true,
    })
    genResult.value = response.data?.data?.skill || response.data?.data
    message.success(tr('agent.skill_generated_and_installed'))
    await loadSkills()
  } catch (e) {
    message.error(tr('errors.generation_failed_error', { error: serverErrorDetail(e, '') }))
  } finally {
    genLoading.value = false
  }
}

// ── 发现（GET /v1/skills/discover）──
//
// 这是**唯一**能看到"远端有、本地没有"的入口：此前前端只有安装（要用户自己知道地址）
// 与列表（只看已装），中间的"发现 → 挑选 → 安装"整段是断的。
const catalogUrl = ref('')
const discovery = ref<SkillDiscovery>({ local: [], remote: [] })
const discoverLoading = ref(false)
const discoverError = ref('')
/** 是否已经拉过一次目录 —— 用来区分"还没拉"与"拉了但目录是空的" */
const discoverDone = ref(false)

async function handleDiscover() {
  discoverLoading.value = true
  discoverError.value = ''
  try {
    discovery.value = await discoverSkills(catalogUrl.value.trim() || undefined)
    discoverDone.value = true
  } catch (e) {
    // 就地报错：拉取失败必须与"目录里确实没有技能"区分开，否则用户以为目录是空的。
    discoverError.value = errorMessageText(e, tr('errors.operation_failed'))
    discovery.value = { local: [], remote: [] }
    discoverDone.value = false
  } finally {
    discoverLoading.value = false
  }
}

/** 把发现结果里的条目装进本地（复用安装端点，装完刷新列表与发现结果） */
async function installDiscovered(item: DiscoveredSkill) {
  if (!item.source) {
    message.warning(tr('agent.discover_no_install_url'))
    return
  }
  installLoading.value = true
  try {
    await api.post('/v1/skills/install', { url: item.source })
    message.success(tr('agent.skill_installed'))
    await loadSkills()
    await handleDiscover()
  } catch (e) {
    message.error(tr('errors.install_failed_error', { error: serverErrorDetail(e, '') }))
  } finally {
    installLoading.value = false
  }
}
</script>

<template>
  <div class="skills-page">
    <header class="page-header">
      <div class="page-header-text">
        <h1 class="page-title">
          {{ $t('agent.skill') }}
        </h1>
        <p class="page-sub">
          {{ $t('agent.extend_agent_custom_capabilities_enable_run_and_ai_generate_them') }}
        </p>
      </div>
    </header>

    <div class="skills-tabs">
        <div
          class="workspace"
          :class="{ 'has-selection': detailVisible }"
        >
          <!-- 左栏：筛选 + 列表 -->
          <aside
            class="pane pane-list"
            :aria-label="$t('agent.skill_list')"
          >
            <div class="pane-head">
              <Input
                v-model:value="searchQuery"
                :placeholder="$t('common.search_name_description_tags')"
                allow-clear
                class="pane-search"
              >
                <template #prefix>
                  <SearchOutlined />
                </template>
              </Input>
              <Select
                v-model:value="typeFilter"
                class="type-filter"
                :options="[{ value: 'all', label: $t('common.all_types') }, ...execTypes.map(t => ({ value: t, label: t }))]"
              />
              <div
                class="group-nav"
                role="group"
                :aria-label="$t('agent.group_filter_label')"
              >
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: originFilter === 'all' }"
                  :aria-pressed="originFilter === 'all'"
                  @click="originFilter = 'all'"
                >
                  {{ $t('agent.filter_all') }}
                  <span class="group-count">{{ groupCounts.all }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: originFilter === 'market' }"
                  :aria-pressed="originFilter === 'market'"
                  @click="originFilter = 'market'"
                >
                  {{ $t('agent.origin_market') }}
                  <span class="group-count">{{ groupCounts.market }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: originFilter === 'local' }"
                  :aria-pressed="originFilter === 'local'"
                  @click="originFilter = 'local'"
                >
                  {{ $t('agent.origin_local') }}
                  <span class="group-count">{{ groupCounts.local }}</span>
                </button>
                <button
                  type="button"
                  class="group-chip"
                  :class="{ active: originFilter === 'generated' }"
                  :aria-pressed="originFilter === 'generated'"
                  @click="originFilter = 'generated'"
                >
                  {{ $t('agent.origin_generated') }}
                  <span class="group-count">{{ groupCounts.generated }}</span>
                </button>
              </div>
            </div>

            <div class="pane-body">
              <PageSkeleton
                v-if="loading"
                variant="list"
                :rows="6"
                :header="false"
              />
              <Alert
                v-else-if="error"
                type="error"
                show-icon
                :message="$t('errors.failed_to_load')"
                :description="$t('agent.unable_to_fetch_skill_list_please_retry_later')"
              >
                <template #action>
                  <Button
                    size="small"
                    @click="loadSkills"
                  >
                    {{ $t('common.retry') }}
                  </Button>
                </template>
              </Alert>
              <EmptyState
                v-else-if="skills.length === 0"
                :icon="markRaw(CodeOutlined)"
                :description="$t('agent.no_skills')"
                :hint="$t('agent.install_skills_from_the_market_or_upload_a_local_skill_package')"
              />
              <EmptyState
                v-else-if="filteredSkills.length === 0"
                :icon="markRaw(SearchOutlined)"
                :description="$t('agent.no_matching_skills')"
                :hint="$t('common.try_adjusting_search_keywords_or_type_filters')"
              />
              <ul
                v-else
                class="item-list"
                role="listbox"
                :aria-label="$t('agent.skill_list')"
              >
                <li
                  v-for="s in filteredSkills"
                  :key="s.name"
                  class="item"
                  :class="{
                    active: selectedSkillName === s.name,
                    muted: s.enabled === false,
                  }"
                  role="option"
                  :aria-selected="selectedSkillName === s.name"
                  tabindex="0"
                  @click="selectSkill(s)"
                  @keydown.enter.prevent="selectSkill(s)"
                  @keydown.space.prevent="selectSkill(s)"
                >
                  <span class="item-icon"><CodeOutlined /></span>
                  <span class="item-main">
                    <span class="item-title-line">
                      <span class="item-name">{{ s.name }}</span>
                      <Tag
                        :color="execColors[s.exec?.type] || 'default'"
                        class="item-tag"
                      >
                        {{ s.exec?.type || 'unknown' }}
                      </Tag>
                    </span>
                    <span class="item-desc">{{ s.description || $t('common.no_description') }}</span>
                    <span class="item-meta">
                      <span
                        v-if="usageLabel(s.name)"
                        class="item-usage"
                        :title="usageTitle(s.name)"
                      >{{ $t('common.used_by_label', { label: usageLabel(s.name) }) }}</span>
                      <span
                        v-else
                        class="item-meta-text"
                      >v{{ s.version }}</span>
                      <span
                        v-if="s.parameters?.length"
                        class="item-meta-text"
                      >· {{ $t('agent.n_params', { n: s.parameters.length }) }}</span>
                    </span>
                  </span>
                  <Switch
                    :checked="s.enabled !== false"
                    size="small"
                    class="item-switch"
                    :aria-label="s.name"
                    @click.stop
                    @change="(v: any) => toggleEnabled(s, Boolean(v))"
                  />
                </li>
              </ul>
            </div>
          </aside>

          <!-- 右栏：详情 + SKILL.md 正文 -->
          <section
            class="pane pane-detail"
            :aria-label="$t('agent.skill_detail_title')"
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
              v-if="!detailVisible || !selectedSkill"
              class="detail-placeholder"
            >
              <EmptyState
                size="page"
                :icon="markRaw(CodeOutlined)"
                :description="$t('agent.skill_detail_title')"
                :hint="$t('agent.select_skill_hint')"
                :auto-pad="false"
              />
            </div>

            <template v-else>
              <div class="detail-head">
                <span class="detail-icon"><CodeOutlined /></span>
                <div class="detail-titles">
                  <h2 class="detail-name">
                    {{ selectedSkill.name }}
                  </h2>
                  <p class="detail-sub">
                    {{ selectedSkill.description || $t('common.no_description') }}
                  </p>
                </div>
                <div class="detail-actions">
                  <Button
                    size="small"
                    type="primary"
                    :disabled="selectedSkill.enabled === false"
                    @click="openRun(selectedSkill)"
                  >
                    <template #icon>
                      <PlayCircleOutlined />
                    </template>
                    {{ $t('common.run') }}
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
                          key="delete"
                          danger
                          @click="requestDelete(selectedSkill)"
                        >
                          <DeleteOutlined class="menu-icon" />{{ $t('common.delete') }}
                        </MenuItem>
                      </Menu>
                    </template>
                  </Dropdown>
                </div>
              </div>

              <div class="detail-badges">
                <Tag :color="execColors[selectedSkill.exec?.type] || 'default'">
                  {{ selectedSkill.exec?.type || 'unknown' }}
                </Tag>
                <Tag>{{ $t('common.source') }}: {{ sourceLabel(selectedSkill) }}</Tag>
                <Tag>{{ $t('agent.origin_label') }}: {{ originLabel(selectedSkill) }}</Tag>
                <Tag>v{{ selectedSkill.version }}</Tag>
                <Tag v-if="selectedSkill.parameters?.length">
                  {{ $t('agent.n_params', { n: selectedSkill.parameters.length }) }}
                </Tag>
                <span class="detail-enable">
                  <span class="enable-label">{{ $t('common.enable') }}</span>
                  <Switch
                    :checked="selectedSkill.enabled !== false"
                    size="small"
                    :aria-label="selectedSkill.name"
                    @change="(v: any) => selectedSkill && toggleEnabled(selectedSkill, Boolean(v))"
                  />
                </span>
                <span
                  class="detail-enable"
                  :title="$t('agent.auto_inject_hint')"
                >
                  <span class="enable-label">{{ $t('agent.auto_inject') }}</span>
                  <Switch
                    :checked="selectedSkill.auto_inject !== false"
                    size="small"
                    :loading="autoInjectSaving === selectedSkill.name"
                    :aria-label="`auto-inject-${selectedSkill.name}`"
                    @change="(v: any) => selectedSkill && toggleAutoInject(selectedSkill, Boolean(v))"
                  />
                </span>
              </div>

              <!-- 版本 / 升级：只有市场来源才谈得上"检查更新" -->
              <div
                v-if="isMarketSkill(selectedSkill)"
                class="upgrade-row"
              >
                <span class="upgrade-current">
                  {{ $t('agent.current_version', { version: selectedSkill.version }) }}
                </span>
                <Button
                  size="small"
                  :loading="upgradeLoading"
                  @click="runUpgrade(selectedSkill)"
                >
                  {{ $t('agent.check_update_and_upgrade') }}
                </Button>
              </div>
              <Alert
                v-if="upgradeNotice"
                class="upgrade-notice"
                :type="upgradeNotice.type"
                :message="upgradeNotice.text"
                show-icon
              />

              <div class="detail-actions-row">
                <Button
                  size="small"
                  :title="$t('agent.use_this_skill_in_the_conversation')"
                  @click="useInChat(selectedSkill)"
                >
                  <template #icon>
                    <MessageOutlined />
                  </template>
                  {{ $t('chat.use_in_the_conversation') }}
                </Button>
                <Button
                  size="small"
                  :title="$t('agent.assemble_this_skill_into_an_agent_persistent_binding')"
                  @click="openAttach(selectedSkill)"
                >
                  <template #icon>
                    <RobotOutlined />
                  </template>
                  {{ $t('agent.assemble_to_agent') }}
                </Button>
                <Tag
                  v-if="usageLabel(selectedSkill.name)"
                  color="blue"
                  class="detail-usage"
                  :title="usageTitle(selectedSkill.name)"
                >
                  {{ $t('common.used_by_label', { label: usageLabel(selectedSkill.name) }) }}
                </Tag>
              </div>

              <!-- SKILL.md 正文：prompt 型技能的 exec.source 即正文，走 Markdown 渲染 -->
              <section class="doc-section">
                <h3 class="doc-title">
                  {{ $t('agent.skill_md_preview') }}
                </h3>
                <SkillDocPreview
                  v-if="selectedSkill.exec?.type === 'prompt'"
                  :source="docBody"
                  :empty-text="$t('agent.skill_no_preview')"
                />
                <template v-else>
                  <div
                    v-if="execSourceCode"
                    class="code-block"
                  >
                    <div class="code-block-label">
                      {{ $t('common.execution_content') }}
                    </div>
                    <pre class="code-pre">{{ execSourceCode }}</pre>
                  </div>
                  <p
                    v-else
                    class="doc-empty"
                  >
                    {{ $t('agent.skill_no_preview') }}
                  </p>
                </template>
              </section>

              <!-- 参数：列表比裸 JSON 好读，仍是原始字段，没有改契约 -->
              <section
                v-if="selectedSkill.parameters?.length"
                class="doc-section"
              >
                <h3 class="doc-title">
                  {{ $t('agent.parameter') }}
                </h3>
                <ul class="param-list">
                  <li
                    v-for="p in selectedSkill.parameters"
                    :key="p.name"
                    class="param-item"
                  >
                    <span class="param-name">{{ p.name }}</span>
                    <span
                      v-if="p.type"
                      class="param-type"
                    >{{ p.type }}</span>
                    <span
                      v-if="p.required"
                      class="param-required"
                    >*</span>
                    <span
                      v-if="p.description"
                      class="param-desc"
                    >{{ p.description }}</span>
                  </li>
                </ul>
              </section>

              <div
                v-if="selectedSkill.author || (selectedSkill.tags || []).length"
                class="detail-foot"
              >
                <span
                  v-if="selectedSkill.author"
                  class="detail-meta"
                >{{ $t('common.author') }}: {{ selectedSkill.author }}</span>
                <div class="detail-tags">
                  <Tag
                    v-for="t in (selectedSkill.tags || []).slice(0, 6)"
                    :key="t"
                  >
                    {{ t }}
                  </Tag>
                </div>
              </div>
            </template>
          </section>
        </div>

    <!-- ── 运行 Modal ── -->
    <Modal
      :open="runOpen"
      :title="$t('common.run_name', { name: runTarget?.name || '' })"
      :footer="null"
      width="520px"
      @cancel="runOpen = false"
    >
      <div class="run-desc">
        {{ runTarget?.description }}
      </div>
      <div
        v-if="runTarget?.parameters?.length"
        class="run-form"
      >
        <div
          v-for="p in runTarget.parameters"
          :key="p.name"
          class="run-field"
        >
          <label class="field-label">
            {{ p.name }}
            <span
              v-if="p.required"
              class="required"
            >*</span>
            <span
              v-if="p.type"
              class="field-type"
            >{{ p.type }}</span>
          </label>
          <Select
            v-if="p.enum?.length"
            :value="paramText(p.name)"
            :options="p.enum.map(e => ({ value: e, label: e }))"
            :placeholder="p.description || p.name"
            style="width: 100%"
            @update:value="(v: unknown) => { runValues[p.name] = v }"
          />
          <InputNumber
            v-else-if="p.type === 'number'"
            :value="paramText(p.name)"
            :placeholder="p.description || p.name"
            style="width: 100%"
            @update:value="(v: unknown) => { runValues[p.name] = v }"
          />
          <Switch
            v-else-if="p.type === 'boolean'"
            :checked="runValues[p.name] === true"
            @update:checked="(v: unknown) => { runValues[p.name] = v === true }"
          />
          <Input.TextArea
            v-else-if="p.type === 'text'"
            :value="paramText(p.name)"
            :rows="3"
            :placeholder="p.description || p.name"
            @update:value="(v: unknown) => { runValues[p.name] = v }"
          />
          <Input
            v-else
            :value="paramText(p.name)"
            :placeholder="p.description || p.name"
            @update:value="(v: unknown) => { runValues[p.name] = v }"
          />
        </div>
      </div>
      <div
        v-else
        class="run-empty"
      >
        {{ $t('agent.this_skill_takes_no_parameters') }}
      </div>
      <div class="run-actions">
        <Button
          type="primary"
          :loading="runSubmitting"
          @click="submitRun"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ $t('common.execute') }}
        </Button>
      </div>
      <div
        v-if="runResult"
        class="run-result"
      >
        <div class="result-label">
          {{ $t('common.execution_result') }}
        </div>
        <pre class="result-pre">{{ renderResult(runResult) }}</pre>
      </div>
    </Modal>

    <!-- 装配到 Agent：写回 Agent 的 skills 列（持久绑定，区别于"在对话中使用"） -->
    <AttachToAgentDialog
      v-model:open="attachOpen"
      kind="skill"
      :value="attachSkill"
      :label="attachSkill"
    />
  </div>
    </div>
</template>

<style scoped>
.skills-page { max-width: 1200px; margin: 0 auto; padding: 28px 24px 60px; }
.page-sub { margin: 4px 0 0; color: var(--text-tertiary); font-size: 13px; }
.skills-tabs :deep(.ant-tabs-nav) { margin-bottom: 16px; }

/* ── 两栏工作台（与 Agents 页同构）── */
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
.type-filter { width: 100%; }
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
.item-icon {
  flex: none; width: 30px; height: 30px; border-radius: var(--radius-md);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary-bg); color: var(--primary); font-size: 15px;
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
.item-usage { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item-switch { flex: none; }

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
.detail-icon {
  flex: none; width: 42px; height: 42px; border-radius: var(--sig-radius-card);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary-bg); color: var(--primary); font-size: 20px;
}
.detail-titles { flex: 1; min-width: 0; }
.detail-name {
  margin: 0;
  font-size: var(--fs-lg); font-weight: var(--fw-semibold); color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.detail-sub { margin: 3px 0 0; font-size: var(--fs-sm); color: var(--text-tertiary); line-height: var(--lh-normal); }
.detail-actions { display: flex; align-items: center; gap: 6px; }
.detail-badges { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.detail-enable { display: inline-flex; align-items: center; gap: 6px; }
.detail-enable + .detail-enable { margin-inline-start: 0; }
.enable-label { font-size: var(--fs-xs); color: var(--text-tertiary); }
/* P1-2：版本 / 升级行与内联结果提示 */
.upgrade-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.upgrade-current { font-size: var(--fs-xs); color: var(--text-tertiary); }
.upgrade-notice { margin-top: 8px; }
.detail-actions-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.detail-usage { margin-inline-start: auto; }
.doc-section { display: flex; flex-direction: column; gap: 8px; }
.doc-title {
  margin: 0;
  font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--text-secondary);
}
.doc-empty { margin: 0; font-size: var(--fs-sm); color: var(--text-tertiary); }
.code-block {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  overflow: hidden;
}
.code-block-label {
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs); color: var(--text-tertiary);
  border-bottom: var(--hairline-width) solid var(--border-card);
}
.code-pre {
  margin: 0; padding: var(--space-3);
  font-family: var(--font-mono); font-size: var(--fs-xs);
  line-height: var(--lh-relaxed); color: var(--text-primary);
  white-space: pre-wrap; word-break: break-word;
  max-height: 320px; overflow-y: auto;
}
.param-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.param-item {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px;
  padding: 8px 10px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}
.param-name { font-family: var(--font-mono); font-size: var(--fs-sm); color: var(--text-primary); }
.param-type {
  font-size: var(--fs-xs); color: var(--text-tertiary);
  background: var(--bg-secondary); padding: 1px 6px; border-radius: var(--radius-sm);
}
.param-required { color: var(--error); }
.param-desc { flex-basis: 100%; font-size: var(--fs-xs); color: var(--text-tertiary); }
.detail-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding-top: 10px; flex-wrap: wrap;
  border-top: var(--hairline-width) solid var(--border-subtle);
}
.detail-meta { font-size: var(--fs-xs); color: var(--text-tertiary); }
.detail-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.card-menu { min-width: 140px; border-radius: var(--sig-radius-card); padding: 4px; box-shadow: var(--shadow-lg); }
.card-menu :deep(.ant-dropdown-menu-item) { display: flex; align-items: center; gap: 8px; font-size: 13px; border-radius: var(--radius-md); }
.menu-icon { font-size: 14px; }

/* ── 安装 / 发现 / 生成（保持原样，仅统一 token）── */
.card-name-line { display: flex; align-items: center; gap: 6px; min-width: 0; }
.card-name { font-size: 14px; font-weight: var(--fw-semibold); color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.exec-tag { flex: none; }
.card-desc { display: block; margin-top: 3px; font-size: 12px; color: var(--text-tertiary); line-height: 1.5; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.card-meta { display: flex; flex-wrap: wrap; gap: 4px; }
.detail-label { font-size: var(--fs-xs); color: var(--text-tertiary); font-weight: var(--fw-medium); }

.panel-card { max-width: 640px; border: var(--hairline-width) solid var(--border-card); border-radius: var(--radius-lg); background: var(--bg-card); box-shadow: var(--shadow-md); padding: 20px; }
.panel-title { font-size: 14px; font-weight: var(--fw-semibold); color: var(--text-primary); margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
.or-divider { text-align: center; color: var(--text-tertiary); margin: 14px 0; }
.mono-input :deep(textarea) { font-family: var(--font-mono); font-size: 12px; }
.install-btn { margin-top: 14px; }
.panel-hint { margin: 8px 0 0; font-size: var(--fs-xs); color: var(--text-tertiary); line-height: 1.5; }

/* 发现页签：左侧条目信息、右侧安装按钮，窄屏时上下堆叠 */
.discover-alert { margin-top: 14px; }
.discover-list { display: flex; flex-direction: column; gap: 10px; margin-top: 16px; }
.discover-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
}
.discover-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.discover-author { font-size: var(--fs-xs); color: var(--text-tertiary); }
.discover-local { display: flex; flex-direction: column; gap: 6px; margin-top: 20px; }
.gen-alert { margin-top: 14px; }
.gen-pre { margin: 12px 0 0; padding: 10px; background: var(--bg-secondary); border-radius: var(--radius-lg); font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-secondary); white-space: pre-wrap; max-height: 260px; overflow-y: auto; }

.run-desc { font-size: 13px; color: var(--text-secondary); margin-bottom: 14px; }
.run-form { display: flex; flex-direction: column; gap: 12px; }
.run-field { display: flex; flex-direction: column; gap: 6px; }
.field-label { font-size: 12px; color: var(--text-secondary); font-weight: var(--fw-medium); display: flex; align-items: center; gap: 6px; }
.required { color: var(--error); }
.field-type { font-size: var(--fs-xs); color: var(--text-tertiary); background: var(--bg-secondary); padding: 1px 6px; border-radius: var(--radius-sm); }
.run-empty { color: var(--text-tertiary); font-size: 13px; padding: 8px 0; }
.run-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
.run-result { margin-top: 14px; border: var(--hairline-width) solid var(--border-card); border-radius: var(--sig-radius-card); overflow: hidden; }
.result-label { padding: 8px 12px; font-size: 12px; color: var(--text-tertiary); background: var(--bg-secondary); border-bottom: var(--hairline-width) solid var(--border-card); }
.result-pre { margin: 0; padding: 12px; font-family: var(--font-mono); font-size: 12px; line-height: 1.6; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow-y: auto; }

/* ── 窄屏：单栏，列表为主；选中后进详情并带返回 ── */
@media (max-width: 900px) {
  .workspace { grid-template-columns: minmax(0, 1fr); }
  .workspace .pane-detail { display: none; }
  .workspace.has-selection .pane-list { display: none; }
  .workspace.has-selection .pane-detail { display: flex; }
  .back-btn { display: inline-flex; }
  .pane-body { max-height: none; }
}

@media (max-width: 768px) {
  .skills-page { padding: 20px 16px 48px; }
}

@media (max-width: 640px) {
  .detail-actions { justify-content: flex-start; }
}
</style>
