<script setup lang="ts">
/**
 * Super-agent 接管 —— `/agents` 页的第三个 Tab。
 *
 * ## 这个面板要回答的四个问题
 *
 * 1. **能不能接管？** 引擎侧开关（`SUPERAGENT_TAKEOVER_ENABLED`）与用户总开关是
 *    两件事。引擎关着时用户配得再全也不会触发 —— 不显式说出来，用户只能怀疑自己
 *    配错了。所以 `engine_enabled=false` 时顶部给明确的告警，而不是让开关看起来正常。
 * 2. **接管谁？** 最多 `max_sessions`（5）个会话。上限由后端定，前端只负责不让人超额。
 * 3. **什么时候接管？** `once`（从现在起 N 分钟后，一次）或 `interval`（每 N 分钟）。
 * 4. **代价多少？** 每个节拍都是真实回合，按正常调用计费。日预算是硬闸门。
 *
 * ## 为什么不提供 `yolo`
 *
 * 后端 `safe_tools_mode` 会把 `yolo` 一律降为 `auto`。UI 里如果保留这个选项，
 * 用户选了、存了、界面上写着 `yolo`，运行时却是 `auto` —— 这是最坏的一种谎报。
 * 所以下拉里只有 `ask` / `auto`，并在提示里说明"无人值守不继承 yolo"。
 *
 * ## 为什么 `once` 用"多少分钟后"而不是日期选择器
 *
 * 用户要的是"今晚别管我，一小时后自己看一眼"，不是"设定 21:37:00"。倒计时语义
 * 还顺带消掉了时区问题（`dayjs` 的本地/UTC 转换在跨时区机器上是常见的错源）。
 * 后端上限 24 小时，前端同样限制。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { RobotOutlined, PlusOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../api'
import {
  createTakeover,
  deleteTakeover,
  getSuperagentStatus,
  getTakeoverSettings,
  listTakeovers,
  saveTakeoverSettings,
  updateTakeover,
  type SuperagentStatus,
  type SuperagentTakeover,
} from '../../api'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

/** 可编辑的一行。服务端的只读状态与可编辑字段放在一起，保存时只提交后者。 */
interface RowState {
  id: string
  session_id: string
  session_title: string
  // ── 可编辑 ──
  enabled: boolean
  schedule_kind: 'once' | 'interval'
  /** `once` 的倒计时（分钟）；只在用户显式选 once 时有意义 */
  once_delay_minutes: number
  interval_minutes: number
  daily_token_budget: number
  tools_mode: string
  // ── 只读状态 ──
  tokens_used_today: number
  last_verdict: string
  last_reason: string
  last_run_at: string
  melt_state: string
  melt_reason: string
  cooldown_until: string
  consecutive_failures: number
  /** 初始快照，用来判断"有没有改过" */
  snapshot: string
}

const loading = ref(false)
const savingSettings = ref(false)
const savingId = ref('')
const adding = ref(false)
const loadError = ref('')

const status = ref<SuperagentStatus | null>(null)
const settings = ref({ enabled: false, max_sessions: 5, day_token_budget: 0, idle_minutes: 60 })
const rows = ref<RowState[]>([])
/** 可选会话（`/v1/conversations`） */
const sessions = ref<Array<{ id: string; title: string }>>([])
const pickedSessionIds = ref<string[]>([])
/** 用于刷新"冷却剩余分钟"的计时器 */
const nowTick = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null

const maxSessions = computed(() => status.value?.max_sessions ?? 5)
const minIntervalMinutes = computed(() =>
  Math.max(1, Math.ceil((status.value?.min_interval_seconds ?? 900) / 60)),
)
const maxOnceMinutes = computed(() =>
  Math.floor((status.value?.max_once_horizon_seconds ?? 86400) / 60),
)
const atLimit = computed(() => rows.value.length >= maxSessions.value)
const takenSessionIds = computed(() => new Set(rows.value.map((r) => r.session_id)))

/** 还能新增的会话；已接管的排除掉，避免建出重复行（后端会 409） */
const addableSlots = computed(() =>
  sessions.value.filter((s) => !takenSessionIds.value.has(s.id)),
)

/** 引擎没开接管循环时，配置能存但永远不会跑 —— 必须说清楚 */
const engineOff = computed(() => status.value !== null && !status.value.engine_enabled)

function snapshotOf(row: Omit<RowState, 'snapshot'>): string {
  return JSON.stringify([
    row.enabled,
    row.schedule_kind,
    row.interval_minutes,
    row.daily_token_budget,
    row.tools_mode,
  ])
}

function toRow(item: SuperagentTakeover, titleMap: Map<string, string>): RowState {
  const intervalSeconds = item.interval_seconds || 3600
  const base: Omit<RowState, 'snapshot'> = {
    id: item.id,
    session_id: item.session_id,
    session_title: titleMap.get(item.session_id) || item.session_id.slice(0, 8),
    enabled: item.enabled,
    schedule_kind: item.schedule_kind === 'once' ? 'once' : 'interval',
    // 已有配置的 once_at 可能是过去时刻；默认倒计时给 60 分钟，用户不改就按它存
    once_delay_minutes: 60,
    interval_minutes: Math.max(1, Math.round(intervalSeconds / 60)),
    daily_token_budget: item.daily_token_budget || 0,
    tools_mode: item.tools_mode === 'ask' ? 'ask' : 'auto',
    tokens_used_today: item.tokens_used_today || 0,
    last_verdict: item.last_verdict || '',
    last_reason: item.last_reason || '',
    last_run_at: item.last_run_at || '',
    melt_state: item.melt_state || '',
    melt_reason: item.melt_reason || '',
    cooldown_until: item.cooldown_until || '',
    consecutive_failures: item.consecutive_failures || 0,
  }
  return { ...base, snapshot: snapshotOf(base) }
}

function isDirty(row: RowState): boolean {
  return snapshotOf(row) !== row.snapshot
}

/** 判定结论 → 可读文案。未知结论**原样显示**，不吞掉（后端加新原因时能看见） */
function verdictLabel(raw: string): string {
  const map: Record<string, string> = {
    run: 'agent.superagent_verdict_run',
    skip: 'agent.superagent_verdict_skip',
    busy: 'agent.superagent_verdict_busy',
    user_active: 'agent.superagent_verdict_user_active',
    idle: 'agent.superagent_verdict_idle',
    not_due: 'agent.superagent_verdict_not_due',
    cooldown: 'agent.superagent_verdict_cooldown',
    melted: 'agent.superagent_verdict_melted',
    budget: 'agent.superagent_verdict_budget',
    disabled: 'agent.superagent_verdict_disabled',
  }
  const key = map[raw]
  return key ? t(key) : raw
}

function meltLabel(raw: string): string {
  if (raw === 'budget') return t('agent.superagent_melt_budget')
  if (raw === 'errors') return t('agent.superagent_melt_errors')
  return ''
}

/** 冷却剩余分钟；已过期返回 0 */
function cooldownLeft(iso: string): number {
  if (!iso) return 0
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return 0
  return Math.max(0, Math.ceil((at - nowTick.value) / 60000))
}

function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

async function loadSessions() {
  try {
    const { data } = await api.get('/v1/conversations')
    const payload = data?.data?.items ?? data?.data ?? []
    sessions.value = (Array.isArray(payload) ? payload : [])
      .map((c: { id?: string; title?: string }) => ({ id: String(c.id || ''), title: String(c.title || '') }))
      .filter((c: { id: string }) => c.id)
  } catch {
    // 会话列表拉不到不该让整个面板失败：接管配置仍可查看与保存
    sessions.value = []
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [takeovers, cfg, st] = await Promise.all([
      listTakeovers(),
      getTakeoverSettings(),
      getSuperagentStatus().catch(() => null),
    ])
    status.value = st
    settings.value = {
      enabled: cfg.enabled,
      max_sessions: cfg.max_sessions || maxSessions.value,
      day_token_budget: cfg.day_token_budget || 0,
      idle_minutes: cfg.idle_minutes || 60,
    }
    const titleMap = new Map(sessions.value.map((s) => [s.id, s.title]))
    rows.value = takeovers.map((item) => toRow(item, titleMap))
  } catch (err) {
    loadError.value = serverErrorMessage(err, t('agent.superagent_load_failed'))
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  if (savingSettings.value) return
  savingSettings.value = true
  try {
    const saved = await saveTakeoverSettings({
      enabled: settings.value.enabled,
      day_token_budget: settings.value.day_token_budget,
      idle_minutes: settings.value.idle_minutes,
    })
    settings.value.enabled = saved.enabled
    message.success(t('agent.superagent_settings_saved'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('agent.superagent_settings_save_failed')))
  } finally {
    savingSettings.value = false
  }
}

async function saveRow(row: RowState, extra: { clear_melt?: boolean } = {}) {
  if (savingId.value) return
  // 倒计时上限是 24 小时（后端同样拒绝），前端先拦住，省一次 422 往返
  if (row.schedule_kind === 'once' && row.once_delay_minutes > maxOnceMinutes.value) {
    message.warning(t('agent.superagent_once_too_far', { max: maxOnceMinutes.value }))
    return
  }
  if (row.schedule_kind === 'interval' && row.interval_minutes < minIntervalMinutes.value) {
    message.warning(t('agent.superagent_interval_too_short', { min: minIntervalMinutes.value }))
    return
  }

  savingId.value = row.id
  try {
    const result = await updateTakeover(row.id, {
      enabled: row.enabled,
      schedule_kind: row.schedule_kind,
      once_at:
        row.schedule_kind === 'once'
          ? new Date(Date.now() + row.once_delay_minutes * 60000).toISOString()
          : null,
      interval_seconds: row.interval_minutes * 60,
      daily_token_budget: row.daily_token_budget,
      tools_mode: row.tools_mode,
      ...extra,
    })
    // 用服务端返回值回填，而不是相信本地草稿 —— 后端可能降级或纠正
    const titleMap = new Map(sessions.value.map((s) => [s.id, s.title]))
    const fresh = toRow(result.takeover, titleMap)
    const idx = rows.value.findIndex((r) => r.id === row.id)
    if (idx >= 0) rows.value[idx] = fresh
    message.success(t('agent.superagent_saved'))
    // warnings 是"你可能是这么想的"，必须展示，不能因为保存成功就吞掉
    for (const warn of result.warnings || []) {
      message.warning(warn)
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('agent.superagent_save_failed')))
  } finally {
    savingId.value = ''
  }
}

async function removeRow(row: RowState) {
  if (savingId.value) return
  savingId.value = row.id
  try {
    await deleteTakeover(row.id)
    rows.value = rows.value.filter((r) => r.id !== row.id)
    message.success(t('agent.superagent_saved'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('agent.superagent_delete_failed')))
  } finally {
    savingId.value = ''
  }
}

async function addRows() {
  const picks = pickedSessionIds.value.filter((id) => !takenSessionIds.value.has(id))
  if (!picks.length) {
    message.warning(t('agent.superagent_session_required'))
    return
  }
  const room = maxSessions.value - rows.value.length
  if (room <= 0) {
    message.warning(t('agent.superagent_limit_reached', { max: maxSessions.value }))
    return
  }
  adding.value = true
  try {
    const titleMap = new Map(sessions.value.map((s) => [s.id, s.title]))
    const created: RowState[] = []
    for (const sid of picks.slice(0, room)) {
      const result = await createTakeover({
        session_id: sid,
        // 新行默认**关**：多一层显式动作，避免"我只是加了个会话，它就开始花钱了"
        enabled: false,
        schedule_kind: 'interval',
        interval_seconds: Math.max(3600, minIntervalMinutes.value * 60),
        tools_mode: 'auto',
        daily_token_budget: settings.value.day_token_budget || 0,
      })
      created.push(toRow(result.takeover, titleMap))
      for (const warn of result.warnings || []) {
        message.warning(warn)
      }
    }
    rows.value = [...created, ...rows.value]
    pickedSessionIds.value = []
    if (picks.length > room) {
      message.warning(t('agent.superagent_limit_reached', { max: maxSessions.value }))
    } else {
      message.success(t('agent.superagent_saved'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('agent.superagent_save_failed')))
  } finally {
    adding.value = false
  }
}

onMounted(async () => {
  await loadSessions()
  await load()
  // 冷却/剩余时间需要跟着走，30 秒刷新一次刻度（与 presence 心跳同频）
  tickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 30000)
})

onUnmounted(() => {
  if (tickTimer) clearInterval(tickTimer)
  tickTimer = null
})
</script>

<template>
  <div class="tk-block">
    <div class="tk-head">
      <RobotOutlined class="tk-icon" />
      <div>
        <div class="tk-title">
          {{ $t('agent.superagent_title') }}
        </div>
        <p class="tk-intro">
          {{ $t('agent.superagent_sub') }}
        </p>
      </div>
    </div>

    <!-- 引擎未启用：配置能存，但永远不会触发。不说明就是静默失效 -->
    <a-alert
      v-if="engineOff"
      type="warning"
      show-icon
      class="tk-alert"
      :message="$t('agent.superagent_engine_off_title')"
      :description="$t('agent.superagent_engine_off_body')"
    />

    <a-alert
      v-if="loadError"
      type="warning"
      show-icon
      class="tk-alert"
      :message="loadError"
    >
      <template #action>
        <a-button
          size="small"
          :loading="loading"
          @click="load"
        >
          {{ $t('common.retry') }}
        </a-button>
      </template>
    </a-alert>

    <a-spin v-else-if="loading && !rows.length">
      <div class="tk-spin-placeholder" />
    </a-spin>

    <template v-else>
      <!-- ── 用户级总开关与全局参数 ── -->
      <div class="tk-master">
        <div class="tk-master-row">
          <span class="tk-master-label">{{ $t('agent.superagent_master_title') }}</span>
          <a-switch v-model:checked="settings.enabled" />
          <span class="tk-hint">{{ $t('agent.superagent_master_hint') }}</span>
        </div>

        <div class="tk-grid">
          <label class="tk-field">
            <span class="tk-field-label">{{ $t('agent.superagent_idle_minutes') }}</span>
            <a-input-number
              v-model:value="settings.idle_minutes"
              :min="1"
              :max="1440"
              :step="10"
              class="tk-num"
            />
            <span class="tk-field-hint">{{ $t('agent.superagent_idle_minutes_hint') }}</span>
          </label>
          <label class="tk-field">
            <span class="tk-field-label">{{ $t('agent.superagent_day_budget') }}</span>
            <a-input-number
              v-model:value="settings.day_token_budget"
              :min="0"
              :step="10000"
              class="tk-num"
            />
            <span class="tk-field-hint">{{ $t('agent.superagent_day_budget_hint') }}</span>
          </label>
        </div>

        <div class="tk-master-actions">
          <a-button
            type="primary"
            size="small"
            :loading="savingSettings"
            @click="saveSettings"
          >
            {{ $t('common.save') }}
          </a-button>
        </div>
      </div>

      <a-alert
        type="info"
        show-icon
        class="tk-alert"
        :message="$t('agent.superagent_cost_warning')"
      />

      <!-- ── 已有接管 ── -->
      <div
        v-if="!rows.length"
        class="tk-empty"
      >
        {{ $t('agent.superagent_empty_takeovers') }}
      </div>

      <div
        v-for="row in rows"
        :key="row.id"
        class="tk-row"
      >
        <div class="tk-row-head">
          <ThunderboltOutlined class="tk-row-icon" />
          <span class="tk-row-title">{{ row.session_title }}</span>
          <a-tag
            :color="row.enabled ? 'green' : 'default'"
            class="tk-tag"
          >
            {{ row.enabled ? $t('agent.superagent_enabled_on') : $t('agent.superagent_enabled_off') }}
          </a-tag>
          <span class="tk-row-spacer" />
          <a-switch v-model:checked="row.enabled" />
        </div>

        <!-- 运行时状态：熔断与冷却必须显式，否则用户看到"开着但没动" -->
        <div class="tk-status">
          <a-tag
            v-if="row.melt_state"
            color="red"
          >
            {{ meltLabel(row.melt_state) }}
          </a-tag>
          <a-tag
            v-else-if="cooldownLeft(row.cooldown_until) > 0"
            color="orange"
          >
            {{ $t('agent.superagent_cooldown_until', { n: cooldownLeft(row.cooldown_until) }) }}
          </a-tag>
          <a-tag
            v-else-if="row.last_verdict"
            color="blue"
          >
            {{ verdictLabel(row.last_verdict) }}
          </a-tag>
          <span class="tk-status-item">
            {{ $t('agent.superagent_tokens_today', { n: row.tokens_used_today }) }}
          </span>
          <span class="tk-status-item">
            {{
              row.last_run_at
                ? $t('agent.superagent_last_run', { time: formatTime(row.last_run_at) })
                : $t('agent.superagent_never_run')
            }}
          </span>
          <span
            v-if="row.consecutive_failures > 0"
            class="tk-status-item tk-status-bad"
          >
            {{ $t('agent.superagent_consecutive_failures', { n: row.consecutive_failures }) }}
          </span>
        </div>
        <p
          v-if="row.melt_reason"
          class="tk-melt-reason"
        >
          {{ row.melt_reason }}
        </p>

        <!-- 配置 -->
        <div class="tk-grid">
          <div class="tk-field">
            <span class="tk-field-label">{{ $t('agent.superagent_schedule_kind') }}</span>
            <a-radio-group
              v-model:value="row.schedule_kind"
              size="small"
            >
              <a-radio-button value="once">
                {{ $t('agent.superagent_schedule_once') }}
              </a-radio-button>
              <a-radio-button value="interval">
                {{ $t('agent.superagent_schedule_interval') }}
              </a-radio-button>
            </a-radio-group>
          </div>

          <label
            v-if="row.schedule_kind === 'once'"
            class="tk-field"
          >
            <span class="tk-field-label">{{ $t('agent.superagent_once_delay') }}</span>
            <a-input-number
              v-model:value="row.once_delay_minutes"
              :min="1"
              :max="maxOnceMinutes"
              :step="10"
              class="tk-num"
            />
            <span class="tk-field-hint">{{ $t('agent.superagent_once_delay_hint', { max: maxOnceMinutes }) }}</span>
          </label>

          <label
            v-else
            class="tk-field"
          >
            <span class="tk-field-label">{{ $t('agent.superagent_interval_minutes') }}</span>
            <a-input-number
              v-model:value="row.interval_minutes"
              :min="minIntervalMinutes"
              :step="15"
              class="tk-num"
            />
            <span class="tk-field-hint">{{ $t('agent.superagent_interval_minutes_hint', { min: minIntervalMinutes }) }}</span>
          </label>

          <label class="tk-field">
            <span class="tk-field-label">{{ $t('agent.superagent_daily_budget') }}</span>
            <a-input-number
              v-model:value="row.daily_token_budget"
              :min="0"
              :step="10000"
              class="tk-num"
            />
            <span class="tk-field-hint">{{ $t('agent.superagent_daily_budget_hint') }}</span>
          </label>

          <label class="tk-field">
            <span class="tk-field-label">{{ $t('agent.superagent_tools_mode') }}</span>
            <a-select
              v-model:value="row.tools_mode"
              size="small"
              class="tk-select"
            >
              <a-select-option value="ask">
                {{ $t('agent.superagent_tools_mode_ask') }}
              </a-select-option>
              <a-select-option value="auto">
                {{ $t('agent.superagent_tools_mode_auto') }}
              </a-select-option>
            </a-select>
            <span class="tk-field-hint">{{ $t('agent.superagent_tools_mode_hint') }}</span>
          </label>
        </div>

        <div class="tk-row-actions">
          <a-button
            type="primary"
            size="small"
            :disabled="!isDirty(row)"
            :loading="savingId === row.id"
            @click="saveRow(row)"
          >
            {{ $t('common.save') }}
          </a-button>
          <a-button
            v-if="row.melt_state"
            size="small"
            @click="saveRow(row, { clear_melt: true })"
          >
            {{ $t('agent.superagent_clear_melt') }}
          </a-button>
          <span
            v-if="isDirty(row)"
            class="tk-hint"
          >
            {{ $t('agent.superagent_dirty_hint') }}
          </span>
          <span class="tk-row-spacer" />
          <a-popconfirm
            :title="$t('agent.superagent_remove_confirm')"
            :ok-text="$t('common.confirm')"
            :cancel-text="$t('common.cancel')"
            @confirm="removeRow(row)"
          >
            <a-button
              danger
              size="small"
              :loading="savingId === row.id"
            >
              {{ $t('common.delete') }}
            </a-button>
          </a-popconfirm>
        </div>
      </div>

      <!-- ── 新增 ── -->
      <div class="tk-add">
        <div class="tk-add-title">
          {{ $t('agent.superagent_add_title') }}
        </div>
        <p class="tk-add-hint">
          {{ $t('agent.superagent_add_hint', { max: maxSessions }) }}
        </p>
        <a-alert
          v-if="atLimit"
          type="warning"
          show-icon
          class="tk-alert"
          :message="$t('agent.superagent_limit_reached', { max: maxSessions })"
        />
        <a-alert
          v-else-if="!addableSlots.length"
          type="info"
          show-icon
          class="tk-alert"
          :message="$t('agent.superagent_add_none_left')"
        />
        <template v-else>
          <a-select
            v-model:value="pickedSessionIds"
            mode="multiple"
            show-search
            option-filter-prop="label"
            class="tk-add-select"
            :placeholder="$t('agent.superagent_session_placeholder')"
            :max-tag-count="4"
          >
            <a-select-option
              v-for="s in addableSlots"
              :key="s.id"
              :value="s.id"
              :label="s.title || s.id"
            >
              {{ s.title || s.id }}
            </a-select-option>
          </a-select>
          <a-button
            type="primary"
            size="small"
            :loading="adding"
            :disabled="!pickedSessionIds.length"
            @click="addRows"
          >
            <template #icon>
              <PlusOutlined />
            </template>
            {{ $t('agent.superagent_add_button') }}
          </a-button>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.tk-block { padding: 4px 0 20px; }
.tk-head { display: flex; gap: 10px; margin-bottom: 16px; }
.tk-icon { font-size: 20px; color: var(--primary); margin-top: 2px; }
.tk-title { font-weight: var(--fw-semibold); color: var(--text-primary); }
.tk-intro { margin: 4px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.tk-alert { margin-bottom: 12px; }
.tk-spin-placeholder { min-height: 120px; }
.tk-hint { color: var(--text-secondary); font-size: 12px; }

.tk-master {
  padding: 12px;
  margin-bottom: 16px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
}
.tk-master-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.tk-master-label { font-weight: var(--fw-medium, 500); color: var(--text-primary); }
.tk-master-actions { display: flex; justify-content: flex-end; }

/* 两列自适应：字段带说明行，用 grid 让同排的说明对齐 */
.tk-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.tk-field { display: flex; flex-direction: column; gap: 4px; }
.tk-field-label { color: var(--text-secondary); font-size: 12px; }
.tk-field-hint { color: var(--text-secondary); font-size: 11px; line-height: 1.5; }
.tk-num { width: 100%; }
.tk-select { width: 100%; }

.tk-empty {
  padding: 24px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 13px;
  border: var(--hairline-width) dashed var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
}

.tk-row {
  padding: 12px;
  margin-bottom: 12px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
}
.tk-row-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.tk-row-icon { color: var(--primary); }
.tk-row-title { font-weight: var(--fw-medium, 500); color: var(--text-primary); }
.tk-tag { margin-inline-start: 0; }
.tk-row-spacer { flex: 1; }

.tk-status { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.tk-status-item { color: var(--text-secondary); font-size: 12px; }
.tk-status-bad { color: var(--danger, #d4380d); }
.tk-melt-reason { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }

.tk-row-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; }

.tk-add {
  padding-top: 16px;
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
}
.tk-add-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 4px; }
.tk-add-hint { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
.tk-add-select { width: 100%; margin-bottom: 10px; }
</style>
