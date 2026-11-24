<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Form, FormItem, InputNumber, Select, Switch, Row, Col } from 'ant-design-vue'
import EngineEnvPanel from '../EngineEnvPanel.vue'
import SettingsSection from './SettingsSection.vue'
import type { AgentSettingsModel, SettingsSectionState } from './types'

const { t } = useI18n()

defineProps<{
  state: SettingsSectionState
  isDesktop: boolean
}>()

const emit = defineEmits<{
  save: []
  discard: []
  'engine-dirty-change': [value: boolean]
}>()

const model = defineModel<AgentSettingsModel>({ required: true })

const planApprovalOptions = computed(() => [
  { value: 'never', label: t('admin.plan_approval_never') },
  { value: 'always', label: t('admin.plan_approval_always') },
])

const narrationOptions = computed(() => [
  { value: 'off', label: t('admin.narration_off') },
  { value: 'brief', label: t('admin.narration_brief') },
  { value: 'detailed', label: t('admin.narration_detailed') },
])
</script>

<template>
  <div class="settings-panel-stack">
    <SettingsSection
      :title="$t('admin.agent_runtime_config')"
      test-id="agent-config"
      save-test="agent-save"
      :state="state"
      @save="$emit('save')"
      @discard="$emit('discard')"
    >
      <Form
        :model="model"
        layout="vertical"
        :disabled="!state.ready || state.saving"
      >
        <Row :gutter="[16, 0]">
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.plan_approval')">
              <Select
                v-model:value="model.plan_approval"
                :options="planApprovalOptions"
              />
              <div class="field-note">
                {{ $t('admin.plan_approval_hint') }}
              </div>
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.narration')">
              <Select
                v-model:value="model.narration"
                :options="narrationOptions"
              />
              <div class="field-note">
                {{ $t('admin.narration_hint') }}
              </div>
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.interaction_wait_seconds')">
              <InputNumber
                v-model:value="model.interaction_wait_seconds"
                data-test="agent-interaction-wait"
                :min="1"
                :max="86400"
                :step="30"
                style="width: 100%"
              />
              <div class="field-note">
                {{ $t('admin.interaction_wait_seconds_hint') }}
              </div>
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.no_progress_threshold')">
              <InputNumber
                v-model:value="model.no_progress_threshold"
                data-test="agent-no-progress-threshold"
                :min="0"
                :max="100"
                style="width: 100%"
              />
              <div class="field-note">
                {{ $t('admin.no_progress_threshold_hint') }}
              </div>
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.no_progress_notice')">
              <Switch v-model:checked="model.no_progress_notice" />
              <div class="field-note">
                {{ $t('admin.no_progress_notice_hint') }}
              </div>
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.token_window_ratio')">
              <InputNumber
                v-model:value="model.token_window_ratio"
                data-test="agent-token-window-ratio"
                :min="0.5"
                :max="100"
                :step="0.5"
                style="width: 100%"
              />
              <div class="field-note">
                {{ $t('admin.token_window_ratio_hint') }}
              </div>
            </FormItem>
          </Col>
        </Row>
      </Form>
    </SettingsSection>

    <section
      v-if="isDesktop"
      class="engine-section"
      data-test="engine-env-section"
    >
      <header class="engine-section-header">
        <h2>{{ $t('settings.engine_env_title') }}</h2>
      </header>
      <EngineEnvPanel @dirty-change="(value: boolean) => emit('engine-dirty-change', value)" />
    </section>
  </div>
</template>

<style scoped>
.settings-panel-stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.field-note {
  margin-block-start: var(--space-2);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
}

.engine-section {
  padding: var(--space-5);
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--sig-radius-card, var(--radius-lg));
  box-shadow: var(--shadow-sm);
}

.engine-section-header {
  margin-block-end: var(--space-4);
}

.engine-section-header h2 {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}

@media (max-width: 576px) {
  .engine-section {
    padding: var(--space-4);
  }
}
</style>
