<script setup lang="ts">
import { Button, Tag } from 'ant-design-vue'
import type { SettingsSectionState } from './types'

withDefaults(defineProps<{
  title: string
  description?: string
  testId: string
  state: SettingsSectionState
  saveTest?: string
  hideActions?: boolean
}>(), {
  description: '',
  saveTest: '',
  hideActions: false,
})

const emit = defineEmits<{
  save: []
  discard: []
}>()
</script>

<template>
  <section
    class="settings-section"
    :data-test="testId"
  >
    <header class="section-header">
      <div class="section-heading">
        <h2>{{ title }}</h2>
        <p
          v-if="description"
          class="section-description"
        >
          {{ description }}
        </p>
      </div>
      <Tag
        v-if="state.dirty"
        color="warning"
        class="dirty-tag"
        :data-test="`${testId}-dirty`"
        role="status"
      >
        {{ $t('common.unsaved') }}
      </Tag>
    </header>

    <div class="section-body">
      <slot />
    </div>

    <footer
      v-if="!hideActions"
      class="section-actions"
    >
      <div class="section-actions-main">
        <Button
          type="primary"
          :data-test="saveTest || `${testId}-save`"
          :loading="state.saving"
          :disabled="!state.ready || !state.dirty || state.saving"
          @click="emit('save')"
        >
          {{ $t('common.save') }}
        </Button>
        <Button
          v-if="state.dirty"
          :data-test="`${testId}-discard`"
          :disabled="state.saving"
          @click="emit('discard')"
        >
          {{ $t('common.discard') }}
        </Button>
      </div>
      <div class="section-actions-extra">
        <slot name="actions" />
      </div>
    </footer>
  </section>
</template>

<style scoped>
.settings-section {
  padding: var(--space-5);
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--sig-radius-card, var(--radius-lg));
  box-shadow: var(--shadow-sm);
}

.section-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-block-end: var(--space-5);
}

.section-heading {
  min-width: 0;
}

.section-heading h2 {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
}

.section-description {
  max-width: 76ch;
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
}

.dirty-tag {
  flex: none;
  margin-inline-end: 0;
}

.section-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-block-start: var(--space-5);
  padding-block-start: var(--space-4);
  border-block-start: var(--hairline-width) solid var(--border-card);
}

.section-actions-main,
.section-actions-extra {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

@media (max-width: 576px) {
  .settings-section {
    padding: var(--space-4);
  }

  .section-header {
    gap: var(--space-2);
    margin-block-end: var(--space-4);
  }

  .section-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .section-actions :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) {
    min-height: 40px;
  }
}
</style>
