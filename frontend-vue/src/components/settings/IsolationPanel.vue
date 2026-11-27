<script setup lang="ts">
/**
 * 执行隔离设置页。
 *
 * 产品只支持四个档位：无隔离 / 作业对象 / 低完整性 / 容器（Kiri）。页面按
 * “当前状态 → 选择档位 → 容器运行时 → 维护”四段组织，避免把说明、操作和
 * 历史遗留的撤销入口混在一起。
 *
 * 三条展示原则：
 * 1. **请求值 vs 实际值**必须同时可见；两者不同就是降级，不能只显示一个绿标。
 * 2. 不可选的原因来自后端 `unavailable_reasons`，前端不自己推断 —— 档位能不能
 *    落地是本机探测结果，不是安全策略的复述。
 * 3. 写操作（切档 / 恢复 / 清除痕迹）都带本地会话自愈；401 不是“没权限”，
 *    而是本机会话还没建好。
 */
import { computed, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  CheckOutlined,
  ContainerOutlined,
  ReloadOutlined,
  SafetyOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  clearIsolationLevel,
  getIsolationStatus,
  prepareContainerRuntime,
  probeIsolation,
  revokeIsolationArtifacts,
  setIsolationLevel,
  type IsolationContainerRuntime,
  type IsolationLevel,
  type IsolationStatus,
} from '../../api'
import { useAuthStore } from '../../stores/auth'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const authStore = useAuthStore()

const loading = ref(false)
const saving = ref(false)
const revoking = ref(false)
const reprobing = ref(false)
const status = ref<IsolationStatus | null>(null)
const loadError = ref('')
const pendingLevel = ref<IsolationLevel | null>(null)
const preparingContainer = ref(false)
const containerHint = ref('')

/** 与后端 `Level._ORDER` 严格一致：只保留产品支持的四个档位。 */
const LEVELS: IsolationLevel[] = ['none', 'job', 'low_il', 'container']

const currentEffective = computed(() => status.value?.effective ?? null)
const containerRuntime = computed<IsolationContainerRuntime | null>(
  () => status.value?.container ?? null,
)

function isSelectable(level: IsolationLevel): boolean {
  return status.value?.selectable?.includes(level) ?? false
}

function needsAcknowledge(level: IsolationLevel): boolean {
  return status.value?.acknowledge_required?.includes(level) ?? false
}

function levelLabel(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}`)
}

function levelDescription(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}_desc`)
}

/** 不可选原因：只解释“本机缺什么机制”，不重复安全底线的策略说明。 */
function disabledReason(level: IsolationLevel): string {
  if (!status.value || isSelectable(level)) return ''
  const reason = (status.value.unavailable_reasons?.[level] || '').trim()
  return reason || t('settings.isolation_unavailable_unknown')
}

/** 401 自愈：本地会话的建立是刻意不 await 的，设置页可能比它先发请求。 */
async function withLocalAuth<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn()
  } catch (err) {
    const is401 = (err as { response?: { status?: number } })?.response?.status === 401
    if (!is401) throw err
    const ready = await authStore.ensureLocalSession(true)
    if (!ready) throw err
    return await fn()
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    status.value = await getIsolationStatus()
  } catch (err) {
    const is401 = (err as { response?: { status?: number } })?.response?.status === 401
    if (is401) {
      const ready = await authStore.ensureLocalSession(true)
      if (ready) {
        try {
          status.value = await getIsolationStatus()
          return
        } catch (retryErr) {
          loadError.value = serverErrorMessage(retryErr, t('settings.isolation_auth_pending'))
          return
        }
      }
      loadError.value = t('settings.isolation_auth_pending')
      return
    }
    loadError.value = serverErrorMessage(err, t('settings.isolation_load_failed'))
  } finally {
    loading.value = false
  }
}

function acknowledgeBelowFloor(level: IsolationLevel): Promise<boolean> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: t('settings.isolation_confirm_title'),
      content: t('settings.isolation_confirm_body', { level: levelLabel(level) }),
      okText: t('settings.isolation_confirm_ok'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

async function applyLevel(level: IsolationLevel) {
  if (!isSelectable(level) || saving.value) return
  if (level === status.value?.effective) return
  if (needsAcknowledge(level) && !(await acknowledgeBelowFloor(level))) return

  saving.value = true
  pendingLevel.value = level
  try {
    const next = await withLocalAuth(() =>
      setIsolationLevel(level, t('settings.isolation_change_reason'), needsAcknowledge(level)),
    )
    status.value = next
    message.success(t('settings.isolation_changed', { level: levelLabel(next.effective) }))
    if (next.requested !== next.effective) {
      message.warning(
        t('settings.isolation_raised_to_floor', {
          requested: levelLabel(next.requested),
          effective: levelLabel(next.effective),
        }),
      )
    }
    if (next.running_processes_unaffected) {
      message.info(t('settings.isolation_running_unaffected'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_change_failed')))
  } finally {
    saving.value = false
    pendingLevel.value = null
  }
}

async function resetToConfig() {
  if (saving.value) return
  saving.value = true
  try {
    const next = await withLocalAuth(() => clearIsolationLevel())
    status.value = next
    message.success(
      next.cleared
        ? t('settings.isolation_reset_done', { level: levelLabel(next.effective) })
        : t('settings.isolation_reset_noop'),
    )
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_reset_failed')))
  } finally {
    saving.value = false
  }
}

async function retryRequested() {
  const requested = status.value?.requested
  if (!requested) return
  if (!isSelectable(requested)) {
    message.warning(t('settings.isolation_retry_unavailable'))
    return
  }
  await applyLevel(requested)
}

async function reprobe() {
  if (reprobing.value) return
  reprobing.value = true
  try {
    status.value = await withLocalAuth(() => probeIsolation())
    message.success(t('settings.isolation_reprobe_done'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_reprobe_failed')))
  } finally {
    reprobing.value = false
  }
}

/** 只下载 + 校验运行时包；安装/启用仍由管理员执行（后端给命令）。 */
async function prepareContainerRuntimePackage() {
  if (preparingContainer.value) return
  preparingContainer.value = true
  containerHint.value = ''
  try {
    const next = await prepareContainerRuntime()
    status.value = next
    containerHint.value = next.prepare?.error || next.container?.install_hint || ''
    if (next.prepare?.ok) {
      message.success(t('settings.isolation_container_already_ready'))
    } else if (next.prepare?.downloaded) {
      message.info(t('settings.isolation_container_downloaded'))
    } else {
      message.error(next.prepare?.error || t('settings.isolation_container_download_failed'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_container_download_failed')))
  } finally {
    preparingContainer.value = false
  }
}

async function revokeArtifacts() {
  if (revoking.value) return
  revoking.value = true
  try {
    const result = await revokeIsolationArtifacts()
    const labelsOk = result.integrity_labels?.every((item) => item.ok) ?? true
    message.success(
      labelsOk
        ? t('settings.isolation_revoke_done')
        : t('settings.isolation_revoke_partial', { detail: result.appcontainer_profile }),
    )
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_revoke_failed')))
  } finally {
    revoking.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="iso-page">
    <header class="iso-head">
      <SafetyOutlined class="iso-head-icon" />
      <div>
        <h2 class="iso-title">
          {{ $t('settings.isolation_title') }}
        </h2>
        <p class="iso-intro">
          {{ $t('settings.isolation_intro') }}
        </p>
      </div>
    </header>

    <a-alert
      v-if="loadError"
      type="warning"
      show-icon
      class="iso-alert"
      :message="loadError"
    >
      <template #action>
        <a-button
          size="small"
          :loading="loading"
          @click="load"
        >
          {{ $t('common.retry') }}
        </a-button>
      </template>
    </a-alert>

    <a-spin v-else-if="loading && !status" />

    <template v-else-if="status">
      <!-- 当前状态 -->
      <section class="iso-card iso-current">
        <div class="iso-current-head">
          <span class="iso-eyebrow">{{ $t('settings.isolation_current') }}</span>
          <a-tag :color="status.below_floor ? 'red' : 'green'">
            {{ levelLabel(status.effective) }}
          </a-tag>
          <span
            v-if="status.runtime_override"
            class="iso-muted"
          >
            {{ $t('settings.isolation_temp_applied') }}
          </span>
          <span class="iso-spacer" />
          <a-button
            type="link"
            size="small"
            :loading="reprobing"
            :disabled="reprobing || saving"
            @click="reprobe"
          >
            {{ $t('settings.isolation_reprobe') }}
          </a-button>
        </div>

        <a-alert
          v-if="status.requested !== status.effective"
          :type="status.below_floor ? 'error' : 'warning'"
          show-icon
          class="iso-alert"
          :message="$t('settings.isolation_degraded_title')"
          :description="
            [
              $t('settings.isolation_degraded_body', {
                requested: levelLabel(status.requested),
                effective: levelLabel(status.effective),
              }),
              ...(status.reasons || []),
            ].join(' ')
          "
        >
          <template #action>
            <a-button
              size="small"
              :loading="saving"
              :disabled="saving"
              @click="retryRequested"
            >
              {{ $t('common.retry') }}
            </a-button>
          </template>
        </a-alert>

        <a-alert
          v-if="status.below_floor"
          :type="status.user_selected ? 'warning' : 'error'"
          show-icon
          class="iso-alert"
          :message="
            status.user_selected
              ? $t('settings.isolation_below_floor_selected_title')
              : $t('settings.isolation_below_floor_title')
          "
          :description="
            status.user_selected
              ? $t('settings.isolation_below_floor_selected_body', {
                floor: levelLabel(status.floor),
              })
              : $t('settings.isolation_below_floor_body')
          "
        />

        <p class="iso-floor-note">
          <WarningOutlined />
          <span>{{ $t('settings.isolation_floor_note', { floor: levelLabel(status.floor) }) }}</span>
        </p>
      </section>

      <!-- 档位选择 -->
      <section class="iso-section">
        <div class="iso-section-head">
          <div>
            <h3>{{ $t('settings.isolation_pick') }}</h3>
            <p>{{ $t('settings.isolation_levels_hint') }}</p>
          </div>
        </div>

        <div
          class="iso-levels"
          role="radiogroup"
          :aria-label="$t('settings.isolation_pick')"
        >
          <button
            v-for="level in LEVELS"
            :key="level"
            type="button"
            role="radio"
            class="iso-level"
            :class="{
              'iso-level--active': level === currentEffective,
              'iso-level--pending': level === pendingLevel,
              'iso-level--disabled': !isSelectable(level),
            }"
            :aria-checked="level === currentEffective"
            :aria-disabled="!isSelectable(level) || saving"
            :disabled="!isSelectable(level) || saving"
            @click="applyLevel(level)"
          >
            <span class="iso-level-top">
              <span
                class="iso-radio"
                :class="{ 'iso-radio--on': level === currentEffective }"
                aria-hidden="true"
              >
                <CheckOutlined v-if="level === currentEffective" />
              </span>
              <span class="iso-level-name">{{ levelLabel(level) }}</span>
              <a-tag
                v-if="level === status.floor"
                color="blue"
                class="iso-badge"
              >
                {{ $t('settings.isolation_floor_badge') }}
              </a-tag>
              <a-tag
                v-if="level === currentEffective"
                color="green"
                class="iso-badge"
              >
                {{ $t('settings.isolation_active_badge') }}
              </a-tag>
              <a-tag
                v-if="!isSelectable(level)"
                class="iso-badge"
              >
                {{ $t('settings.isolation_unavailable_badge') }}
              </a-tag>
            </span>
            <span class="iso-level-desc">{{ levelDescription(level) }}</span>
            <span
              v-if="!isSelectable(level)"
              class="iso-level-reason"
            >
              {{ disabledReason(level) }}
            </span>
          </button>
        </div>
      </section>

      <!-- 容器运行时：只在选择容器挡位时需要，但状态要常驻可见 -->
      <section
        v-if="containerRuntime"
        class="iso-card iso-container"
        data-testid="iso-container-runtime"
      >
        <div class="iso-container-head">
          <div>
            <h3 class="iso-container-title">
              <ContainerOutlined />
              {{ $t('settings.isolation_container_title') }}
            </h3>
            <p class="iso-muted">
              {{ levelDescription('container') }}
            </p>
          </div>
          <a-tag :color="containerRuntime.installed ? 'green' : 'default'">
            {{
              containerRuntime.installed
                ? $t('settings.isolation_container_ready', { version: containerRuntime.version })
                : $t('settings.isolation_container_missing', { version: containerRuntime.version })
            }}
          </a-tag>
        </div>
        <p
          v-if="containerRuntime.reason"
          class="iso-container-reason"
        >
          {{ containerRuntime.reason }}
        </p>
        <p
          v-else-if="containerRuntime.engine?.engine_version"
          class="iso-container-reason"
        >
          {{
            $t('settings.isolation_container_engine', {
              version: containerRuntime.engine.engine_version,
              platform: containerRuntime.platform,
            })
          }}
        </p>
        <div
          v-if="!containerRuntime.installed && containerRuntime.supported"
          class="iso-container-actions"
        >
          <a-button
            type="primary"
            size="small"
            :loading="preparingContainer"
            data-testid="iso-container-prepare"
            @click="prepareContainerRuntimePackage"
          >
            {{ $t('settings.isolation_container_prepare') }}
          </a-button>
          <span
            v-if="containerRuntime.asset"
            class="iso-meta"
          >
            {{ containerRuntime.asset.name }} · SHA-256
            {{ containerRuntime.asset.sha256.slice(0, 12) }}…
          </span>
        </div>
        <pre
          v-if="containerHint"
          class="iso-code"
        >{{ containerHint }}</pre>
      </section>

      <!-- 维护 -->
      <section class="iso-section iso-maintenance">
        <div class="iso-section-head">
          <div>
            <h3>{{ $t('settings.isolation_maintenance') }}</h3>
            <p>{{ $t('settings.isolation_reset_hint') }}</p>
          </div>
        </div>
        <div class="iso-actions">
          <a-button
            :disabled="!status.runtime_override || saving"
            :loading="saving"
            @click="resetToConfig"
          >
            <template #icon>
              <ReloadOutlined />
            </template>
            {{ $t('settings.isolation_reset') }}
          </a-button>
          <span class="iso-muted">{{ $t('settings.isolation_reset_hint') }}</span>
        </div>

        <div class="iso-revoke">
          <div>
            <div class="iso-revoke-title">
              {{ $t('settings.isolation_revoke_title') }}
            </div>
            <p class="iso-muted">
              {{ $t('settings.isolation_revoke_body') }}
            </p>
          </div>
          <a-popconfirm
            :title="$t('settings.isolation_revoke_confirm')"
            :ok-text="$t('common.confirm')"
            :cancel-text="$t('common.cancel')"
            @confirm="revokeArtifacts"
          >
            <a-button
              danger
              size="small"
              :loading="revoking"
            >
              {{ $t('settings.isolation_revoke_button') }}
            </a-button>
          </a-popconfirm>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.iso-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-block-end: 20px;
}

.iso-head {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.iso-head-icon {
  margin-block-start: 3px;
  font-size: var(--fs-2xl);
  color: var(--primary);
}
.iso-title {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
}
.iso-intro {
  margin: 4px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-base);
  line-height: 1.6;
}

.iso-card {
  padding: 14px 16px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--bg-card, transparent);
}

.iso-current-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.iso-eyebrow {
  color: var(--text-secondary);
  font-size: var(--fs-base);
}
.iso-spacer {
  flex: 1;
}
.iso-muted {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.iso-alert {
  margin-block-start: 10px;
}

.iso-floor-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.iso-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.iso-section-head h3 {
  margin: 0;
  color: var(--text-primary);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}
.iso-section-head p {
  margin: 3px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.iso-levels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px;
}

.iso-level {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--bg-card, transparent);
  color: var(--text-primary);
  text-align: start;
  cursor: pointer;
  transition:
    border-color var(--dur-fast),
    background-color var(--dur-fast);
}
.iso-level:hover:not(.iso-level--disabled) {
  border-color: var(--primary);
}
.iso-level--active {
  border-color: var(--primary);
  background: var(--primary-bg, rgba(0, 0, 0, 0.02));
}
.iso-level--pending {
  opacity: 0.6;
}
.iso-level--disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.iso-level:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

.iso-level-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.iso-radio {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: var(--hairline-width) solid var(--border-strong, var(--border));
  border-radius: 50%;
  font-size: var(--fs-xs);
  color: var(--text-on-primary, #fff);
}
.iso-radio--on {
  border-color: var(--primary);
  background: var(--primary);
}
.iso-level-name {
  font-weight: var(--fw-medium, 500);
}
.iso-badge {
  margin-inline-start: 0;
}
.iso-level-desc {
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.iso-level-reason {
  color: var(--warning, var(--text-secondary));
  font-size: var(--fs-xs);
  line-height: 1.5;
}

.iso-container-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.iso-container-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 4px;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}
.iso-container-reason {
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: var(--fs-base);
  line-height: 1.6;
}
.iso-container-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-block-start: 10px;
}
.iso-meta {
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  word-break: break-all;
}
.iso-code {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-subtle);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.iso-maintenance {
  padding-block-start: 12px;
  border-block-start: var(--hairline-width) solid var(--border-card, var(--border));
}
.iso-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.iso-revoke {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-block-start: 16px;
  padding-block-start: 14px;
  border-block-start: var(--hairline-width) solid var(--border-card, var(--border));
}
.iso-revoke-title {
  margin-block-end: 4px;
  color: var(--text-primary);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}

@media (prefers-reduced-motion: reduce) {
  .iso-level {
    transition: none;
  }
}
</style>
