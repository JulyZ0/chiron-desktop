<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { Alert, Button, RadioButton, RadioGroup, Switch, Tag } from 'ant-design-vue'

import AdminTabbedView from '@/components/admin/AdminTabbedView.vue'
import PerformancePanel from '@/components/admin/monitor/PerformancePanel.vue'
import QueuePanel from '@/components/admin/monitor/QueuePanel.vue'
import CachePanel from '@/components/admin/monitor/CachePanel.vue'
import { fmtDateTime } from '@/components/admin/monitor/format'
import { useMonitorOverview } from '@/composables/useMonitorOverview'
import type { MonitorWindow } from '@/api/admin'

/**
 * 运行时监控 = 性能 / 队列 / 缓存三个仪表盘。
 *
 * 三个面板共用**同一份** overview（见 useMonitorOverview）：一次采集、一个
 * generated_at，10s 轮询可暂停，时间窗 5m/15m/60m 切换。旧版三页各自请求各自的
 * 端点（/performance、/queue、/cache/stats）且时间戳不同步，排障时对不上同一时刻。
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
  { key: 'performance', label: t('admin.performance'), comp: PerformancePanel },
  { key: 'queue', label: t('admin.queue'), comp: QueuePanel },
  { key: 'cache', label: t('admin.cache'), comp: CachePanel },
]

const ranges: MonitorWindow[] = ['5m', '15m', '60m']

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
  <div
    class="monitor-view"
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
          @click="refresh"
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

    <AdminTabbedView
      route-path="/admin/monitor"
      default-tab="performance"
      :tabs="tabs"
    />
  </div>
</template>

<style scoped>
.monitor-view { padding: 0 4px; }

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
</style>
