<script setup lang="ts">
import { Form, FormItem, InputNumber, Row, Col } from 'ant-design-vue'
import SettingsSection from './SettingsSection.vue'
import type { RateLimitConfig, SettingsSectionState } from './types'

defineProps<{
  state: SettingsSectionState
}>()

const model = defineModel<RateLimitConfig>({ required: true })

defineEmits<{
  save: []
  discard: []
}>()
</script>

<template>
  <SettingsSection
    :title="$t('common.rate_limit_config')"
    :description="$t('common.takes_effect_immediately_after_saving_hot_reload')"
    test-id="rate-limit-config"
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
          :md="8"
        >
          <FormItem :label="$t('common.global_per_minute')">
            <InputNumber
              v-model:value="model.global"
              :min="100"
              :max="1000000"
              style="width: 100%"
            />
          </FormItem>
        </Col>
        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.per_tenant_per_minute')">
            <InputNumber
              v-model:value="model.tenant"
              :min="10"
              :max="100000"
              style="width: 100%"
            />
          </FormItem>
        </Col>
        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.per_user_per_minute')">
            <InputNumber
              v-model:value="model.user"
              :min="1"
              :max="10000"
              style="width: 100%"
            />
          </FormItem>
        </Col>
      </Row>
    </Form>
  </SettingsSection>
</template>
