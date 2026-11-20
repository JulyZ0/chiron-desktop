<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Card, Descriptions, DescriptionsItem, Spin, Tag } from 'ant-design-vue'

import Sparkline from '@/components/admin/Sparkline.vue'
import SectionStatus from './SectionStatus.vue'
import { useMonitorOverview } from '@/composables/useMonitorOverview'
import { UNKNOWN, fmtDuration, fmtNum } from './format'

/**
 * 「队列」面板。
 *
 * 深度 / pending / lag 来自 Redis Stream 命令（XLEN、XINFO GROUPS），最老等待来自
 * XRANGE 首条的 ID 时间戳；run 计数来自 agent_runs（DB）与 engine:run:* 租约（Redis）。
 * 任何一项读不到都显示「—」，不用 0 顶替 —— 队列深度 0 与"Redis 读不到"是两种
 * 完全相反的结论。
 */
const { t } = useI18n()
const { overview, loading, history } = useMonitorOverview()

const queue = computed(() => overview.value?.queue ?? null)
const runState = computed(() => queue.value?.run_state ?? null)
const streams = computed(() => queue.value?.streams ?? [])

const engineStream = computed(() => streams.value.find(s => s.name === 'engine') ?? null)
const queueTrend = computed(() => history.value.queueDepth.slice())

function oldestWait(row: { oldest_wait_seconds: number | null }): string {
  return row.oldest_wait_seconds === null ? UNKNOWN : fmtDuration(row.oldest_wait_seconds)
}
</script>

<template>
  <div
    class="monitor-panel"
    data-test="queue-panel"
  >
    <Spin :spinning="loading && !overview">
      <div
        v-if="!overview"
        class="monitor-panel__empty"
        data-test="queue-empty"
      >
        {{ loading ? t('common.loading') : UNKNOWN }}
      </div>

      <template v-else-if="queue">
        <SectionStatus :meta="queue" />

        <div class="kpi-grid">
          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_queue_depth') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-queue-depth"
            >
              {{ engineStream ? fmtNum(engineStream.depth) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              engine:tasks · XLEN
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_pending') }}
            </div>
            <div class="kpi-card__value">
              {{ engineStream ? fmtNum(engineStream.pending) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              {{ t('admin.monitor_lag') }}: {{ engineStream ? fmtNum(engineStream.lag) : UNKNOWN }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_oldest_wait') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-oldest-wait"
            >
              {{ engineStream ? oldestWait(engineStream) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              XRANGE (oldest entry)
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_active_runs') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-active-runs"
            >
              {{ runState ? fmtNum(runState.active) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              agent_runs · {{ t('admin.monitor_run_leases') }} {{ runState ? fmtNum(runState.run_leases) : UNKNOWN }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_failed_1h') }}
            </div>
            <div class="kpi-card__value">
              {{ runState ? fmtNum(runState.failed_last_hour) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              agent_runs.status='failed'
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_timeout_1h') }}
            </div>
            <div class="kpi-card__value">
              {{ runState ? fmtNum(runState.timeout_last_hour) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              subagent_runs.error_code='timeout'
            </div>
          </Card>
        </div>

        <Card
          class="trend-card"
          :title="t('admin.monitor_queue_trend')"
        >
          <Sparkline
            :values="queueTrend"
             :label="t('admin.monitor_queue_trend')"
            :empty-label="t('admin.monitor_scope_note')"
            tone="accent"
          />
          <div class="monitor-panel__hint">
            {{ t('admin.monitor_scope_note') }}
          </div>
        </Card>

        <Card
          class="stream-card"
          :title="t('admin.monitor_streams')"
        >
          <div
            v-if="streams.length === 0"
            class="monitor-panel__empty"
            data-test="streams-empty"
          >
            <Tag color="red">
              {{ t('admin.monitor_status_unavailable') }}
            </Tag>
            <span class="monitor-panel__hint">{{ t('admin.monitor_no_streams') }}</span>
          </div>
          <div
            v-else
            class="table-scroll"
          >
            <table class="monitor-table">
              <thead>
                <tr>
                  <th scope="col">{{ t('common.name') }}</th>
                  <th scope="col">{{ t('admin.monitor_queue_depth') }}</th>
                  <th scope="col">{{ t('admin.monitor_pending') }}</th>
                  <th scope="col">{{ t('admin.monitor_lag') }}</th>
                  <th scope="col">{{ t('admin.monitor_consumers') }}</th>
                  <th scope="col">{{ t('admin.monitor_dlq') }}</th>
                  <th scope="col">{{ t('admin.monitor_oldest_wait') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in streams"
                  :key="row.key"
                  :data-test="`stream-row-${row.name}`"
                >
                  <td>
                    <code>{{ row.name }}</code>
                    <div class="monitor-table__sub">
                      {{ row.key }}
                    </div>
                  </td>
                  <td>{{ fmtNum(row.depth) }}</td>
                  <td>{{ fmtNum(row.pending) }}</td>
                  <td>{{ fmtNum(row.lag) }}</td>
                  <td>{{ fmtNum(row.consumers) }}</td>
                  <td>{{ fmtNum(row.dlq_depth) }}</td>
                  <td>{{ oldestWait(row) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>

        <Card
          class="stream-card"
          :title="t('admin.monitor_run_state')"
        >
          <Descriptions
            v-if="runState"
            bordered
            size="small"
            :column="1"
          >
            <DescriptionsItem :label="t('admin.monitor_active_runs')">
              {{ fmtNum(runState.active) }}
            </DescriptionsItem>
            <DescriptionsItem :label="t('admin.monitor_awaiting_input')">
              {{ fmtNum(runState.awaiting_input) }}
            </DescriptionsItem>
            <DescriptionsItem :label="t('admin.monitor_run_leases')">
              {{ fmtNum(runState.run_leases) }}
            </DescriptionsItem>
            <DescriptionsItem :label="t('admin.monitor_sources')">
              {{ runState.source || UNKNOWN }}
            </DescriptionsItem>
          </Descriptions>
          <div
            v-else
            class="monitor-panel__empty"
            data-test="run-state-unknown"
          >
            {{ UNKNOWN }}
          </div>
        </Card>
      </template>
    </Spin>
  </div>
</template>

<style scoped>
.monitor-panel { padding: 0; }

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--space-4);
}

.kpi-card__label {
  color: var(--text-tertiary);
  font-size: var(--fs-xs, 12px);
  margin-bottom: var(--space-1);
}

.kpi-card__value {
  font-size: var(--fs-2xl, 24px);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.kpi-card__hint,
.monitor-panel__hint {
  margin-top: var(--space-1);
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
  overflow-wrap: anywhere;
}

.trend-card,
.stream-card {
  margin-top: var(--space-4);
}

.monitor-panel__empty {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  color: var(--text-quaternary);
  padding: var(--space-4) 0;
}

.table-scroll { overflow-x: auto; }

.monitor-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm, 13px);
  font-variant-numeric: tabular-nums;
}

.monitor-table th,
.monitor-table td {
  text-align: start;
  padding: var(--space-2);
  border-bottom: 1px solid var(--border-subtle);
  white-space: nowrap;
}

.monitor-table th {
  color: var(--text-tertiary);
  font-weight: var(--fw-medium);
}

.monitor-table__sub {
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
}
</style>
