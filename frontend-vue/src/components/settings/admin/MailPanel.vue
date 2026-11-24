<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Button, Form, FormItem, Input, InputNumber, InputPassword, Select, Tag, Row, Col } from 'ant-design-vue'
import SettingsSection from './SettingsSection.vue'
import type { MailConfigDraft, SettingsSectionState } from './types'

const { t } = useI18n()

defineProps<{
  state: SettingsSectionState
  hasPassword: boolean
  hasApiKey: boolean
  mailReady: boolean
  problem: string
  testing: boolean
}>()

const model = defineModel<MailConfigDraft>({ required: true })

defineEmits<{
  save: []
  discard: []
  test: []
}>()

const providerOptions = computed(() => [
  { value: 'smtp', label: t('admin.mail_provider_smtp') },
  { value: 'qingchen', label: t('admin.mail_provider_qingchen') },
])

const securityOptions = computed(() => [
  { value: 'starttls', label: t('admin.mail_security_starttls') },
  { value: 'ssl', label: t('admin.mail_security_ssl') },
  { value: 'none', label: t('admin.mail_security_none') },
])
</script>

<template>
  <SettingsSection
    :title="$t('admin.mail_config')"
    test-id="mail-config"
    :state="state"
    @save="$emit('save')"
    @discard="$emit('discard')"
  >
    <template #actions>
      <Button
        data-test="mail-test"
        :loading="testing"
        :disabled="!state.ready || !mailReady || state.saving"
        @click="$emit('test')"
      >
        {{ $t('admin.mail_send_test') }}
      </Button>
    </template>

    <div class="mail-status">
      <Tag :color="mailReady ? 'green' : 'orange'">
        {{ mailReady ? $t('admin.mail_ready') : $t('admin.mail_not_ready') }}
      </Tag>
      <span
        v-if="problem"
        class="mail-problem"
      >
        {{ problem }}
      </span>
    </div>

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
          <FormItem :label="$t('admin.mail_provider')">
            <Select
              v-model:value="model.provider"
              :options="providerOptions"
            />
          </FormItem>
        </Col>

        <template v-if="model.provider === 'smtp'">
          <Col
            :xs="24"
            :md="8"
          >
            <FormItem :label="$t('admin.mail_host')">
              <Input
                v-model:value="model.host"
                autocomplete="off"
                placeholder="smtp.example.com"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="4"
          >
            <FormItem :label="$t('admin.mail_port')">
              <InputNumber
                v-model:value="model.port"
                :min="1"
                :max="65535"
                style="width: 100%"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.mail_security')">
              <Select
                v-model:value="model.security"
                :options="securityOptions"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.mail_username')">
              <Input
                v-model:value="model.username"
                autocomplete="off"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.mail_password')">
              <InputPassword
                v-model:value="model.password"
                data-test="mail-password"
                autocomplete="new-password"
                :placeholder="hasPassword ? $t('admin.mail_secret_keep') : ''"
              />
            </FormItem>
          </Col>
        </template>

        <template v-else>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.mail_base_url')">
              <Input
                v-model:value="model.base_url"
                autocomplete="off"
                placeholder="https://mail.example.com"
              />
            </FormItem>
          </Col>
          <Col
            :xs="24"
            :md="12"
          >
            <FormItem :label="$t('admin.mail_api_key')">
              <InputPassword
                v-model:value="model.api_key"
                data-test="mail-api-key"
                autocomplete="new-password"
                :placeholder="hasApiKey ? $t('admin.mail_secret_keep') : ''"
              />
            </FormItem>
          </Col>
        </template>

        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.mail_from')">
            <Input
              v-model:value="model.from"
              data-test="mail-from"
              autocomplete="off"
              placeholder="noreply@example.com"
            />
          </FormItem>
        </Col>
        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.mail_from_name')">
            <Input v-model:value="model.from_name" />
          </FormItem>
        </Col>
        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.mail_reply_to')">
            <Input
              v-model:value="model.reply_to"
              autocomplete="off"
            />
          </FormItem>
        </Col>
        <Col
          :xs="24"
          :md="8"
        >
          <FormItem :label="$t('admin.mail_timeout')">
            <InputNumber
              v-model:value="model.timeout_seconds"
              :min="1"
              :max="120"
              style="width: 100%"
            />
          </FormItem>
        </Col>
      </Row>
    </Form>
  </SettingsSection>
</template>

<style scoped>
.mail-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-block-end: var(--space-4);
}

.mail-problem {
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: var(--lh-normal);
}
</style>
