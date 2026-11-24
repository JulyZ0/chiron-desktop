<script setup lang="ts">
import { computed, reactive, ref, watch, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Alert, Button, Spin, Tabs, TabPane, Tag, message } from 'ant-design-vue'
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
  type MailConfigPatch,
} from '@/api/admin'
import { useTabbedQuery } from '@/composables/useTabbedQuery'
import { useUnsavedChangesGuard } from '@/composables/useUnsavedChangesGuard'
import { desktopHost } from '@/stores/device'
import GeneralPanel from '@/components/settings/admin/GeneralPanel.vue'
import ModelQuotaPanel from '@/components/settings/admin/ModelQuotaPanel.vue'
import RuntimePanel from '@/components/settings/admin/RuntimePanel.vue'
import MailPanel from '@/components/settings/admin/MailPanel.vue'
import DataPanel from '@/components/settings/admin/DataPanel.vue'
import type {
  ApiKeyConfig,
  CacheConfig,
  MailConfigDraft,
  ModelRateConfig,
  QuotaConfig,
  RateLimitConfig,
  SettingsTabKey,
} from '@/components/settings/admin/types'

const { t } = useI18n()
const isDesktop = Boolean(desktopHost()?.isDesktop)

const TAB_KEYS: readonly SettingsTabKey[] = ['general', 'models', 'runtime', 'mail', 'data']
const { activeTab, visited, select } = useTabbedQuery({
  routePath: '/admin/settings',
  defaultTab: 'general',
  tabKeys: TAB_KEYS,
})

const tabs = computed<Array<{ key: SettingsTabKey; label: string }>>(() => [
  { key: 'general', label: t('settings.general_settings') },
  { key: 'models', label: t('admin.model_rate_config') },
  { key: 'runtime', label: t('admin.agent_runtime_config') },
  { key: 'mail', label: t('admin.mail_config') },
  { key: 'data', label: t('common.data_storage') },
])

const createRateLimit = (): RateLimitConfig => ({ global: 1000, tenant: 500, user: 100 })
const createCache = (): CacheConfig => ({
  l1Capacity: 2048,
  l2Ttl: 3600,
  semanticThreshold: 0.95,
  prefetchEnabled: true,
})
const createApiKey = (): ApiKeyConfig => ({
  circuitBreakerThreshold: 5,
  recoveryTimeout: 60,
  weightDecay: 0.5,
  autoRecovery: true,
})
const createQuota = (): QuotaConfig => ({
  tenant_daily_cost_cents: 0,
  user_daily_cost_cents: 0,
  device_write_max_projects: 0,
})
const createModelRate = (): ModelRateConfig => ({
  gateway_model_rpm: 0,
  gateway_model_rps: 0,
  gateway_model_user_rpm: 0,
  gateway_model_user_rps: 0,
  gateway_model_tpm: 0,
  gateway_model_user_tpm: 0,
  sandbox_max_concurrency_per_tenant: 0,
})
const createAgent = (): AgentSettingsConfig => ({ ...AGENT_SETTINGS_DEFAULTS })
const createMail = (): MailConfigDraft => ({
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

const rateLimitConfig = ref(createRateLimit())
const cacheConfig = ref(createCache())
const apiKeyConfig = ref(createApiKey())
const quotaConfig = ref(createQuota())
const modelRateConfig = ref(createModelRate())
const agentConfig = ref(createAgent())
const mail = ref(createMail())

const rateLimitBaseline = ref(createRateLimit())
const cacheBaseline = ref(createCache())
const apiKeyBaseline = ref(createApiKey())
const quotaBaseline = ref(createQuota())
const modelRateBaseline = ref(createModelRate())
const agentBaseline = ref(createAgent())
const mailBaseline = ref(createMail())

const mailHasPassword = ref(false)
const mailHasAPIKey = ref(false)
const mailReady = ref(false)
const mailProblem = ref('')
const testingMail = ref(false)
const exporting = ref(false)

type SaveKey = 'rateLimit' | 'cache' | 'apiKey' | 'quota' | 'modelRate' | 'agent' | 'mail'
const saving = reactive<Record<SaveKey, boolean>>({
  rateLimit: false,
  cache: false,
  apiKey: false,
  quota: false,
  modelRate: false,
  agent: false,
  mail: false,
})

const loadedTabs = ref<Set<SettingsTabKey>>(new Set())
const attemptedTabs = ref<Set<SettingsTabKey>>(new Set())
const tabLoading = reactive<Record<SettingsTabKey, boolean>>({
  general: false,
  models: false,
  runtime: false,
  mail: false,
  data: false,
})
const tabError = reactive<Record<SettingsTabKey, string>>({
  general: '',
  models: '',
  runtime: '',
  mail: '',
  data: '',
})

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function equal<T>(left: T, right: T): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function errorText(err: unknown): string {
  return err instanceof Error && err.message ? err.message : t('errors.unknown_error')
}

/** 只覆盖后端契约里已有的键，未返回的键继续使用本地默认值。 */
function mergeConfig<T extends object>(target: T, saved: Record<string, unknown>): void {
  const writable = target as Record<string, unknown>
  for (const key of Object.keys(target)) {
    if (saved[key] !== undefined) writable[key] = saved[key]
  }
}

async function loadGeneral(): Promise<void> {
  mergeConfig(rateLimitConfig.value, await getSettings('rate_limit'))
  rateLimitBaseline.value = clone(rateLimitConfig.value)
}

async function loadModels(): Promise<void> {
  const [apiKey, quota, modelRate] = await Promise.all([
    getSettings('api_key'),
    getSettings('quota'),
    getSettings('python'),
  ])
  mergeConfig(apiKeyConfig.value, apiKey)
  mergeConfig(quotaConfig.value, quota)
  mergeConfig(modelRateConfig.value, modelRate)
  apiKeyBaseline.value = clone(apiKeyConfig.value)
  quotaBaseline.value = clone(quotaConfig.value)
  modelRateBaseline.value = clone(modelRateConfig.value)
}

async function loadRuntime(): Promise<void> {
  agentConfig.value = await getAgentSettings()
  agentBaseline.value = clone(agentConfig.value)
}

async function loadMail(): Promise<void> {
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
  mailBaseline.value = clone(mail.value)
}

async function loadData(): Promise<void> {
  mergeConfig(cacheConfig.value, await getSettings('cache'))
  cacheBaseline.value = clone(cacheConfig.value)
}

/** 每个 Tab 只加载一次；失败后可显式重试，切换离开再回来不会重复请求。 */
async function loadTab(tab: SettingsTabKey, retry = false): Promise<void> {
  if (loadedTabs.value.has(tab) || tabLoading[tab] || (attemptedTabs.value.has(tab) && !retry)) return
  attemptedTabs.value = new Set([...attemptedTabs.value, tab])
  tabLoading[tab] = true
  tabError[tab] = ''
  try {
    switch (tab) {
      case 'general':
        await loadGeneral()
        break
      case 'models':
        await loadModels()
        break
      case 'runtime':
        await loadRuntime()
        break
      case 'mail':
        await loadMail()
        break
      case 'data':
        await loadData()
        break
    }
    loadedTabs.value = new Set([...loadedTabs.value, tab])
  } catch (err) {
    tabError[tab] = errorText(err)
  } finally {
    tabLoading[tab] = false
  }
}

watch(activeTab, (tab) => void loadTab(tab as SettingsTabKey), { immediate: true })

const sectionDirty = computed(() => ({
  rateLimit: !equal(rateLimitConfig.value, rateLimitBaseline.value),
  cache: !equal(cacheConfig.value, cacheBaseline.value),
  apiKey: !equal(apiKeyConfig.value, apiKeyBaseline.value),
  quota: !equal(quotaConfig.value, quotaBaseline.value),
  modelRate: !equal(modelRateConfig.value, modelRateBaseline.value),
  agent: !equal(agentConfig.value, agentBaseline.value),
  mail: !equal(mail.value, mailBaseline.value),
}))

const engineDirty = ref(false)
const hasUnsaved = computed(() => Object.values(sectionDirty.value).some(Boolean) || engineDirty.value)
useUnsavedChangesGuard(hasUnsaved)

function tabDirty(tab: SettingsTabKey): boolean {
  switch (tab) {
    case 'general':
      return sectionDirty.value.rateLimit
    case 'models':
      return sectionDirty.value.apiKey || sectionDirty.value.modelRate || sectionDirty.value.quota
    case 'runtime':
      return sectionDirty.value.agent || engineDirty.value
    case 'mail':
      return sectionDirty.value.mail
    case 'data':
      return sectionDirty.value.cache
  }
}

async function saveConfig<T extends object>(
  key: SaveKey,
  category: string,
  config: T,
  baseline: Ref<T>,
  successKey: string,
): Promise<void> {
  saving[key] = true
  try {
    const snapshot = clone(config)
    await saveSettings(category, { ...snapshot } as Record<string, unknown>)
    baseline.value = snapshot
    message.success(t(successKey))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: errorText(err) }))
  } finally {
    saving[key] = false
  }
}

function saveRateLimit(): Promise<void> {
  return saveConfig('rateLimit', 'rate_limit', rateLimitConfig.value, rateLimitBaseline, 'common.rate_limit_config_saved')
}

function saveCache(): Promise<void> {
  return saveConfig('cache', 'cache', cacheConfig.value, cacheBaseline, 'admin.cache_config_saved')
}

function saveApiKey(): Promise<void> {
  return saveConfig('apiKey', 'api_key', apiKeyConfig.value, apiKeyBaseline, 'common.api_key_config_saved')
}

function saveQuota(): Promise<void> {
  return saveConfig('quota', 'quota', quotaConfig.value, quotaBaseline, 'admin.quota_saved')
}

function saveModelRate(): Promise<void> {
  return saveConfig('modelRate', 'python', modelRateConfig.value, modelRateBaseline, 'admin.model_rate_saved')
}

async function saveAgent(): Promise<void> {
  saving.agent = true
  try {
    const snapshot = clone(agentConfig.value)
    await saveAgentSettings(snapshot)
    agentBaseline.value = snapshot
    message.success(t('admin.agent_runtime_config_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: errorText(err) }))
  } finally {
    saving.agent = false
  }
}

async function saveMail(): Promise<void> {
  saving.mail = true
  try {
    const snapshot = clone(mail.value)
    const patch: MailConfigPatch = {
      provider: snapshot.provider,
      host: snapshot.host,
      port: snapshot.port,
      username: snapshot.username,
      security: snapshot.security,
      from: snapshot.from,
      from_name: snapshot.from_name,
      reply_to: snapshot.reply_to,
      base_url: snapshot.base_url,
      timeout_seconds: snapshot.timeout_seconds,
      // 空串表示保持服务端密钥，绝不能作为默认值随表单提交。
      ...(snapshot.password ? { password: snapshot.password } : {}),
      ...(snapshot.api_key ? { api_key: snapshot.api_key } : {}),
    }
    const cfg = await saveMailConfig(patch)
    mail.value.password = ''
    mail.value.api_key = ''
    mailHasPassword.value = cfg.has_password
    mailHasAPIKey.value = cfg.has_api_key
    mailReady.value = cfg.ready
    mailProblem.value = cfg.problem || ''
    mailBaseline.value = { ...snapshot, password: '', api_key: '' }
    message.success(t('admin.mail_saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: errorText(err) }))
  } finally {
    saving.mail = false
  }
}

async function sendTestMail(): Promise<void> {
  testingMail.value = true
  try {
    const res = await testMailConfig()
    message.success(t('admin.mail_test_sent', { to: res.to }))
  } catch (err) {
    message.error(t('admin.mail_test_failed', { error: errorText(err) }))
  } finally {
    testingMail.value = false
  }
}

async function downloadExport(): Promise<void> {
  exporting.value = true
  try {
    const blob = await exportTenantData()
    const stamp = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
    const url = URL.createObjectURL(blob)
    try {
      const a = document.createElement('a')
      a.href = url
      a.download = `chiron-export-${stamp}.zip`
      a.click()
    } finally {
      URL.revokeObjectURL(url)
    }
    message.success(t('admin.data_export_done'))
  } catch (err) {
    message.error(t('admin.data_export_failed', { error: errorText(err) }))
  } finally {
    exporting.value = false
  }
}

function discard(section: keyof typeof sectionDirty.value): void {
  switch (section) {
    case 'rateLimit':
      rateLimitConfig.value = clone(rateLimitBaseline.value)
      break
    case 'cache':
      cacheConfig.value = clone(cacheBaseline.value)
      break
    case 'apiKey':
      apiKeyConfig.value = clone(apiKeyBaseline.value)
      break
    case 'quota':
      quotaConfig.value = clone(quotaBaseline.value)
      break
    case 'modelRate':
      modelRateConfig.value = clone(modelRateBaseline.value)
      break
    case 'agent':
      agentConfig.value = clone(agentBaseline.value)
      break
    case 'mail':
      mail.value = clone(mailBaseline.value)
      break
  }
}
</script>

<template>
  <div class="settings">
    <header class="settings-header">
      <h1>{{ $t('settings.system_settings') }}</h1>
      <Tag
        v-if="hasUnsaved"
        color="warning"
        data-test="settings-unsaved"
        role="status"
      >
        {{ $t('common.unsaved') }}
      </Tag>
    </header>

    <Tabs
      :active-key="activeTab"
      class="settings-tabs"
      @change="select"
    >
      <TabPane
        v-for="tab in tabs"
        :key="tab.key"
      >
        <template #tab>
          <span class="tab-label">
            {{ tab.label }}
            <span
              v-if="tabDirty(tab.key)"
              class="tab-dirty-marker"
              :data-test="`tab-dirty-${tab.key}`"
              :title="$t('common.unsaved')"
            >
              <span
                class="tab-dirty-dot"
                aria-hidden="true"
              />
              <span class="tab-dirty-text">{{ $t('common.unsaved') }}</span>
            </span>
          </span>
        </template>

        <div
          v-if="visited.has(tab.key)"
          class="tab-content"
        >
          <Alert
            v-if="tabError[tab.key]"
            type="error"
            show-icon
            class="tab-error"
            :data-test="`${tab.key}-load-error`"
            :message="tabError[tab.key]"
          >
            <template #action>
              <Button
                size="small"
                :loading="tabLoading[tab.key]"
                @click="loadTab(tab.key, true)"
              >
                {{ $t('common.retry') }}
              </Button>
            </template>
          </Alert>

          <Spin :spinning="tabLoading[tab.key]">
            <GeneralPanel
              v-if="tab.key === 'general'"
              v-model="rateLimitConfig"
              :state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.rateLimit, saving: saving.rateLimit }"
              @save="saveRateLimit"
              @discard="discard('rateLimit')"
            />
            <ModelQuotaPanel
              v-else-if="tab.key === 'models'"
              v-model:api-key="apiKeyConfig"
              v-model:model-rate="modelRateConfig"
              v-model:quota="quotaConfig"
              :api-key-state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.apiKey, saving: saving.apiKey }"
              :model-rate-state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.modelRate, saving: saving.modelRate }"
              :quota-state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.quota, saving: saving.quota }"
              @save-api-key="saveApiKey"
              @discard-api-key="discard('apiKey')"
              @save-model-rate="saveModelRate"
              @discard-model-rate="discard('modelRate')"
              @save-quota="saveQuota"
              @discard-quota="discard('quota')"
            />
            <RuntimePanel
              v-else-if="tab.key === 'runtime'"
              v-model="agentConfig"
              :is-desktop="isDesktop"
              :state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.agent, saving: saving.agent }"
              @save="saveAgent"
              @discard="discard('agent')"
              @engine-dirty-change="engineDirty = $event"
            />
            <MailPanel
              v-else-if="tab.key === 'mail'"
              v-model="mail"
              :state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.mail, saving: saving.mail }"
              :has-password="mailHasPassword"
              :has-api-key="mailHasAPIKey"
              :mail-ready="mailReady"
              :problem="mailProblem"
              :testing="testingMail"
              @save="saveMail"
              @discard="discard('mail')"
              @test="sendTestMail"
            />
            <DataPanel
              v-else-if="tab.key === 'data'"
              v-model="cacheConfig"
              :cache-state="{ ready: loadedTabs.has(tab.key), dirty: sectionDirty.cache, saving: saving.cache }"
              :exporting="exporting"
              @save-cache="saveCache"
              @discard-cache="discard('cache')"
              @export-data="downloadExport"
            />
          </Spin>
        </div>
      </TabPane>
    </Tabs>
  </div>
</template>

<style scoped>
.settings {
  padding: 0 var(--space-1);
}

.settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-block-end: var(--space-3);
}

.settings-header h1 {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-2xl);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
}

.settings-tabs :deep(.ant-tabs-nav) {
  margin-block-end: var(--space-4);
}

.settings-tabs :deep(.ant-tabs-nav-wrap) {
  overflow-x: auto;
  scrollbar-width: thin;
}

.settings-tabs :deep(.ant-tabs-nav-list) {
  min-width: max-content;
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.tab-dirty-marker {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--warning);
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.tab-dirty-dot {
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--warning);
}

.tab-content {
  min-width: 0;
}

.tab-error {
  margin-block-end: var(--space-4);
}

@media (max-width: 576px) {
  .settings-header {
    align-items: flex-start;
  }

  .settings-tabs :deep(.ant-tabs-tab) {
    padding-inline: var(--space-3);
  }
}
</style>
