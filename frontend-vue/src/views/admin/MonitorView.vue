<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Alert, Button, Card, InputNumber, RadioButton, RadioGroup, Switch, Tag } from 'ant-design-vue'

import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import AdminPage from '@/components/admin/AdminPage.vue'
import DashboardView from './DashboardView.vue'
import Sparkline from '@/components/admin/Sparkline.vue'
import PerformancePanel from '@/components/admin/monitor/PerformancePanel.vue'
import QueuePanel from '@/components/admin/monitor/QueuePanel.vue'
import CachePanel from '@/components/admin/monitor/CachePanel.vue'
import { fmtDateTime, fmtNum, seriesOf } from '@/components/admin/monitor/format'
import { useMonitorOverview } from '@/composables/useMonitorOverview'
import { getMonitorHistory, getRetention, previewRetention, saveSettings } from '@/api/admin'
import type {
  AdminRetentionInfo,
  MonitorHistory,
  MonitorWindow,
  RetentionChange,
  RetentionConfig,
} from '@/api/admin'

/**
 * 运行时监控 = 性能 / 队列 / 缓存三个仪表盘 + 两张"跨重启"卡片。
 *
 * 三个面板共用**同一份** overview（见 useMonitorOverview）：一次采集、一个
 * generated_at，10s 轮询可暂停，时间窗 5m/15m/60m 切换。
 *
 * 另外两张卡片来自 P2/P0-4：
 *  - 历史趋势：PG 分钟聚合（跨副本、跨重启），只覆盖**已走完的分钟**；
 *  - 保留期回收量：后台每小时一轮清理的结果（本进程视角）。
 * 两者都与 10s 轮询解耦：历史趋势不需要每 10s 重查，保留期卡片按需刷新。
 */
const { t } = useI18n()
const {
  overview,
  loading,
  error,
  windowRange,
  autoRefresh,
  updatedAt,
  refresh,
  setWindow,
  setAutoRefresh,
} = useMonitorOverview()

const tabs = [
  // 原「仪表盘」与「运行时监控」合并为一个入口：总览是"现在怎么样"，
  // 三个面板是"慢/堵/缓存"的细分视图。放在同一页的 Tabs 里，避免两个菜单项
  // 各自轮询同一批事实（连接数 / 队列 / 缓存 / 延迟）。
  { key: 'overview', label: t('common.dashboard'), comp: DashboardView },
  { key: 'performance', label: t('admin.performance'), comp: PerformancePanel },
  { key: 'queue', label: t('admin.queue'), comp: QueuePanel },
  { key: 'cache', label: t('admin.cache'), comp: CachePanel },
]

const ranges: MonitorWindow[] = ['5m', '15m', '60m']

const history = ref<MonitorHistory | null>(null)
const historyError = ref('')
const historyLoading = ref(false)

const retention = ref<AdminRetentionInfo | null>(null)
const retentionError = ref('')
const retentionSaving = ref(false)
const retentionSaved = ref(false)
const retentionForm = ref<RetentionConfig>({
  usage_days: 180,
  share_days: 90,
  checkpoint_days: 30,
})

const historySeries = computed(() => seriesOf((history.value?.points ?? []).map(p => p.count)))
const historyErrorSeries = computed(() => seriesOf((history.value?.points ?? []).map(p => p.errors)))
const historyHasData = computed(() => historySeries.value.length > 0)

const lastRun = computed(() => retention.value?.last_run ?? null)
const retentionTotal = computed(() => lastRun.value?.total_deleted ?? 0)

async function loadHistory() {
  historyLoading.value = true
  historyError.value = ''
  try {
    history.value = await getMonitorHistory(24)
  } catch (e) {
    // 历史趋势失败不影响实时面板：只在本卡片内报错。
    historyError.value = e instanceof Error ? e.message : String(e)
  } finally {
    historyLoading.value = false
  }
}

async function loadRetention() {
  retentionError.value = ''
  try {
    const info = await getRetention()
    retention.value = info
    retentionForm.value = { ...info.config }
  } catch (e) {
    retentionError.value = e instanceof Error ? e.message : String(e)
  }
}

// H82：收紧保留期 = 下一轮清理会多删一批历史数据。所以保存**必须**分两步：
//   1. 先预览（只读）：拿到"旧值 → 新值"和按类型的待删行数；
//   2. 收紧时要求用户明确勾选确认并填写原因，才真正写配置。
// 取消预览不会调用 saveSettings —— "取消"在旧实现里等于"已经保存过了"，
// 因为保存与展示是同一个动作。
const retentionPreview = ref<{
  changes: RetentionChange[]
  impact: Record<string, number>
  requiresConfirm: boolean
  effective: string
} | null>(null)
const retentionConfirmOpen = ref(false)
const retentionConfirmChecked = ref(false)
const retentionReason = ref('')
const retentionPreviewing = ref(false)

function retentionImpactEntries(): Array<{ key: string, label: string, value: number }> {
  const impact = retentionPreview.value?.impact ?? {}
  const labelOf = (key: string) => {
    switch (key) {
      case 'usage_events': return t('admin.retention_impact_usage')
      case 'conversation_shares': return t('admin.retention_impact_shares')
      case 'checkpoints': return t('admin.retention_impact_checkpoints')
      default: return key
    }
  }
  return Object.entries(impact).map(([key, value]) => ({ key, label: labelOf(key), value }))
}

/** 第一步：只读预览（不写配置）。 */
async function previewRetentionChange(): Promise<boolean> {
  retentionPreviewing.value = true
  retentionError.value = ''
  try {
    const preview = await previewRetention({ ...retentionForm.value })
    retentionPreview.value = {
      changes: preview.changes ?? [],
      impact: preview.impact ?? {},
      requiresConfirm: Boolean(preview.requires_confirm),
      effective: preview.changes_effective || '',
    }
    retentionConfirmChecked.value = false
    retentionReason.value = ''
    return true
  } catch (e) {
    retentionError.value = e instanceof Error ? e.message : String(e)
    return false
  } finally {
    retentionPreviewing.value = false
  }
}

/** 第二步：真正保存。收紧时带上 confirm 与 reason（后端缺任一项都会拒绝）。 */
async function saveRetention() {
  if (!(await previewRetentionChange())) return
  const needsConfirm = Boolean(retentionPreview.value?.requiresConfirm)
  if (needsConfirm) {
    // 打开确认框，等用户勾选 + 填原因；此时**尚未**写入任何配置。
    retentionConfirmOpen.value = true
    return
  }
  await commitRetentionSave()
}

/** 用户确认后提交（或放宽时直接提交）。 */
async function commitRetentionSave() {
  const needsConfirm = Boolean(retentionPreview.value?.requiresConfirm)
  if (needsConfirm && (!retentionConfirmChecked.value || !retentionReason.value.trim())) {
    retentionError.value = t('admin.retention_confirm_required')
    return
  }
  retentionSaving.value = true
  retentionSaved.value = false
  retentionError.value = ''
  try {
    const payload: Record<string, unknown> = { ...retentionForm.value }
    if (needsConfirm) {
      payload.confirm = true
      payload.reason = retentionReason.value.trim()
    }
    await saveSettings('retention', payload)
    retentionSaved.value = true
    retentionConfirmOpen.value = false
    await loadRetention()
  } catch (e) {
    // 保存失败：不动表单，让用户看到原因后重试（后端是先校验后落库，不会半写）。
    retentionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    retentionSaving.value = false
  }
}

function cancelRetentionConfirm() {
  retentionConfirmOpen.value = false
  retentionConfirmChecked.value = false
  retentionReason.value = ''
  // 取消 = 什么都没写：把表单恢复成服务器当前值，避免"看起来改了但没保存"的错觉。
  const current = retention.value?.config
  if (current) retentionForm.value = { ...current }
}

function refreshAll() {
  refresh()
  loadHistory()
  loadRetention()
}

onMounted(() => {
  loadHistory()
  loadRetention()
})

function onRangeChange(e: unknown) {
  const value = (e as { target?: { value?: MonitorWindow } }).target?.value
  if (value) setWindow(value)
}

function onAutoRefreshChange(checked: boolean | string | number) {
  setAutoRefresh(Boolean(checked))
}

function rangeLabel(r: MonitorWindow): string {
  return t(`admin.monitor_window_${r}`)
}
</script>

<template>
  <AdminPage
    :title="t('admin.runtime_monitoring')"
    data-test="monitor-view"
  >
    <div
      class="monitor-toolbar"
      data-test="monitor-toolbar"
    >
      <div class="monitor-toolbar__group">
        <span class="monitor-toolbar__label">{{ t('admin.monitor_window_range') }}</span>
        <RadioGroup
          :value="windowRange"
          button-style="solid"
          size="small"
          @change="onRangeChange"
        >
          <RadioButton
            v-for="r in ranges"
            :key="r"
            :value="r"
            :data-test="`window-${r}`"
          >
            {{ rangeLabel(r) }}
          </RadioButton>
        </RadioGroup>
      </div>

      <div class="monitor-toolbar__group">
        <span class="monitor-toolbar__label">{{ t('admin.monitor_auto_refresh') }}</span>
        <Switch
          :checked="autoRefresh"
          size="small"
          data-test="auto-refresh"
          @change="onAutoRefreshChange"
        />
        <span
          class="monitor-toolbar__muted"
          data-test="auto-refresh-state"
        >{{ autoRefresh ? t('admin.monitor_refresh_10s') : t('admin.monitor_paused') }}</span>
      </div>

      <div class="monitor-toolbar__group">
        <Button
          size="small"
          :loading="loading"
          data-test="monitor-refresh"
          @click="refreshAll"
        >
          {{ t('common.refresh') }}
        </Button>
        <span
          class="monitor-toolbar__muted"
          data-test="monitor-updated-at"
        >{{ t('admin.monitor_updated_at') }} {{ updatedAt ? fmtDateTime(updatedAt) : '—' }}</span>
      </div>

      <Tag
        v-if="overview?.partial"
        color="orange"
        data-test="monitor-partial"
      >
        {{ t('admin.monitor_partial') }}
      </Tag>
    </div>

    <Alert
      v-if="error"
      class="monitor-error"
      type="error"
      show-icon
      data-test="monitor-error"
      :message="t('admin.monitor_fetch_failed')"
      :description="error"
    />

    <!-- P2：历史趋势（PG 分钟聚合；跨副本、跨重启） -->
    <Card
      class="history-card"
      size="small"
      data-test="monitor-history"
      :title="t('admin.history_title')"
    >
      <template #extra>
        <span class="monitor-toolbar__muted">{{ t('admin.history_window_24h') }}</span>
      </template>
      <Alert
        v-if="historyError"
        type="warning"
        show-icon
        :message="historyError"
      />
      <div
        v-else-if="history && !historyHasData"
        class="history-empty"
        data-test="history-empty"
      >
        {{ t('admin.monitor_no_samples') }}（{{ t('admin.history_from_rollup') }}）
      </div>
      <template v-else>
        <div
          class="history-grid"
          data-test="history-points"
        >
          <div class="history-series">
            <div class="history-series__label">
              {{ t('admin.monitor_requests') }}
              <span
                class="history-series__total"
                data-test="history-total"
              >{{ fmtNum(history?.total_count ?? 0) }}</span>
            </div>
            <Sparkline
              :values="historySeries"
              :label="t('admin.history_title')"
              :empty-label="t('admin.monitor_no_samples')"
            />
          </div>
          <div class="history-series">
            <div class="history-series__label">
              5xx
              <span
                class="history-series__total"
                data-test="history-errors"
              >{{ fmtNum(history?.total_errors ?? 0) }}</span>
            </div>
            <Sparkline
              :values="historyErrorSeries"
              :label="t('admin.monitor_error_rate')"
              :empty-label="t('admin.monitor_no_samples')"
              tone="danger"
            />
          </div>
        </div>
        <div class="history-meta">
          <span>{{ t('admin.history_from_rollup') }}</span>
          <span v-if="history">· {{ history.points.length }} {{ t('admin.history_points') }}</span>
        </div>
      </template>
    </Card>

    <!-- P0-4：保留期回收量 + 配置（本页唯一一处 retention 表单） -->
    <Card
      class="retention-card"
      size="small"
      data-test="retention-card"
      :title="t('admin.retention_title')"
    >
      <template #extra>
        <span class="monitor-toolbar__muted">
          {{ t('admin.retention_interval', { minutes: retention?.interval_minutes ?? 60 }) }}
        </span>
      </template>

      <Alert
        v-if="retentionError"
        type="warning"
        show-icon
        :message="retentionError"
      />

      <div class="retention-summary">
        <div class="retention-stat">
          <div class="retention-stat__label">
            {{ t('admin.retention_total_deleted') }}
          </div>
          <div
            class="retention-stat__value"
            data-test="retention-total"
          >
            {{ lastRun ? fmtNum(retentionTotal) : '—' }}
          </div>
          <div class="retention-stat__hint">
            {{ lastRun ? fmtDateTime(lastRun.run_at) : t('admin.retention_never_run') }}
          </div>
        </div>
        <div
          v-for="task in lastRun?.tasks ?? []"
          :key="task.table"
          class="retention-stat"
        >
          <div class="retention-stat__label">
            {{ task.table }}
          </div>
          <div
            class="retention-stat__value"
            :data-test="`retention-deleted-${task.table}`"
          >
            {{ fmtNum(task.deleted) }}
          </div>
          <div class="retention-stat__hint">
            {{ task.error || t('admin.retention_batches', { n: task.batches }) }}
          </div>
        </div>
        <div
          v-if="lastRun?.checkpoint"
          class="retention-stat"
        >
          <div class="retention-stat__label">
            checkpoint CAS
          </div>
          <div
            class="retention-stat__value"
            data-test="retention-deleted-checkpoints"
          >
            {{ fmtNum(lastRun.checkpoint.deleted) }}
          </div>
          <div class="retention-stat__hint">
            {{ lastRun.checkpoint.error || t('admin.retention_retention_days', { days: lastRun.checkpoint.retention_days }) }}
          </div>
        </div>
      </div>

      <div class="retention-form">
        <label class="retention-field">
          <span>{{ t('admin.retention_usage_days') }}</span>
          <InputNumber
            v-model:value="retentionForm.usage_days"
            :min="1"
            :max="3650"
            size="small"
            data-test="retention-usage-days"
          />
        </label>
        <label class="retention-field">
          <span>{{ t('admin.retention_share_days') }}</span>
          <InputNumber
            v-model:value="retentionForm.share_days"
            :min="1"
            :max="3650"
            size="small"
            data-test="retention-share-days"
          />
        </label>
        <label class="retention-field">
          <span>{{ t('admin.retention_checkpoint_days') }}</span>
          <InputNumber
            v-model:value="retentionForm.checkpoint_days"
            :min="1"
            :max="3650"
            size="small"
            data-test="retention-checkpoint-days"
          />
        </label>
        <Button
          size="small"
          type="primary"
          :loading="retentionSaving"
          data-test="retention-save"
          @click="saveRetention"
        >
          {{ t('common.save') }}
        </Button>
        <span
          v-if="retentionSaved"
          class="retention-saved"
          data-test="retention-saved"
        >{{ t('admin.retention_saved') }}</span>
        <span
          v-if="retentionPreviewing"
          class="retention-meta"
          data-test="retention-previewing"
        >{{ t('admin.retention_previewing') }}</span>
      </div>

      <!-- H82：预览结果（只读，尚未写任何配置） -->
      <div
        v-if="retentionPreview && retentionPreview.changes.length"
        class="retention-preview"
        data-test="retention-preview"
      >
        <div class="retention-preview__title">
          {{ t('admin.retention_preview_title') }}
          <span
            v-if="retentionPreview.requiresConfirm"
            class="retention-preview__warn"
          >{{ t('admin.retention_requires_confirm') }}</span>
        </div>
        <ul class="retention-preview__changes">
          <li
            v-for="c in retentionPreview.changes"
            :key="c.field"
            :class="{ tightened: c.tightened }"
          >
            {{ t(`admin.retention_${c.field}`) }}: {{ c.old }} → {{ c.new }}
            <span v-if="c.tightened">（{{ t('admin.retention_tightened') }}）</span>
          </li>
        </ul>
        <div class="retention-preview__impact">
          <span>{{ t('admin.retention_impact_title') }}</span>
          <span
            v-for="entry in retentionImpactEntries()"
            :key="entry.key"
            class="retention-preview__impact-item"
          >
            {{ entry.label }}: {{ entry.value < 0 ? t('admin.retention_impact_unknown') : entry.value }}
          </span>
        </div>
        <div class="retention-preview__effective">{{ retentionPreview.effective }}</div>
      </div>

      <!-- H82：收紧必须显式确认 + 原因 -->
      <div
        v-if="retentionConfirmOpen"
        class="retention-confirm"
        data-test="retention-confirm"
      >
        <label class="retention-confirm__check">
          <input
            v-model="retentionConfirmChecked"
            type="checkbox"
            data-test="retention-confirm-check"
          >
          <span>{{ t('admin.retention_confirm_label') }}</span>
        </label>
        <input
          v-model="retentionReason"
          class="retention-confirm__reason"
          :placeholder="t('admin.retention_reason_placeholder')"
          maxlength="200"
          data-test="retention-reason"
        >
        <div class="retention-confirm__actions">
          <Button
            size="small"
            type="primary"
            danger
            :loading="retentionSaving"
            data-test="retention-confirm-submit"
            @click="commitRetentionSave"
          >
            {{ t('admin.retention_confirm_submit') }}
          </Button>
          <Button
            size="small"
            data-test="retention-confirm-cancel"
            @click="cancelRetentionConfirm"
          >
            {{ t('common.cancel') }}
          </Button>
        </div>
      </div>
      <div
        v-if="retentionError"
        class="retention-error"
        data-test="retention-error"
      >{{ retentionError }}</div>
      <div class="history-meta">
        {{ retention?.note || t('admin.retention_note') }}
      </div>
    </Card>

    <AdminTabbedView
      route-path="/admin/monitor"
      default-tab="performance"
      :tabs="tabs"
    />
  </AdminPage>
</template>

<style scoped>
/* H82：保留期预览与确认区 */
.retention-preview {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  font-size: 12px;
}
.retention-preview__title {
  font-weight: 600;
  margin-bottom: 4px;
}
.retention-preview__warn {
  margin-inline-start: 8px;
  color: var(--color-warning);
}
.retention-preview__changes {
  margin: 0 0 4px;
  padding-inline-start: 18px;
}
.retention-preview__changes .tightened {
  color: var(--color-warning);
}
.retention-preview__impact-item {
  margin-inline-end: 12px;
}
.retention-preview__effective {
  color: var(--text-secondary);
}
.retention-confirm {
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--color-warning);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.retention-confirm__check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.retention-confirm__reason {
  padding: 4px 6px;
  font-size: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
}
.retention-confirm__actions {
  display: flex;
  gap: 8px;
}
.retention-error {
  margin-top: 4px;
  color: var(--color-error);
  font-size: 12px;
}
.retention-meta {
  font-size: 12px;
  color: var(--text-secondary);
}
.monitor-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3) var(--space-5);
  padding: var(--space-2) var(--space-1) var(--space-3);
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: var(--space-3);
}

.monitor-toolbar__group {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.monitor-toolbar__label {
  color: var(--text-secondary);
  font-size: var(--fs-sm, 13px);
}

.monitor-toolbar__muted {
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
}

.monitor-error { margin-bottom: var(--space-3); }

.history-card,
.retention-card { margin-bottom: var(--space-3); }

.history-empty {
  color: var(--text-quaternary);
  font-size: var(--fs-sm, 13px);
  padding: var(--space-2) 0;
}

.history-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-4);
}

.history-series__label {
  display: flex;
  justify-content: space-between;
  color: var(--text-secondary);
  font-size: var(--fs-sm, 13px);
  margin-bottom: var(--space-1);
}

.history-series__total { color: var(--text-primary); font-variant-numeric: tabular-nums; }

.history-meta {
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
  margin-top: var(--space-2);
}

.retention-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.retention-stat__label { color: var(--text-secondary); font-size: var(--fs-sm, 13px); }
.retention-stat__value { font-size: var(--fs-xl, 20px); font-variant-numeric: tabular-nums; }
.retention-stat__hint { color: var(--text-quaternary); font-size: var(--fs-xs, 12px); }

.retention-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.retention-field {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--text-secondary);
  font-size: var(--fs-sm, 13px);
}

.retention-saved { color: var(--color-success, #52c41a); font-size: var(--fs-xs, 12px); }
</style>
