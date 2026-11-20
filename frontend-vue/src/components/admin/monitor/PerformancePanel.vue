<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Card, Descriptions, DescriptionsItem, Spin, Tag } from 'ant-design-vue'

import Sparkline from '@/components/admin/Sparkline.vue'
import SectionStatus from './SectionStatus.vue'
import { useMonitorOverview } from '@/composables/useMonitorOverview'
import { UNKNOWN, fmtDuration, fmtMB, fmtMs, fmtNum, fmtPct, seriesOf } from './format'

/**
 * 「性能」面板 —— 全部数字来自 GET /v1/admin/monitor/overview 的 performance 区块。
 *
 * 口径都写在卡片副标题里（时间窗、样本量、来源），因为这一页最常见的误读是
 * 把「近 15 分钟」当成「进程启动以来」，或者把「无样本」当成「延迟为 0」。
 */
const { t } = useI18n()
const { overview, loading } = useMonitorOverview()

const perf = computed(() => overview.value?.performance ?? null)
const reqs = computed(() => perf.value?.requests ?? null)

const requestTrend = computed(() => seriesOf((reqs.value?.buckets ?? []).map(b => b.count)))
const latencyTrend = computed(() => seriesOf((reqs.value?.buckets ?? []).map(b => b.p95_ms)))
const errorTrend = computed(() => seriesOf((reqs.value?.buckets ?? []).map(b => b.errors)))

const sampleNote = computed(() => {
  if (!reqs.value) return ''
  if (!reqs.value.has_samples) return t('admin.monitor_no_samples')
  return `${t('admin.monitor_samples')}: ${fmtNum(reqs.value.samples)}`
})

const engine = computed(() => perf.value?.engine ?? null)
const pool = computed(() => perf.value?.db_pool ?? null)
</script>

<template>
  <div
    class="monitor-panel"
    data-test="performance-panel"
  >
    <Spin :spinning="loading && !overview">
      <div
        v-if="!overview"
        class="monitor-panel__empty"
        data-test="performance-empty"
      >
        {{ loading ? t('common.loading') : UNKNOWN }}
      </div>

      <template v-else-if="perf">
        <SectionStatus :meta="perf" />

        <div class="kpi-grid">
          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_requests') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-requests"
            >
              {{ reqs?.has_samples ? fmtNum(reqs.samples) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              {{ sampleNote }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_error_rate') }}
            </div>
            <div
              class="kpi-card__value"
              :class="{ 'kpi-card__value--danger': (reqs?.error_rate_percent ?? 0) > 0 }"
              data-test="kpi-error-rate"
            >
              {{ reqs?.has_samples ? fmtPct(reqs.error_rate_percent) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              5xx {{ reqs?.has_samples ? fmtNum(reqs.server_errors) : UNKNOWN }}
              · 4xx {{ reqs?.has_samples ? fmtNum(reqs.client_errors) : UNKNOWN }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              P50
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-p50"
            >
              {{ fmtMs(reqs?.p50_ms) }}
            </div>
            <div class="kpi-card__hint">
              {{ t('admin.monitor_window') }} {{ perf.window }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              P95
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-p95"
            >
              {{ fmtMs(reqs?.p95_ms) }}
            </div>
            <div class="kpi-card__hint">
              {{ t('admin.monitor_samples') }}: {{ reqs?.has_samples ? fmtNum(reqs.samples) : UNKNOWN }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              P99
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-p99"
            >
              {{ fmtMs(reqs?.p99_ms) }}
            </div>
            <div class="kpi-card__hint">
              max {{ fmtMs(reqs?.max_ms) }}
            </div>
          </Card>
        </div>

        <div class="trend-grid">
          <Card :title="t('admin.monitor_request_trend')">
            <Sparkline
              :values="requestTrend"
               :label="t('admin.monitor_request_trend')"
              :empty-label="t('admin.monitor_no_samples')"
              tone="accent"
            />
          </Card>
          <Card :title="t('admin.monitor_latency_trend')">
            <Sparkline
              :values="latencyTrend"
               :label="t('admin.monitor_latency_trend')"
              :empty-label="t('admin.monitor_no_samples')"
              tone="danger"
            />
          </Card>
          <Card :title="t('admin.monitor_error_trend')">
            <Sparkline
              :values="errorTrend"
               :label="t('admin.monitor_error_trend')"
              :empty-label="t('admin.monitor_no_samples')"
              tone="danger"
            />
          </Card>
        </div>

        <div class="detail-grid">
          <Card :title="t('admin.monitor_go_runtime')">
            <Descriptions
              bordered
              size="small"
              :column="1"
            >
              <DescriptionsItem :label="t('admin.monitor_goroutines')">
                {{ fmtNum(perf.go_runtime.goroutines) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_heap_alloc')">
                {{ fmtMB(perf.go_runtime.heap_alloc_mb) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_heap_inuse')">
                {{ fmtMB(perf.go_runtime.heap_inuse_mb) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_gc_runs')">
                {{ fmtNum(perf.go_runtime.gc_runs) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('common.cpu_usage')">
                {{ fmtPct(perf.go_runtime.cpu_percent) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('common.run_time')">
                {{ fmtDuration(perf.go_runtime.uptime_seconds) }}
              </DescriptionsItem>
            </Descriptions>
          </Card>

          <Card :title="t('admin.monitor_engine_process')">
            <template v-if="engine && engine.reachable">
              <Descriptions
                bordered
                size="small"
                :column="1"
              >
                <DescriptionsItem :label="t('admin.monitor_engine_rss')">
                  {{ fmtMB(engine.memory_mb) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('common.cpu_usage')">
                  {{ fmtPct(engine.cpu_percent) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_engine_active_tasks')">
                  {{ fmtNum(engine.active_tasks) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('common.instances')">
                  {{ fmtNum(engine.instances) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('common.version')">
                  {{ engine.version || UNKNOWN }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('common.run_time')">
                  {{ fmtDuration(engine.uptime_seconds) }}
                </DescriptionsItem>
              </Descriptions>
            </template>
            <div
              v-else
              class="monitor-panel__empty"
              data-test="engine-unknown"
            >
              <Tag color="red">
                {{ engine?.configured ? t('admin.monitor_engine_unreachable') : t('admin.monitor_engine_not_configured') }}
              </Tag>
              <span class="monitor-panel__hint">{{ engine?.source || UNKNOWN }}</span>
            </div>
          </Card>

          <Card :title="t('admin.monitor_db_pool')">
            <template v-if="pool">
              <Descriptions
                bordered
                size="small"
                :column="1"
              >
                <DescriptionsItem :label="t('admin.monitor_pool_acquired')">
                  {{ fmtNum(pool.acquired_conns) }} / {{ fmtNum(pool.max_conns) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_pool_idle')">
                  {{ fmtNum(pool.idle_conns) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_pool_total')">
                  {{ fmtNum(pool.total_conns) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_pool_utilization')">
                  {{ fmtPct(pool.utilization_percent) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_pool_empty_acquire')">
                  {{ fmtNum(pool.empty_acquire_count) }}
                </DescriptionsItem>
                <DescriptionsItem :label="t('admin.monitor_pool_acquire_duration')">
                  {{ fmtMs(pool.acquire_duration_ms) }}
                </DescriptionsItem>
              </Descriptions>
            </template>
            <div
              v-else
              class="monitor-panel__empty"
              data-test="db-pool-unknown"
            >
              {{ UNKNOWN }}
            </div>
          </Card>

          <Card :title="t('admin.monitor_dependencies')">
            <Descriptions
              bordered
              size="small"
              :column="1"
            >
              <DescriptionsItem :label="t('admin.redis_latency')">
                {{ fmtMs(perf.redis_latency_ms) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('common.db_latency')">
                {{ fmtMs(perf.db_latency_ms) }}
              </DescriptionsItem>
            </Descriptions>
          </Card>
        </div>
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

.kpi-card__value--danger { color: var(--error); }

.kpi-card__hint {
  margin-top: var(--space-1);
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
  overflow-wrap: anywhere;
}

.trend-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--space-4);
  margin-top: var(--space-4);
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-4);
  margin-top: var(--space-4);
}

.monitor-panel__empty {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  color: var(--text-quaternary);
  padding: var(--space-4) 0;
}

.monitor-panel__hint {
  font-size: var(--fs-xs, 12px);
  overflow-wrap: anywhere;
}
</style>
