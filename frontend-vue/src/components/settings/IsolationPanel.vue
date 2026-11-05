<script setup lang="ts">
/**
 * 本机隔离档位 —— 设置页「执行隔离」标签页。
 *
 * ## 这个面板要回答的三个问题
 *
 * 1. **现在是什么档位？** 显示 `requested`（配置请求）与 `effective`（实际生效）
 *    两个值。两者不同就是发生了降级 —— 必须显式展示，否则就是静默降级。
 * 2. **能改成什么？** 只列出 `selectable` 里的档位；低于硬底线的置灰不可选。
 * 3. **怎么撤销？** 两个层次：撤销设置（回到配置文件）+ 撤销磁盘产物
 *    （AppContainer profile、目录完整性标签）。前者免费，后者必要 ——
 *    不撤销会让工作区目录长期是 Low 标签，影响后续未隔离使用。
 *
 * ## 为什么档位说明写在 UI 里而不是只放文档
 *
 * 用户选档位时看到的应该是"这个档位能挡住什么、挡不住什么"，而不是一个抽象名字。
 * 尤其是 `low_il` —— 它是 Windows 上的实际默认值，但它挡不住同用户读取，
 * 必须说清楚，否则用户会以为自己在跑 Docker 级隔离。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { SafetyOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  clearIsolationLevel,
  getIsolationStatus,
  revokeIsolationArtifacts,
  setIsolationLevel,
  type IsolationLevel,
  type IsolationStatus,
} from '../../api'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const revoking = ref(false)
const status = ref<IsolationStatus | null>(null)
/** 加载失败的原文；有值时展示重试入口而不是空白面板 */
const loadError = ref('')
/** 待切换的档位（提交前先记下来，避免提交中状态抖回旧值） */
const pendingLevel = ref<IsolationLevel | null>(null)

/** 档位由弱到强，与后端 `_ORDER` 严格一致 */
const ALL_LEVELS: IsolationLevel[] = ['none', 'job', 'low_il', 'restricted', 'appcontainer']

const currentEffective = computed(() => status.value?.effective ?? null)

/**
 * 档位是否可选：以后端 `selectable` 为准。
 *
 * 不在前端自己算底线 —— 底线是安全策略，只能有**一处**事实来源。
 * 前端重算一遍，两边规则一旦漂移就会出现"UI 可选、后端拒绝"或反之。
 */
function isSelectable(level: IsolationLevel): boolean {
  return status.value?.selectable?.includes(level) ?? false
}

/** 为什么这个档位不可选 —— 让鼠标悬停时能解释原因，而不是单纯置灰 */
function disabledReason(level: IsolationLevel): string {
  if (!status.value) return ''
  if (isSelectable(level)) return ''
  return t('settings.isolation_below_floor_hint', { floor: status.value.floor })
}

function levelLabel(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}`)
}

function levelDescription(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}_desc`)
}

/**
 * 鉴权未就绪时的自愈：先补建本机会话，再重试一次。
 *
 * 为什么必须在面板这一层兜：`initToken()` 里本地分支是 `void ensureLocalSession()`
 * —— **刻意不 await**，为了让首帧不被本地后端拖住。设置页若在这之前就挂载，
 * `requestToken()` 返回空串，网关在 `ValidateToken` 之前就判 `tokenStr == ""`，
 * 于是回一句 `missing authorization`（英文原文，且与"权限不足"毫无关系）。
 * 这里补一次会话再重试，正常路径下用户根本看不到这个错误。
 */
async function load() {
  loading.value = true
  loadError.value = ''
  try {
    status.value = await getIsolationStatus()
  } catch (err) {
    const status401 = (err as { response?: { status?: number } })?.response?.status === 401
    if (status401) {
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

async function applyLevel(level: IsolationLevel) {
  if (!isSelectable(level) || saving.value) return
  // 选的就是当前生效档位：不必往返一次
  if (level === status.value?.runtime_override) return

  saving.value = true
  pendingLevel.value = level
  try {
    const next = await setIsolationLevel(level, t('settings.isolation_change_reason'))
    status.value = next
    message.success(t('settings.isolation_changed', { level: levelLabel(next.effective) }))
    // 请求值被抬到底线时，明确告诉用户"你要的没生效、实际是这个"
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
    const next = await clearIsolationLevel()
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

async function revokeArtifacts() {
  if (revoking.value) return
  revoking.value = true
  try {
    const result = await revokeIsolationArtifacts()
    // 如实回报：profile 删没删掉、标签还原没有 —— 不能只说一句"已撤销"
    const okLabel = result.integrity_labels?.every((item) => item.ok)
    message.success(
      okLabel
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
  <div class="iso-block">
    <div class="iso-head">
      <SafetyOutlined class="iso-icon" />
      <div>
        <div class="iso-title">
          {{ $t('settings.isolation_title') }}
        </div>
        <p class="iso-intro">
          {{ $t('settings.isolation_intro') }}
        </p>
      </div>
    </div>

    <!-- 加载失败要给出可执行的重试，不是空白 -->
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
      <!-- 当前状态：请求值 vs 实际生效值 -->
      <div class="iso-current">
        <div class="iso-current-row">
          <span class="iso-current-label">{{ $t('settings.isolation_current') }}</span>
          <a-tag :color="status.below_floor ? 'red' : 'green'">
            {{ levelLabel(status.effective) }}
          </a-tag>
          <span
            v-if="status.runtime_override"
            class="iso-temp-tag"
          >
            {{ $t('settings.isolation_temp_applied') }}
          </span>
        </div>
        <!-- 降级必须显式说明，这是整个面板最重要的一条信息 -->
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
        />
        <a-alert
          v-if="status.below_floor"
          type="error"
          show-icon
          class="iso-alert"
          :message="$t('settings.isolation_below_floor_title')"
          :description="$t('settings.isolation_below_floor_body')"
        />
        <div class="iso-floor-hint">
          <WarningOutlined />
          <span>{{ $t('settings.isolation_floor_note', { floor: levelLabel(status.floor) }) }}</span>
        </div>
      </div>

      <!-- 档位选择 -->
      <div class="iso-picker">
        <div class="iso-picker-title">
          {{ $t('settings.isolation_pick') }}
        </div>
        <div class="iso-levels">
          <button
            v-for="level in ALL_LEVELS"
            :key="level"
            type="button"
            class="iso-level"
            :class="{
              'iso-level--active': level === currentEffective,
              'iso-level--pending': level === pendingLevel,
              'iso-level--disabled': !isSelectable(level),
            }"
            :disabled="!isSelectable(level) || saving"
            :title="disabledReason(level)"
            @click="applyLevel(level)"
          >
            <div class="iso-level-head">
              <span class="iso-level-name">{{ levelLabel(level) }}</span>
              <a-tag
                v-if="level === status.floor"
                color="blue"
                class="iso-level-badge"
              >
                {{ $t('settings.isolation_floor_badge') }}
              </a-tag>
              <a-tag
                v-if="level === currentEffective"
                color="green"
                class="iso-level-badge"
              >
                {{ $t('settings.isolation_active_badge') }}
              </a-tag>
            </div>
            <p class="iso-level-desc">
              {{ levelDescription(level) }}
            </p>
          </button>
        </div>
      </div>

      <!-- 撤销：设置层 -->
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
        <span class="iso-action-hint">{{ $t('settings.isolation_reset_hint') }}</span>
      </div>

      <!-- 撤销：磁盘产物层 —— 这是"随时能撤销"里容易被忽略的一半 -->
      <div class="iso-revoke">
        <div class="iso-revoke-title">
          {{ $t('settings.isolation_revoke_title') }}
        </div>
        <p class="iso-revoke-body">
          {{ $t('settings.isolation_revoke_body') }}
        </p>
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
    </template>
  </div>
</template>

<style scoped>
.iso-block { padding: 4px 0 20px; }
.iso-head { display: flex; gap: 10px; margin-bottom: 16px; }
.iso-icon { font-size: 20px; color: var(--primary); margin-top: 2px; }
.iso-title { font-weight: var(--fw-semibold); color: var(--text-primary); }
.iso-intro { margin: 4px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.iso-alert { margin-bottom: 12px; }

.iso-current { margin-bottom: 20px; }
.iso-current-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.iso-current-label { color: var(--text-secondary); font-size: 13px; }
.iso-temp-tag { color: var(--text-secondary); font-size: 12px; }
.iso-floor-hint {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.iso-picker { margin-bottom: 20px; }
.iso-picker-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 10px; }
.iso-levels { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }

/* 用原生 button 而不是 a-radio：每个档位要带两行说明，radio 的排版撑不开 */
.iso-level {
  display: block;
  width: 100%;
  text-align: start;
  padding: 10px 12px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--bg-card, transparent);
  color: var(--text-primary);
  cursor: pointer;
  transition: border-color var(--dur-fast), background-color var(--dur-fast);
}
.iso-level:hover:not(.iso-level--disabled) { border-color: var(--primary); }
.iso-level--active { border-color: var(--primary); background: var(--primary-bg, rgba(0, 0, 0, 0.02)); }
.iso-level--pending { opacity: 0.6; }
.iso-level--disabled { cursor: not-allowed; opacity: 0.5; }
.iso-level:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

.iso-level-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.iso-level-name { font-weight: var(--fw-medium, 500); }
.iso-level-badge { margin-inline-start: 0; }
.iso-level-desc { margin: 4px 0 0; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }

.iso-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
.iso-action-hint { color: var(--text-secondary); font-size: 12px; }

.iso-revoke {
  padding-top: 16px;
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
}
.iso-revoke-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 6px; }
.iso-revoke-body { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }

@media (prefers-reduced-motion: reduce) {
  .iso-level { transition: none; }
}
</style>
