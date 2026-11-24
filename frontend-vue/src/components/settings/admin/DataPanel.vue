<script setup lang="ts">
import { Button, Form, FormItem, InputNumber, Slider, Switch, Row, Col } from 'ant-design-vue'
import SettingsSection from './SettingsSection.vue'
import type { CacheConfig, SettingsSectionState } from './types'

defineProps<{
  cacheState: SettingsSectionState
  exporting: boolean
}>()

const model = defineModel<CacheConfig>({ required: true })

defineEmits<{
  saveCache: []
  discardCache: []
  exportData: []
}>()
</script>

<template>
  <div class="settings-panel-stack">
    <SettingsSection
      :title="$t('admin.cache_config')"
      test-id="cache-config"
      :state="cacheState"
      @save="$emit('saveCache')"
      @discard="$emit('discardCache')"
    >
      <Form
        :model="model"
        layout="vertical"
        :disabled="!cacheState.ready || cacheState.saving"
      >
        <Row :gutter="[16, 0]">
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('common.l1_capacity')">
              <InputNumber
                v-model:value="model.l1Capacity"
                :min="100"
                :max="10000"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem label="L2 TTL">
              <InputNumber
                v-model:value="model.l2Ttl"
                :min="60"
                :max="86400"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.semantic_cache_threshold')">
              <Slider
                v-model:value="model.semanticThreshold"
                :min="0.5"
                :max="1"
                :step="0.01"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('common.enable_prefetch')">
              <Switch v-model:checked="model.prefetchEnabled" />
            </FormItem>
          </Col>
        </Row>
      </Form>
    </SettingsSection>

    <section
      class="export-section"
      data-test="data-export"
    >
      <div class="export-copy">
        <h2>{{ $t('admin.data_export') }}</h2>
        <p>{{ $t('admin.data_export_hint') }}</p>
      </div>
      <Button
        type="primary"
        data-test="data-export-btn"
        :loading="exporting"
        @click="$emit('exportData')"
      >
        {{ $t('admin.data_export_btn') }}
      </Button>
    </section>
  </div>
</template>

<style scoped>
.settings-panel-stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.export-section {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-5);
  padding: var(--space-5);
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--sig-radius-card, var(--radius-lg));
  box-shadow: var(--shadow-sm);
}

.export-copy {
  min-width: 0;
}

.export-copy h2 {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
}

.export-copy p {
  max-width: 86ch;
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
}

@media (max-width: 576px) {
  .export-section {
    align-items: stretch;
    flex-direction: column;
    padding: var(--space-4);
  }

  .export-section :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) {
    min-height: 40px;
  }
}
</style>
