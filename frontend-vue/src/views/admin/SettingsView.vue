<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Card, Row, Col, Form, FormItem, Input, InputNumber, InputPassword, Button, Switch, Slider, Select, Tag, message } from 'ant-design-vue'
import {
  saveSettings,
  getSettings,
  getAgentSettings,
  saveAgentSettings,
  getMailConfig,
  saveMailConfig,
  testMailConfig,
  exportTenantData,
  AGENT_SETTINGS_DEFAULTS,
  type AgentSettingsConfig,
} from '@/api/admin'

import EngineEnvPanel from '../../components/settings/EngineEnvPanel.vue'
import { desktopHost } from '@/stores/device'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
/** 引擎功能开关只对桌面端有意义（改的是本机引擎进程环境）。 */
const isDesktop = Boolean(desktopHost()?.isDesktop)
const saving = ref(false)
const loading = ref(false)

/** 保存失败提示：优先 Error.message（调用方/axios 抛出时已带说明），取不到用兜底文案 */
function saveErrorText(err: unknown): string {
  return err instanceof Error && err.message ? err.message : t('errors.unknown_error')
}

const rateLimitConfig = ref({
  global: 1000,
  tenant: 500,
  user: 100,
})

const cacheConfig = ref({
  l1Capacity: 2048,
  l2Ttl: 3600,
  semanticThreshold: 0.95,
  prefetchEnabled: true,
})

const apiKeyConfig = ref({
  circuitBreakerThreshold: 5,
  recoveryTimeout: 60,
  weightDecay: 0.5,
  autoRecovery: true,
})

// 日费用熔断（分）：0 = 不限。按 usage_events 当日 UTC 汇总，submit 与 OpenAI 兼容入口共用。
const quotaConfig = ref({
  tenant_daily_cost_cents: 0,
  user_daily_cost_cents: 0,
  device_write_max_projects: 0,
})

// 模型调用限流（租户级，按每次 LLM 调用计数）：0/0 = 关闭。
// 与 HTTP 请求限流分开，避免一次回合的多次模型调用吃光普通请求额度。
const modelRateConfig = ref({
  gateway_model_rpm: 0,
  gateway_model_rps: 0,
  gateway_model_user_rpm: 0,
  gateway_model_user_rps: 0,
  gateway_model_tpm: 0,
  gateway_model_user_tpm: 0,
  sandbox_max_concurrency_per_tenant: 0,
})

// Agent 运行配置：键名/默认值/白名单都由 @/api/admin 统一维护（与引擎 config.py 同名同义），
// 这里只绑定，不重复一份常量 —— 抄两份默认值就会在某次改默认值后悄悄不一致。
const agentConfig = ref<AgentSettingsConfig>({ ...AGENT_SETTINGS_DEFAULTS })

// 邮件通道：服务端只回密钥"是否已设置"，密码/API Key 输入框留空 = 保持原值。
const mail = ref({
  provider: 'smtp',
  host: '',
  port: 587,
  username: '',
  password: '',
  security: 'starttls',
  from: '',
  from_name: 'Chiron',
  reply_to: '',
  base_url: '',
  api_key: '',
  timeout_seconds: 30,
})
const mailHasPassword = ref(false)
const mailHasAPIKey = ref(false)
const mailReady = ref(false)
const mailProblem = ref('')
const testingMail = ref(false)
const exporting = ref(false)

const mailProviderOptions = computed(() => [
  { value: 'smtp', label: t('admin.mail_provider_smtp') },
  { value: 'qingchen', label: t('admin.mail_provider_qingchen') },
])
const mailSecurityOptions = computed(() => [
  { value: 'starttls', label: t('admin.mail_security_starttls') },
  { value: 'ssl', label: t('admin.mail_security_ssl') },
  { value: 'none', label: t('admin.mail_security_none') },
])

const planApprovalOptions = computed(() => [
  { value: 'never', label: t('admin.plan_approval_never') },
  { value: 'always', label: t('admin.plan_approval_always') },
])

const narrationOptions = computed(() => [
  { value: 'off', label: t('admin.narration_off') },
  { value: 'brief', label: t('admin.narration_brief') },
  { value: 'detailed', label: t('admin.narration_detailed') },
])

async function saveRateLimit() {
  saving.value = true
  try {
    await saveSettings('rate_limit', rateLimitConfig.value)
    message.success(t('common.rate_limit_config_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function saveCache() {
  saving.value = true
  try {
    await saveSettings('cache', cacheConfig.value)
    message.success(t('admin.cache_config_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function saveApiKey() {
  saving.value = true
  try {
    await saveSettings('api_key', apiKeyConfig.value)
    message.success(t('common.api_key_config_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function saveQuota() {
  saving.value = true
  try {
    await saveSettings('quota', quotaConfig.value)
    message.success(t('admin.quota_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function saveModelRate() {
  saving.value = true
  try {
    await saveSettings('python', modelRateConfig.value)
    message.success(t('admin.model_rate_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function saveAgent() {
  saving.value = true
  try {
    await saveAgentSettings(agentConfig.value)
    message.success(t('admin.agent_runtime_config_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function loadMail() {
  try {
    const cfg = await getMailConfig()
    mail.value = {
      provider: cfg.provider === 'qingchen' ? 'qingchen' : 'smtp',
      host: cfg.host || '',
      port: cfg.port || 587,
      username: cfg.username || '',
      password: '',
      security: cfg.security || 'starttls',
      from: cfg.from || '',
      from_name: cfg.from_name || 'Chiron',
      reply_to: cfg.reply_to || '',
      base_url: cfg.base_url || '',
      api_key: '',
      timeout_seconds: cfg.timeout_seconds || 30,
    }
    mailHasPassword.value = cfg.has_password
    mailHasAPIKey.value = cfg.has_api_key
    mailReady.value = cfg.ready
    mailProblem.value = cfg.problem || ''
  } catch {
    // 读不到时保持空表单：不要用假默认覆盖用户已保存的值。
  }
}

async function saveMail() {
  saving.value = true
  try {
    const cfg = await saveMailConfig({
      provider: mail.value.provider,
      host: mail.value.host,
      port: mail.value.port,
      username: mail.value.username,
      security: mail.value.security,
      from: mail.value.from,
      from_name: mail.value.from_name,
      reply_to: mail.value.reply_to,
      base_url: mail.value.base_url,
      timeout_seconds: mail.value.timeout_seconds,
      // 只提交实际输入的密钥；空串会清空服务端密钥，不能随表单默认值一起发。
      ...(mail.value.password ? { password: mail.value.password } : {}),
      ...(mail.value.api_key ? { api_key: mail.value.api_key } : {}),
    })
    mail.value.password = ''
    mail.value.api_key = ''
    mailHasPassword.value = cfg.has_password
    mailHasAPIKey.value = cfg.has_api_key
    mailReady.value = cfg.ready
    mailProblem.value = cfg.problem || ''
    message.success(t('admin.mail_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

async function sendTestMail() {
  testingMail.value = true
  try {
    const res = await testMailConfig()
    message.success(t('admin.mail_test_sent', { to: res.to }))
  } catch (err) {
    message.error(t('admin.mail_test_failed', { error: saveErrorText(err) }))
  } finally {
    testingMail.value = false
  }
}

async function downloadExport() {
  exporting.value = true
  try {
    const blob = await exportTenantData()
    const stamp = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `chiron-export-${stamp}.zip`
    a.click()
    URL.revokeObjectURL(url)
    message.success(t('admin.data_export_done'))
  } catch (err) {
    message.error(t('admin.data_export_failed', { error: saveErrorText(err) }))
  } finally {
    exporting.value = false
  }
}

// 修复：加载已持久化的真实配置（不再以写死的示例默认值覆盖线上配置）。
// 按返回的 key 覆盖默认值；后端无记录时保留默认（本地为空态）。
function mergeConfig(target: { value: Record<string, unknown> }, saved: Record<string, unknown>) {
  for (const k of Object.keys(target.value)) {
    if (saved[k] !== undefined) target.value[k] = saved[k]
  }
}

onMounted(async () => {
  loading.value = true
  try {
    mergeConfig(rateLimitConfig, await getSettings('rate_limit'))
    mergeConfig(cacheConfig, await getSettings('cache'))
    mergeConfig(apiKeyConfig, await getSettings('api_key'))
    mergeConfig(quotaConfig, await getSettings('quota'))
    mergeConfig(modelRateConfig, await getSettings('python'))
    // Agent 运行配置单独一路：后端无记录时要显示**引擎正在用的默认值**，
    // 不能用 mergeConfig（它只覆盖已有 key，语义不同在此无差别，但归一化要额外做）。
    agentConfig.value = await getAgentSettings()
    // 邮件配置独立：失败不能把上面已加载的配置清空。
    await loadMail()
  } catch {
    // 拉取失败保留默认值，不阻断页面
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="settings">
    <Row :gutter="16">
      <!-- 限流配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('common.rate_limit_config')">
          <Form
            :model="rateLimitConfig"
            layout="vertical"
          >
            <FormItem :label="$t('common.global_per_minute')">
              <InputNumber
                v-model:value="rateLimitConfig.global"
                :min="100"
                :max="1000000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.per_tenant_per_minute')">
              <InputNumber
                v-model:value="rateLimitConfig.tenant"
                :min="10"
                :max="100000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.per_user_per_minute')">
              <InputNumber
                v-model:value="rateLimitConfig.user"
                :min="1"
                :max="10000"
                style="width: 100%"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveRateLimit"
          >
            {{ $t('common.save') }}
          </Button>
          <div class="config-note">
            {{ $t('common.takes_effect_immediately_after_saving_hot_reload') }}
          </div>
        </Card>
      </Col>

      <!-- 缓存配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('admin.cache_config')">
          <Form
            :model="cacheConfig"
            layout="vertical"
          >
            <FormItem :label="$t('common.l1_capacity')">
              <InputNumber
                v-model:value="cacheConfig.l1Capacity"
                :min="100"
                :max="10000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem label="L2 TTL">
              <InputNumber
                v-model:value="cacheConfig.l2Ttl"
                :min="60"
                :max="86400"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.semantic_cache_threshold')">
              <Slider
                v-model:value="cacheConfig.semanticThreshold"
                :min="0.5"
                :max="1"
                :step="0.01"
              />
            </FormItem>
            <FormItem :label="$t('common.enable_prefetch')">
              <Switch v-model:checked="cacheConfig.prefetchEnabled" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveCache"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>

      <!-- API Key 配置 -->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('common.api_key_config')">
          <Form
            :model="apiKeyConfig"
            layout="vertical"
          >
            <FormItem :label="$t('common.circuit_breaker_threshold')">
              <InputNumber
                v-model:value="apiKeyConfig.circuitBreakerThreshold"
                :min="1"
                :max="100"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('errors.restore_timeout')">
              <InputNumber
                v-model:value="apiKeyConfig.recoveryTimeout"
                :min="10"
                :max="3600"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('common.weight_decay')">
              <Slider
                v-model:value="apiKeyConfig.weightDecay"
                :min="0.1"
                :max="1"
                :step="0.1"
              />
            </FormItem>
            <FormItem :label="$t('common.auto_recover')">
              <Switch v-model:checked="apiKeyConfig.autoRecovery" />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveApiKey"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>

      <!-- 模型调用限流（system_settings 的 python 分类，引擎启动/广播消费）-->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card
          :title="$t('admin.model_rate_config')"
          data-test="model-rate-config"
        >
          <Form
            :model="modelRateConfig"
            layout="vertical"
          >
            <FormItem :label="$t('admin.model_rate_rpm')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_rpm"
                :min="0"
                :step="60"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.model_rate_rps')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_rps"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.model_rate_user_rpm')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_user_rpm"
                :min="0"
                :step="60"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.model_rate_user_rps')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_user_rps"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.model_rate_tpm')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_tpm"
                :min="0"
                :step="10000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.model_rate_user_tpm')">
              <InputNumber
                v-model:value="modelRateConfig.gateway_model_user_tpm"
                :min="0"
                :step="10000"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.sandbox_concurrency')">
              <InputNumber
                v-model:value="modelRateConfig.sandbox_max_concurrency_per_tenant"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
            <div class="config-note">
              {{ $t('admin.model_rate_hint') }}
            </div>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveModelRate"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>

      <!-- 日费用熔断（system_settings 的 quota 分类）-->      <Col
        :xs="24"
        :sm="12"
      >
        <Card
          :title="$t('admin.quota_config')"
          data-test="quota-config"
        >
          <Form
            :model="quotaConfig"
            layout="vertical"
          >
            <FormItem :label="$t('admin.quota_tenant_daily')">
              <InputNumber
                v-model:value="quotaConfig.tenant_daily_cost_cents"
                :min="0"
                :step="100"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.quota_user_daily')">
              <InputNumber
                v-model:value="quotaConfig.user_daily_cost_cents"
                :min="0"
                :step="100"
                style="width: 100%"
              />
            </FormItem>
            <FormItem :label="$t('admin.device_write_max_projects')">
              <InputNumber
                v-model:value="quotaConfig.device_write_max_projects"
                :min="0"
                style="width: 100%"
              />
            </FormItem>
            <div class="config-note">
              {{ $t('admin.quota_hint') }}
            </div>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveQuota"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>

      <!-- 邮件通道（邀请邮件 / 测试发信）-->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card
          :title="$t('admin.mail_config')"
          data-test="mail-config"
        >
          <div class="mail-status">
            <Tag :color="mailReady ? 'green' : 'orange'">
              {{ mailReady ? $t('admin.mail_ready') : $t('admin.mail_not_ready') }}
            </Tag>
            <span
              v-if="mailProblem"
              class="config-note"
            >{{ mailProblem }}</span>
          </div>
          <Form
            :model="mail"
            layout="vertical"
          >
            <FormItem :label="$t('admin.mail_provider')">
              <Select
                v-model:value="mail.provider"
                :options="mailProviderOptions"
              />
            </FormItem>

            <template v-if="mail.provider === 'smtp'">
              <FormItem :label="$t('admin.mail_host')">
                <Input
                  v-model:value="mail.host"
                  placeholder="smtp.example.com"
                />
              </FormItem>
              <FormItem :label="$t('admin.mail_port')">
                <InputNumber
                  v-model:value="mail.port"
                  :min="1"
                  :max="65535"
                  style="width: 100%"
                />
              </FormItem>
              <FormItem :label="$t('admin.mail_security')">
                <Select
                  v-model:value="mail.security"
                  :options="mailSecurityOptions"
                />
              </FormItem>
              <FormItem :label="$t('admin.mail_username')">
                <Input v-model:value="mail.username" />
              </FormItem>
              <FormItem :label="$t('admin.mail_password')">
                <InputPassword
                  v-model:value="mail.password"
                  :placeholder="mailHasPassword ? $t('admin.mail_secret_keep') : ''"
                />
              </FormItem>
            </template>

            <template v-else>
              <FormItem :label="$t('admin.mail_base_url')">
                <Input
                  v-model:value="mail.base_url"
                  placeholder="https://mail.example.com"
                />
              </FormItem>
              <FormItem :label="$t('admin.mail_api_key')">
                <InputPassword
                  v-model:value="mail.api_key"
                  :placeholder="mailHasAPIKey ? $t('admin.mail_secret_keep') : ''"
                />
              </FormItem>
            </template>

            <FormItem :label="$t('admin.mail_from')">
              <Input
                v-model:value="mail.from"
                placeholder="noreply@example.com"
              />
            </FormItem>
            <FormItem :label="$t('admin.mail_from_name')">
              <Input v-model:value="mail.from_name" />
            </FormItem>
            <FormItem :label="$t('admin.mail_reply_to')">
              <Input v-model:value="mail.reply_to" />
            </FormItem>
            <FormItem :label="$t('admin.mail_timeout')">
              <InputNumber
                v-model:value="mail.timeout_seconds"
                :min="1"
                :max="120"
                style="width: 100%"
              />
            </FormItem>
          </Form>
          <Button
            type="primary"
            :loading="saving"
            @click="saveMail"
          >
            {{ $t('common.save') }}
          </Button>
          <Button
            class="mail-test-btn"
            :loading="testingMail"
            :disabled="!mailReady"
            @click="sendTestMail"
          >
            {{ $t('admin.mail_send_test') }}
          </Button>
        </Card>
      </Col>
      <Col
        :xs="24"
        :sm="12"
      >
        <Card
          :title="$t('admin.agent_runtime_config')"
          data-test="agent-config"
        >
          <Form
            :model="agentConfig"
            layout="vertical"
          >
            <FormItem :label="$t('admin.plan_approval')">
              <Select
                v-model:value="agentConfig.plan_approval"
                :options="planApprovalOptions"
              />
              <div class="config-note">
                {{ $t('admin.plan_approval_hint') }}
              </div>
            </FormItem>
            <FormItem :label="$t('admin.interaction_wait_seconds')">
              <InputNumber
                v-model:value="agentConfig.interaction_wait_seconds"
                data-test="agent-interaction-wait"
                :min="1"
                :max="86400"
                :step="30"
                style="width: 100%"
              />
              <div class="config-note">
                {{ $t('admin.interaction_wait_seconds_hint') }}
              </div>
            </FormItem>
            <FormItem :label="$t('admin.narration')">
              <Select
                v-model:value="agentConfig.narration"
                :options="narrationOptions"
              />
              <div class="config-note">
                {{ $t('admin.narration_hint') }}
              </div>
            </FormItem>
            <FormItem :label="$t('admin.no_progress_notice')">
              <Switch v-model:checked="agentConfig.no_progress_notice" />
              <div class="config-note">
                {{ $t('admin.no_progress_notice_hint') }}
              </div>
            </FormItem>
            <FormItem :label="$t('admin.no_progress_threshold')">
              <InputNumber
                v-model:value="agentConfig.no_progress_threshold"
                data-test="agent-no-progress-threshold"
                :min="0"
                :max="100"
                style="width: 100%"
              />
              <div class="config-note">
                {{ $t('admin.no_progress_threshold_hint') }}
              </div>
            </FormItem>
            <FormItem :label="$t('admin.token_window_ratio')">
              <InputNumber
                v-model:value="agentConfig.token_window_ratio"
                data-test="agent-token-window-ratio"
                :min="0.5"
                :max="100"
                :step="0.5"
                style="width: 100%"
              />
              <div class="config-note">
                {{ $t('admin.token_window_ratio_hint') }}
              </div>
            </FormItem>
          </Form>
          <Button
            type="primary"
            data-test="agent-save"
            :loading="saving"
            @click="saveAgent"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>
    </Row>

    <!-- 数据导出（R10）：ZIP + 每表 NDJSON；不含秘密与二进制原文。 -->
    <Card
      :title="$t('admin.data_export')"
      data-test="data-export"
      style="margin-top: 16px"
    >
      <p class="config-note">
        {{ $t('admin.data_export_hint') }}
      </p>
      <Button
        type="primary"
        :loading="exporting"
        @click="downloadExport"
      >
        {{ $t('admin.data_export_btn') }}
      </Button>
    </Card>

    <!-- 引擎功能开关：原独立页签/独立后台页并入系统设置（仅桌面端）。 -->
    <Card
      v-if="isDesktop"
      style="margin-top: 16px"
    >
      <EngineEnvPanel />
    </Card>
  </div>
</template>

<style scoped>
.settings { padding: 0; }

.mail-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.mail-test-btn { margin-inline-start: var(--space-2); }

/* 窄屏：按钮提高触控高度 */
@media (max-width: 576px) {
  .settings :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) { min-height: 40px; }
}
</style>