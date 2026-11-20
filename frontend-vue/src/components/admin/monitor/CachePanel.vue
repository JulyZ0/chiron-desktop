<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Card, Descriptions, DescriptionsItem, Progress, Spin, Tag } from 'ant-design-vue'

import Sparkline from '@/components/admin/Sparkline.vue'
import SectionStatus from './SectionStatus.vue'
import { useMonitorOverview } from '@/composables/useMonitorOverview'
import { UNKNOWN, fmtMB, fmtNum, fmtPct, fmtTTL } from './format'

/**
 * 「缓存」面板。
 *
 * 两个来源分开呈现，不合并成一个"总命中率"：
 *   - Redis 键空间命中率（INFO stats 的 keyspace_hits/misses）—— 这是**存储层**；
 *   - 引擎 LLM 多级缓存（/info 的 gateway.cache）—— 这是**推理层**。
 * 两者分母不同，加在一起得到的数没有解释力。任一不可用显示「—」。
 */
const { t } = useI18n()
const { overview, loading, history } = useMonitorOverview()

const cache = computed(() => overview.value?.cache ?? null)
const redis = computed(() => cache.value?.redis ?? null)
const llm = computed(() => cache.value?.llm ?? null)
const redisHitTrend = computed(() => history.value.redisHitRate.slice())
const llmHitTrend = computed(() => history.value.llmHitRate.slice())

const memoryPercent = computed(() => {
  const used = redis.value?.used_memory_mb
  const max = redis.value?.max_memory_mb
  if (typeof used !== 'number' || typeof max !== 'number' || max <= 0) return null
  return Math.min(100, Math.round((used / max) * 100))
})
</script>

<template>
  <div
    class="monitor-panel"
    data-test="cache-panel"
  >
    <Spin :spinning="loading && !overview">
      <div
        v-if="!overview"
        class="monitor-panel__empty"
        data-test="cache-empty"
      >
        {{ loading ? t('common.loading') : UNKNOWN }}
      </div>

      <template v-else-if="cache">
        <SectionStatus :meta="cache" />

        <div class="kpi-grid">
          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_redis_hit') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-redis-hit"
            >
              {{ fmtPct(redis?.hit_rate_percent) }}
            </div>
            <div class="kpi-card__hint">
              hits {{ fmtNum(redis?.keyspace_hits) }} / misses {{ fmtNum(redis?.keyspace_misses) }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_llm_hit') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-llm-hit"
            >
              {{ llm ? fmtPct(llm.hit_rate_percent) : UNKNOWN }}
            </div>
            <div class="kpi-card__hint">
              L1 {{ fmtNum(llm?.l1_hits) }} · L2 {{ fmtNum(llm?.l2_hits) }} · L3 {{ fmtNum(llm?.l3_hits) }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_memory_used') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-redis-memory"
            >
              {{ fmtMB(redis?.used_memory_mb) }}
            </div>
            <div class="kpi-card__hint">
              {{ t('admin.monitor_memory_max') }}: {{ fmtMB(redis?.max_memory_mb) }}
            </div>
          </Card>

          <Card class="kpi-card">
            <div class="kpi-card__label">
              {{ t('admin.monitor_keys') }}
            </div>
            <div
              class="kpi-card__value"
              data-test="kpi-redis-keys"
            >
              {{ fmtNum(redis?.keys) }}
            </div>
            <div class="kpi-card__hint">
              {{ t('admin.monitor_keys_with_ttl') }}: {{ fmtNum(redis?.keys_with_ttl) }}
            </div>
          </Card>
        </div>

        <div class="trend-grid">
          <Card :title="t('admin.monitor_redis_hit_trend')">
            <Sparkline
              :values="redisHitTrend"
               :label="t('admin.monitor_redis_hit_trend')"
              :empty-label="t('admin.monitor_scope_note')"
              tone="success"
            />
            <div class="monitor-panel__hint">
              {{ t('admin.monitor_scope_note') }}
            </div>
          </Card>
          <Card :title="t('admin.monitor_llm_hit_trend')">
            <Sparkline
              :values="llmHitTrend"
               :label="t('admin.monitor_llm_hit_trend')"
              :empty-label="t('admin.monitor_scope_note')"
              tone="success"
            />
            <div class="monitor-panel__hint">
              {{ t('admin.monitor_scope_note') }}
            </div>
          </Card>
        </div>

        <div class="detail-grid">
          <Card :title="t('admin.monitor_redis_cache')">
            <div
              v-if="memoryPercent !== null"
              class="memory-bar"
            >
              <span class="kpi-card__label">{{ t('admin.monitor_memory_usage') }}</span>
              <Progress
                :percent="memoryPercent"
                size="small"
                :show-info="false"
              />
              <span class="kpi-card__hint">{{ fmtPct(memoryPercent, 0) }}</span>
            </div>
            <Descriptions
              bordered
              size="small"
              :column="1"
            >
              <DescriptionsItem :label="t('admin.monitor_avg_ttl')">
                {{ fmtTTL(redis?.avg_ttl_ms) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_evicted')">
                {{ fmtNum(redis?.evicted_keys) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_expired')">
                {{ fmtNum(redis?.expired_keys) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_policy')">
                {{ redis?.maxmemory_policy || UNKNOWN }}
              </DescriptionsItem>
            </Descriptions>
          </Card>

          <Card :title="t('admin.monitor_llm_cache')">
            <div
              v-if="!llm"
              class="monitor-panel__empty"
              data-test="llm-unknown"
            >
              <Tag color="red">
                {{ t('admin.monitor_status_unavailable') }}
              </Tag>
              <span class="monitor-panel__hint">{{ t('admin.monitor_llm_cache_unknown') }}</span>
            </div>
            <Descriptions
              v-else
              bordered
              size="small"
              :column="1"
            >
              <DescriptionsItem :label="t('admin.monitor_l1_hits')">
                {{ fmtNum(llm.l1_hits) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_l2_hits')">
                {{ fmtNum(llm.l2_hits) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_l3_hits')">
                {{ fmtNum(llm.l3_hits) }}
              </DescriptionsItem>
              <DescriptionsItem :label="t('admin.monitor_misses')">
                {{ fmtNum(llm.misses) }}
              </DescriptionsItem>
            </Descriptions>
            <div class="monitor-panel__hint">
              {{ llm?.source || UNKNOWN }}
            </div>
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

.kpi-card__hint,
.monitor-panel__hint {
  margin-top: var(--space-1);
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
  overflow-wrap: anywhere;
}

.trend-grid,
.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
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

.memory-bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
</style>
