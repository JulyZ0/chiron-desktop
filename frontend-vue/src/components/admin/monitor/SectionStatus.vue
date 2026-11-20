<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Alert, Tag } from 'ant-design-vue'

import type { MonitorSectionMeta } from '@/api/admin'
import { fmtDateTime } from './format'

/**
 * 区块体检条：状态 + 采集口径 + 具体失败组件。
 *
 * 为什么把 errors 摊开显示而不是只写"部分数据不可用"：排障的人需要立刻知道
 * 是 redis 还是 postgres 还是引擎，处置完全不同（重启组件 / 改配置 / 等引擎起来）。
 */
const props = defineProps<{ meta: MonitorSectionMeta }>()

const { t } = useI18n()

const statusText = computed(() => {
  switch (props.meta.status) {
    case 'ok':
      return t('admin.monitor_status_ok')
    case 'partial':
      return t('admin.monitor_status_partial')
    default:
      return t('admin.monitor_status_unavailable')
  }
})

const tagColor = computed(() => {
  switch (props.meta.status) {
    case 'ok':
      return 'green'
    case 'partial':
      return 'orange'
    default:
      return 'red'
  }
})

const errors = computed(() => props.meta.errors ?? [])
const sources = computed(() => props.meta.sources ?? [])
</script>

<template>
  <div
    class="section-status"
    data-test="section-status"
    :data-status="meta.status"
  >
    <div class="section-status__head">
      <Tag :color="tagColor">
        {{ statusText }}
      </Tag>
      <span
        v-if="meta.generated_at"
        class="section-status__time"
      >{{ t('admin.monitor_updated_at') }} {{ fmtDateTime(meta.generated_at) }}</span>
      <span class="section-status__sources">
        <span class="section-status__label">{{ t('admin.monitor_sources') }}</span>
        <span
          v-for="s in sources"
          :key="s"
          class="section-status__source"
        >{{ s }}</span>
      </span>
    </div>
    <Alert
      v-if="errors.length"
      class="section-status__errors"
      type="warning"
      :show-icon="true"
      :message="t('admin.monitor_errors')"
    >
      <template #description>
        <ul class="section-status__error-list">
          <li
            v-for="e in errors"
            :key="`${e.component}:${e.message}`"
          >
            <strong>{{ e.component }}</strong>: {{ e.message }}
          </li>
        </ul>
      </template>
    </Alert>
  </div>
</template>

<style scoped>
.section-status {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.section-status__head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.section-status__sources {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-2);
  color: var(--text-tertiary);
  font-size: var(--fs-xs, 12px);
  line-height: 1.6;
}

.section-status__time {
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
  line-height: 1.6;
}

.section-status__label {
  font-weight: var(--fw-medium);
}

.section-status__source::before {
  content: '·';
  margin-inline-end: var(--space-1);
}

.section-status__errors {
  margin: 0;
}

.section-status__error-list {
  margin: 0;
  padding-inline-start: var(--space-4);
}
</style>
