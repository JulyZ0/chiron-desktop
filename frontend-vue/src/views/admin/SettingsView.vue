<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Card, Row, Col, Form, FormItem, InputNumber, Button, Switch, Slider, Select, message } from 'ant-design-vue'
import {
  saveSettings,
  getSettings,
  getAgentSettings,
  saveAgentSettings,
  AGENT_SETTINGS_DEFAULTS,
  type AgentSettingsConfig,
} from '@/api/admin'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
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

// Agent 运行配置：键名/默认值/白名单都由 @/api/admin 统一维护（与引擎 config.py 同名同义），
// 这里只绑定，不重复一份常量 —— 抄两份默认值就会在某次改默认值后悄悄不一致。
const agentConfig = ref<AgentSettingsConfig>({ ...AGENT_SETTINGS_DEFAULTS })

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
    // Agent 运行配置单独一路：后端无记录时要显示**引擎正在用的默认值**，
    // 不能用 mergeConfig（它只覆盖已有 key，语义不同在此无差别，但归一化要额外做）。
    agentConfig.value = await getAgentSettings()
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

      <!-- Agent 运行配置（system_settings 的 agent 分类）-->
      <Col
        :xs="24"
        :sm="12"
      >
        <Card :title="$t('admin.agent_runtime_config')">
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
            :loading="saving"
            @click="saveAgent"
          >
            {{ $t('common.save') }}
          </Button>
        </Card>
      </Col>
    </Row>
  </div>
</template>

<style scoped>
.settings { padding: 0; }

/* 窄屏：按钮提高触控高度 */
@media (max-width: 576px) {
  .settings :deep(.ant-btn:not(.ant-btn-sm):not(.ant-btn-link)) { min-height: 40px; }
}
</style>