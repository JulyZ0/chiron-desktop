<script setup lang="ts">
import { Form, FormItem, InputNumber, Slider, Switch, Row, Col } from 'ant-design-vue'
import SettingsSection from './SettingsSection.vue'
import type {
  ApiKeyConfig,
  ModelRateConfig,
  QuotaConfig,
  SettingsSectionState,
} from './types'

defineProps<{
  apiKeyState: SettingsSectionState
  modelRateState: SettingsSectionState
  quotaState: SettingsSectionState
}>()

const apiKeyModel = defineModel<ApiKeyConfig>('apiKey', { required: true })
const modelRateModel = defineModel<ModelRateConfig>('modelRate', { required: true })
const quotaModel = defineModel<QuotaConfig>('quota', { required: true })

defineEmits<{
  saveApiKey: []
  discardApiKey: []
  saveModelRate: []
  discardModelRate: []
  saveQuota: []
  discardQuota: []
}>()
</script>

<template>
  <div class="settings-panel-stack">
    <SettingsSection
      :title="$t('common.api_key_config')"
      test-id="api-key-config"
      :state="apiKeyState"
      @save="$emit('saveApiKey')"
      @discard="$emit('discardApiKey')"
    >
      <Form
        :model="apiKeyModel"
        layout="vertical"
        :disabled="!apiKeyState.ready || apiKeyState.saving"
      >
        <Row :gutter="[16, 0]">
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('common.circuit_breaker_threshold')">
              <InputNumber
                v-model:value="apiKeyModel.circuitBreakerThreshold"
                :min="1"
                :max="100"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('errors.restore_timeout')">
              <InputNumber
                v-model:value="apiKeyModel.recoveryTimeout"
                :min="10"
                :max="3600"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('common.weight_decay')">
              <Slider
                v-model:value="apiKeyModel.weightDecay"
                :min="0.1"
                :max="1"
                :step="0.1"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('common.auto_recover')">
              <Switch v-model:checked="apiKeyModel.autoRecovery" />
            </FormItem>
          </Col>
        </Row>
      </Form>
    </SettingsSection>

    <SettingsSection
      :title="$t('admin.model_rate_config')"
      test-id="model-rate-config"
      :state="modelRateState"
      @save="$emit('saveModelRate')"
      @discard="$emit('discardModelRate')"
    >
      <Form
        :model="modelRateModel"
        layout="vertical"
        :disabled="!modelRateState.ready || modelRateState.saving"
      >
        <Row :gutter="[16, 0]">
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_rpm')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_rpm"
                :min="0"
                :step="60"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_rps')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_rps"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_user_rpm')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_user_rpm"
                :min="0"
                :step="60"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_user_rps')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_user_rps"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_tpm')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_tpm"
                :min="0"
                :step="10000"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.model_rate_user_tpm')">
              <InputNumber
                v-model:value="modelRateModel.gateway_model_user_tpm"
                :min="0"
                :step="10000"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :sm="12"
            :lg="8"
          >
            <FormItem :label="$t('admin.sandbox_concurrency')">
              <InputNumber
                v-model:value="modelRateModel.sandbox_max_concurrency_per_tenant"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
          </Col>
        </Row>
        <p class="section-note">
          {{ $t('admin.model_rate_hint') }}
        </p>
      </Form>
    </SettingsSection>

    <SettingsSection
      :title="$t('admin.quota_config')"
      test-id="quota-config"
      :state="quotaState"
      @save="$emit('saveQuota')"
      @discard="$emit('discardQuota')"
    >
      <Form
        :model="quotaModel"
        layout="vertical"
        :disabled="!quotaState.ready || quotaState.saving"
      >
        <Row :gutter="[16, 0]">
          <Col
            :xs="24"
            :md="4"
          >
            <FormItem :label="$t('admin.quota_tenant_daily')">
              <InputNumber
                v-model:value="quotaModel.tenant_daily_cost_cents"
                :min="0"
                :step="100"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="4"
          >
            <FormItem :label="$t('admin.quota_user_daily')">
              <InputNumber
                v-model:value="quotaModel.user_daily_cost_cents"
                :min="0"
                :step="100"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="4"
          >
            <FormItem :label="$t('admin.device_write_max_projects')">
              <InputNumber
                v-model:value="quotaModel.device_write_max_projects"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
          </Col>
        </Row>
        <p class="section-note">
          {{ $t('admin.quota_hint') }}
        </p>
      </Form>
    </SettingsSection>
  </div>
</template>

<style scoped>
.settings-panel-stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.section-note {
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
}
</style>
